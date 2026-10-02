"""ÜCRETLİ Hermes LLM modu: sahte LLM ile. Hiçbir test gerçek servise istek göndermez.

LangGraph gerektirir (requirements-live.txt); kurulu değilse bu dosya atlanır. Sahte LLM, kural tabanlı ayrıştırıcıdan
"mükemmel" taslaklar üretir ya da kasten bozar; böylece akış, doğrulama, onarım ve bütçe muhasebesi sınanır.
"""
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")

from sqlalchemy import text  # noqa: E402

from app.budget import killswitch as ks  # noqa: E402
from app.budget import pricing  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.engine.providers import TransientError  # noqa: E402
from app.hermes import ingest as hermes_ingest  # noqa: E402
from app.hermes import rules  # noqa: E402
from app.live import PaidCallBlocked, hermes_llm  # noqa: E402
from app.live.hermes_llm import LineDraft, SceneDraft, ShotDraft, parse_with_llm, problems_of  # noqa: E402

pytestmark = pytest.mark.usefixtures("paid")
USAGE = (1000, 400)


def kind_of(shot):
    cam = shot["camera"]
    if cam["shot"] == "close_up":
        return "dialogue"
    if cam["movement"] in ("handheld", "tracking"):
        return "action"
    return "establish" if cam["movement"] == "pan" else "beat"


def perfect(chunk):
    """Kural tabanlı ayrıştırıcının sonucundan, kusursuz bir LLM yanıtı."""
    return SceneDraft(shots=[ShotDraft(
        description=s["description"], visual_prompt=s["visual_prompt"], kind=kind_of(s), emotion=s["emotion"],
        characters=s["characters"], dialogue=[LineDraft(**d) for d in s["dialogue"]]) for s in rules.parse(chunk)["shots"]])


def chunk_of(prompt):
    return prompt.split("Sahne:\n", 1)[1].split("\n\nÖnceki çıktın")[0]


class FakeLLM:
    """ChatAnthropic'in kullanılan yüzeyi: with_structured_output(...).invoke(...) -> {raw, parsed, parsing_error}."""

    def __init__(self, brain=None, usage=USAGE, delay=0.0):
        self.brain, self.usage, self.delay = brain or (lambda prompt, n: perfect(chunk_of(prompt))), usage, delay
        self.prompts, self.lock, self.live, self.peak = [], threading.Lock(), 0, 0

    def with_structured_output(self, schema, include_raw=False):
        assert schema is SceneDraft and include_raw is True
        return SimpleNamespace(invoke=self._invoke)

    def _invoke(self, messages):
        assert messages[0][0] == "system" and messages[1][0] == "human"
        with self.lock:
            self.prompts.append(messages[1][1])
            n, self.live = len(self.prompts), self.live + 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.delay)
            parsed = self.brain(messages[1][1], n)
        finally:
            with self.lock:
                self.live -= 1
        raw = SimpleNamespace(usage_metadata={"input_tokens": self.usage[0], "output_tokens": self.usage[1]})
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


def ledger(pid):
    with session_scope() as s:
        rows = s.execute(text("SELECT kind, provider, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}).all()
    return [(k, p, float(a)) for k, p, a in rows]


# ---------- şema eşdeğerliği ----------

def test_a_perfect_llm_gives_exactly_what_the_rule_parser_gives(mk_project):
    pid = mk_project()
    got = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM())
    assert got == rules.parse(rules.SAMPLE_SCRIPT)


def test_output_has_the_same_schema_as_the_rule_parser(mk_project):
    pid = mk_project()
    got, ref = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM()), rules.parse(rules.SAMPLE_SCRIPT)
    assert set(got) == set(ref) == {"cast", "shots"}
    for a, b in zip(got["shots"], ref["shots"]):
        assert set(a) == set(b) and set(a["camera"]) == set(b["camera"])
        assert all(type(a[k]) is type(b[k]) for k in a)


def test_the_output_can_be_ingested_like_any_other_parse(mk_project, monkeypatch):
    pid = mk_project()
    monkeypatch.setattr(settings, "hermes_mode", "llm")
    monkeypatch.setattr(hermes_llm, "make_llm", lambda key: FakeLLM())
    result = hermes_ingest.ingest(pid, rules.SAMPLE_SCRIPT)
    assert result["cast"] == ["Kerem", "Ayşe"] and result["shots"] == 4
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM scenes WHERE project_id = :p"), {"p": pid}).scalar() == 4


def test_ingest_uses_the_llm_only_in_paid_mode_and_only_when_asked(monkeypatch, mk_project):
    seen = []
    monkeypatch.setattr(hermes_llm, "parse_with_llm", lambda text, pid, llm=None: seen.append((text, pid)) or {"cast": [], "shots": []})
    monkeypatch.setattr(settings, "hermes_mode", "llm")
    hermes_ingest.parse_script("metin", "proje-1")
    monkeypatch.setattr(settings, "hermes_mode", "rules")
    hermes_ingest.parse_script("metin", "proje-2")
    monkeypatch.setattr(settings, "hermes_mode", "llm")
    monkeypatch.setattr(settings, "dry_run", True)
    hermes_ingest.parse_script("metin", "proje-3")
    assert seen == [("metin", "proje-1")]


# ---------- rol paylaşımı ----------

def test_numbers_and_numbering_come_from_code_not_from_the_llm(mk_project):
    pid = mk_project()

    def brain(prompt, n):
        chunk = chunk_of(prompt)
        d = perfect(chunk)
        d.shots[0].kind = "action"                            # LLM "aksiyon" dese bile sayılar kodla hesaplanır
        return d

    got = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM(brain))
    assert [s["scene_id"] for s in got["shots"]] == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
    first = got["shots"][0]
    assert first["action_complexity"] == 0.8 and first["camera"]["shot"] == "wide"
    assert first["duration_s"] == rules.shot_duration(first["description"], first["dialogue"], True)


def test_the_visual_prompt_falls_back_to_the_rule_based_one_when_the_llm_leaves_it_empty(mk_project):
    pid = mk_project()

    def brain(prompt, n):
        d = perfect(chunk_of(prompt))
        for s in d.shots:
            s.visual_prompt = "  "
        return d

    got = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM(brain))
    assert [s["visual_prompt"] for s in got["shots"]] == [s["visual_prompt"] for s in rules.parse(rules.SAMPLE_SCRIPT)["shots"]]


# ---------- paralellik ----------

def test_scenes_are_extracted_in_parallel_up_to_the_cap(mk_project):
    pid = mk_project()
    script = "\n\n".join(f"İÇ. YER {i}\n\nKerem bekler {i}." for i in range(1, 9))
    llm = FakeLLM(delay=0.15)
    got = parse_with_llm(script, pid, llm=llm)
    assert len(got["shots"]) == 8 and 2 <= llm.peak <= hermes_llm.MAX_PARALLEL_CALLS
    assert [s["scene_id"] for s in got["shots"]] == [f"S{i:03d}_SH01" for i in range(1, 9)]


# ---------- onarım ----------

def test_a_dropped_dialogue_line_is_caught_and_repaired(mk_project):
    pid = mk_project()

    def brain(prompt, n):
        d = perfect(chunk_of(prompt))
        if "Burada kimse yok mu" in prompt and "reddedildi" not in prompt:
            d.shots[0].dialogue = []                          # LLM bir repliği düşürdü
        return d

    llm = FakeLLM(brain)
    got = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=llm)
    assert got == rules.parse(rules.SAMPLE_SCRIPT) and len(llm.prompts) == 3        # 2 çıkarma + 1 onarım
    repair = next(p for p in llm.prompts if "reddedildi" in p)
    assert "Replikler senaryodakiyle aynı değil" in repair and '"shots"' in repair


def test_an_unreadable_answer_is_repaired_too(mk_project):
    pid = mk_project()
    calls = []

    def brain(prompt, n):
        calls.append(n)
        return None if n == 1 else perfect(chunk_of(prompt))

    got = parse_with_llm("İÇ. ODA\n\nKerem bekler.", pid, llm=FakeLLM(brain))
    assert len(calls) == 2 and got["shots"][0]["description"] == "Kerem bekler."


def test_after_two_repair_rounds_the_scene_falls_back_to_the_free_rule_parser(mk_project, caplog):
    pid = mk_project()

    def brain(prompt, n):
        d = perfect(chunk_of(prompt))
        if "DIŞ. ÇÖKEN KÖPRÜ" in prompt:                      # ikinci sahne hep bozuk
            d.shots[0].dialogue = []
        return d

    llm = FakeLLM(brain)
    got = parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=llm)
    bridge_calls = [p for p in llm.prompts if "DIŞ. ÇÖKEN KÖPRÜ" in p]
    assert len(bridge_calls) == 3 and hermes_llm.MAX_REPAIR_ROUNDS == 2               # 1 çıkarma + en çok 2 onarım (spec)
    assert got == rules.parse(rules.SAMPLE_SCRIPT)                                    # düşülen sahne yine geçerli ve numaralı
    assert "kural tabanlı ayrıştırıcıya düşülüyor" in caplog.text


# ---------- doğrulama ----------

CHUNK = "İÇ. ODA\n\nKerem bekler.\n\nKEREM\nBurada kimse yok mu?\n\nAYŞE\nSesini alçalt.\n"
CAST = ["Kerem", "Ayşe"]


def draft(*shots):
    return SceneDraft(shots=list(shots))


def shot(dialogue=(), characters=("Kerem",), description="Kerem bekler."):
    return ShotDraft(description=description, characters=list(characters),
                     dialogue=[LineDraft(character=c, line=l) for c, l in dialogue])


GOOD = draft(shot([("Kerem", "Burada kimse yok mu?")]), shot([("Ayşe", "Sesini alçalt.")], characters=("Ayşe",), description="Ayşe gelir."))


def test_a_faithful_draft_has_no_problems():
    assert problems_of(GOOD, CHUNK, CAST) == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("no draft", lambda: None, "okunamadı"),
    ("no shots", lambda: draft(), "hiç çekim yok"),
    ("empty description", lambda: draft(shot([("Kerem", "Burada kimse yok mu?")], description=" "), GOOD.shots[1]), "açıklama boş"),
    ("character outside the cast", lambda: draft(shot([("Kerem", "Burada kimse yok mu?")], characters=("Hayalet",)), GOOD.shots[1]), "'Hayalet' kadroda yok"),
    ("speaker outside the cast", lambda: draft(shot([("Hayalet", "Burada kimse yok mu?")]), GOOD.shots[1]), "'Hayalet' kadroda yok"),
    ("dropped line", lambda: draft(shot([("Kerem", "Burada kimse yok mu?")])), "Replikler"),
    ("invented line", lambda: draft(*GOOD.shots, shot([("Kerem", "Uydurma söz.")])), "Replikler"),
    ("changed word", lambda: draft(shot([("Kerem", "Burada kimse var mı?")]), GOOD.shots[1]), "Replikler"),
    ("wrong speaker", lambda: draft(shot([("Ayşe", "Burada kimse yok mu?")]), shot([("Kerem", "Sesini alçalt.")])), "Replikler"),
    ("swapped order", lambda: draft(GOOD.shots[1], GOOD.shots[0]), "Replikler"),
    ("empty line text", lambda: draft(shot([("Kerem", " ")]), GOOD.shots[1]), "boş replik"),
    ("too many lines in a shot", lambda: draft(shot([("Kerem", "Burada"), ("Kerem", "kimse"), ("Kerem", "yok mu?")]), GOOD.shots[1]), "en çok 2 replik"),
])
def test_validation_catches_every_kind_of_unfaithful_draft(name, mutate, needle):
    errors = problems_of(mutate(), CHUNK, CAST)
    assert errors and any(needle in e for e in errors), (name, errors)


def test_case_and_punctuation_do_not_matter_but_the_words_do():
    d = draft(shot([("Kerem", "burada KİMSE yok mu")]), GOOD.shots[1])
    assert problems_of(d, CHUNK, CAST) == []


def test_a_speech_split_across_two_shots_is_accepted():
    chunk = "İÇ. ODA\n\nKerem bekler.\n\nKEREM\nBirinci cümle burada. İkinci cümle şurada.\n"
    d = draft(shot([("Kerem", "Birinci cümle burada.")]), shot([("Kerem", "İkinci cümle şurada.")], description="Devam."))
    assert problems_of(d, chunk, ["Kerem"]) == []


# ---------- bütçe: her LLM çağrısı rezervasyondan geçer ----------

def test_every_llm_call_is_reserved_then_committed_with_the_real_token_cost(mk_project):
    pid = mk_project()
    llm = FakeLLM(usage=(1200, 300))
    parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=llm)
    rows = ledger(pid)
    assert [(k, p) for k, p, _ in rows].count(("reserve", "anthropic")) == 2 and [k for k, _, _ in rows].count("commit") == 2
    commits = [a for k, _, a in rows if k == "commit"]
    assert commits == [pytest.approx(pricing.estimate_llm(1200, 300), abs=1e-4)] * 2
    first_prompt = llm.prompts[0]
    expected = pricing.estimate_llm(hermes_llm.PROMPT_OVERHEAD_TOKENS + len(first_prompt) // hermes_llm.CHARS_PER_TOKEN, hermes_llm.MAX_OUTPUT_TOKENS)
    assert any(abs(a - expected) < 1e-4 for k, _, a in rows if k == "reserve")
    assert ks.r().exists(ks.FROZEN.format(pid)) == 0


def test_repair_calls_are_budgeted_like_any_other(mk_project):
    pid = mk_project()

    def brain(prompt, n):
        d = perfect(chunk_of(prompt))
        if "reddedildi" not in prompt and "Burada kimse yok mu" in prompt:
            d.shots[0].dialogue = []
        return d

    parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM(brain))
    assert [k for k, _, _ in ledger(pid)].count("commit") == 3


def test_a_transient_llm_error_is_retried_with_a_fresh_reservation(mk_project):
    pid = mk_project()

    class Overloaded(Exception):
        status_code = 529

    def brain(prompt, n):
        if n == 1:
            raise Overloaded("overloaded")
        return perfect(chunk_of(prompt))

    got = parse_with_llm("İÇ. ODA\n\nKerem bekler.", pid, llm=FakeLLM(brain))
    kinds = [k for k, _, _ in ledger(pid)]
    assert got["shots"] and kinds == ["reserve", "release", "reserve", "commit"]


def test_a_permanent_llm_error_propagates_and_releases_the_reservation(mk_project):
    pid = mk_project()

    class BadRequest(Exception):
        status_code = 400

    with pytest.raises(BadRequest):
        parse_with_llm("İÇ. ODA\n\nKerem bekler.", pid, llm=FakeLLM(lambda p, n: (_ for _ in ()).throw(BadRequest("x"))))
    assert [k for k, _, _ in ledger(pid)] == ["reserve", "release"]


def test_the_llm_gives_up_after_three_transient_failures(mk_project):
    pid = mk_project()

    class Overloaded(Exception):
        status_code = 503

    llm = FakeLLM(lambda p, n: (_ for _ in ()).throw(Overloaded("x")))
    with pytest.raises(TransientError):
        parse_with_llm("İÇ. ODA\n\nKerem bekler.", pid, llm=llm)
    assert len(llm.prompts) == hermes_llm.MAX_ATTEMPTS and [k for k, _, _ in ledger(pid)].count("release") == 3


def test_the_budget_limit_stops_parsing_and_freezes_the_project(mk_project):
    pid = mk_project(limit=0.01)                              # en küçük geçerli bütçe; tek LLM çağrısının rezervasyonu bundan büyük
    with pytest.raises(ks.ProjectFrozen):
        parse_with_llm(rules.SAMPLE_SCRIPT, pid, llm=FakeLLM())
    assert ks.is_frozen(pid) and ledger(pid) == []


# ---------- kilit ----------

def test_free_mode_blocks_before_an_llm_is_even_built(monkeypatch, mk_project):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(hermes_llm, "make_llm", lambda key: pytest.fail("LLM kuruldu"))
    with pytest.raises(PaidCallBlocked):
        parse_with_llm(rules.SAMPLE_SCRIPT, mk_project())


def test_a_missing_key_blocks_before_an_llm_is_even_built(monkeypatch, mk_project):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(hermes_llm, "make_llm", lambda key: pytest.fail("LLM kuruldu"))
    with pytest.raises(PaidCallBlocked, match="ANTHROPIC_API_KEY"):
        parse_with_llm(rules.SAMPLE_SCRIPT, mk_project())


def test_an_empty_script_costs_nothing(mk_project):
    pid = mk_project()
    assert parse_with_llm("", pid, llm=FakeLLM(lambda p, n: pytest.fail("çağrı yapıldı"))) == {"cast": [], "shots": []}
    assert ledger(pid) == []


def test_the_real_chat_model_is_built_with_the_configured_model_and_key(monkeypatch):
    """Gerçek ChatAnthropic yalnızca kurulur (ağ çağrısı yok); paket kurulu değilse atlanır."""
    pytest.importorskip("langchain_anthropic")
    model = hermes_llm.make_llm("test-anahtar")
    assert model.model == settings.hermes_model and model.max_tokens == hermes_llm.MAX_OUTPUT_TOKENS and model.temperature == 0


# ---------- sahne bölme ----------

def test_split_scenes_keeps_text_before_the_first_heading_as_its_own_part():
    chunks = rules.split_scenes("Giriş cümlesi.\n\nİÇ. ODA\n\nKerem bekler.\n\nDIŞ. SOKAK\n\nYağmur yağar.")
    assert len(chunks) == 3 and chunks[0].startswith("Giriş") and chunks[1].startswith("İÇ. ODA") and chunks[2].startswith("DIŞ. SOKAK")
    assert rules.split_scenes("") == [] and rules.split_scenes("\n\n  \n") == []
    assert len(rules.split_scenes("İÇ. A\n\nx\n\nİÇ. B\n\ny")) == 2
