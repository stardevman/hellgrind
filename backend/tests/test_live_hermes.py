"""Hermes LLM modu: LangGraph akışı SAHTE LLM ile, ağ yasak. langgraph kurulu değilse atlanır (requirements-live.txt).

Hiçbir gerçek Anthropic isteği yapılmaz; `guard` yerine sahte anahtar dönen bir işlev konur.
"""
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import text

pytest.importorskip("langgraph")

from app.budget import killswitch as ks  # noqa: E402
from app.budget import pricing  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.hermes import rules  # noqa: E402
from app.live import hermes_llm  # noqa: E402
from app.live.hermes_llm import Breakdown, parse_with_llm  # noqa: E402

SCRIPT = ("İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\nMerhaba.\n\n"
          "DIŞ. SOKAK - GÜN\n\nAyşe yürür.\n\nAYŞE\nSelam.\n\n"
          "İÇ. ARABA - GÜN\n\nSessiz bir yolculuk.\n")
USAGE = {"input_tokens": 1000, "output_tokens": 500}
CALL_COST = pricing.estimate_llm(1000, 500)


def shot(characters=("Kerem",), speaker=None, duration=4.0):
    return {"description": "Bir çekim", "visual_prompt": "a quiet room", "camera": {"shot": "medium", "movement": "static"},
            "emotion": "neutral", "characters": list(characters), "duration_s": duration, "action_complexity": 0.2,
            "dialogue": [{"character": speaker, "line": "Merhaba.", "emotion": "neutral"}] if speaker else []}


class FakeLLM:
    """behave(human_text, call_no) -> shots listesi (sözlük) ya da None (şemaya uymayan çıktı)."""

    def __init__(self, behave):
        self.behave, self.calls, self.lock, self.threads = behave, [], threading.Lock(), set()

    def invoke(self, messages):
        with self.lock:
            self.calls.append(messages[-1][1])
            n = len(self.calls)
            self.threads.add(threading.get_ident())
        shots = self.behave(messages[-1][1], n)
        parsed = Breakdown.model_validate({"shots": shots}) if shots is not None else None
        return {"raw": SimpleNamespace(usage_metadata=USAGE), "parsed": parsed, "parsing_error": None if parsed else "bozuk çıktı"}


def good(human, n):
    return [shot(("Ayşe",), "Ayşe") if "Ayşe yürür" in human else shot(("Kerem",), "Kerem")]


@pytest.fixture
def hermes(monkeypatch, no_network, mk_project):
    monkeypatch.setattr(hermes_llm, "guard", lambda key: "sahte-anahtar")
    return mk_project


def run(project, llm, script=SCRIPT):
    return parse_with_llm(script, project, llm=llm)


def ledger(pid):
    with session_scope() as s:
        return s.execute(text("SELECT kind::text, provider, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}).all()


# ---------- akış ----------

def test_each_scene_is_extracted_once_and_in_parallel_branches(hermes):
    llm = FakeLLM(good)
    run(hermes(), llm)
    assert len(llm.calls) == 3
    assert all("Kadro: Kerem, Ayşe" in c for c in llm.calls)
    assert any("Kerem oturur" in c for c in llm.calls) and any("Ayşe yürür" in c for c in llm.calls)


def test_output_has_the_same_schema_as_the_rule_based_parser(hermes):
    out = run(hermes(), FakeLLM(good))
    expected = rules.parse(SCRIPT)
    assert out["cast"] == expected["cast"] == ["Kerem", "Ayşe"]
    assert set(out["shots"][0]) == set(expected["shots"][0])
    assert set(out["shots"][0]["camera"]) == set(expected["shots"][0]["camera"]) == {"shot", "movement", "angle", "lens_mm"}
    assert [s["scene_id"] for s in out["shots"]] == ["S001_SH01", "S002_SH01", "S003_SH01"]
    assert [s["heading"] for s in out["shots"]] == ["İÇ. ODA - GECE", "DIŞ. SOKAK - GÜN", "İÇ. ARABA - GÜN"]


def test_scenes_without_shots_do_not_consume_a_scene_number(hermes):
    """Başlıktan önceki metin (kapak yazısı) çekim üretmez; numaralandırma kural tabanlı ayrıştırıcı gibi S001'den başlar."""
    def behave(human, n):
        return [] if "KAPAK" in human else good(human, n)
    out = run(hermes(), FakeLLM(behave), script="KAPAK SAYFASI\n\n" + SCRIPT)
    assert [s["scene_id"] for s in out["shots"]] == ["S001_SH01", "S002_SH01", "S003_SH01"]


# ---------- doğrulama ve onarım ----------

def test_an_invented_character_is_repaired_with_one_extra_call(hermes):
    def behave(human, n):
        if "Hatalar" not in human and "Ayşe yürür" in human:
            return [shot(("Zeynep",), "Zeynep")]                        # kadroda yok
        return good(human, n)
    llm = FakeLLM(behave)
    out = run(hermes(), llm)
    assert len(llm.calls) == 4 and sum("Hatalar" in c for c in llm.calls) == 1
    assert out["shots"][1]["characters"] == ["Ayşe"]


def test_a_dialogue_speaker_outside_the_cast_also_triggers_repair(hermes):
    def behave(human, n):
        if "Hatalar" not in human and "Kerem oturur" in human:
            return [shot(("Kerem",), "Zeynep")]
        return good(human, n)
    llm = FakeLLM(behave)
    run(hermes(), llm)
    assert sum("Hatalar" in c for c in llm.calls) == 1


def test_repair_is_limited_to_two_rounds_and_the_loop_ends(hermes):
    def behave(human, n):
        return [shot(("Zeynep", "Kerem"))] if "Ayşe yürür" in human else good(human, n)
    llm = FakeLLM(behave)
    out = run(hermes(), llm)
    assert len(llm.calls) == 3 + 2                                    # 3 çıkarım + o sahne için en çok 2 onarım
    assert out["shots"][1]["characters"] == ["Kerem"]                 # onarılamayan uydurma ad kadroya sızmaz


def test_empty_shot_list_is_repaired_then_dropped(hermes):
    llm = FakeLLM(lambda human, n: [] if "Ayşe yürür" in human else good(human, n))
    out = run(hermes(), llm)
    assert len(llm.calls) == 5 and [s["scene_id"] for s in out["shots"]] == ["S001_SH01", "S002_SH01"]


# ---------- bütçe (değişmez kural 3) ----------

def test_every_call_is_reserved_and_committed_at_the_real_token_cost(hermes):
    pid = hermes(limit=100)
    llm = FakeLLM(good)
    run(pid, llm)

    rows = ledger(pid)
    assert [k for k, _, _ in rows].count("reserve") == [k for k, _, _ in rows].count("commit") == len(llm.calls) == 3
    assert {p for _, p, _ in rows} == {f"anthropic:{hermes_llm.settings.hermes_model}"}
    assert all(float(a) == CALL_COST for k, _, a in rows if k == "commit")
    with session_scope() as s:
        spent = float(s.execute(text("SELECT spent_usd FROM projects WHERE id = :p"), {"p": pid}).scalar())
    assert spent == pytest.approx(3 * CALL_COST, abs=1e-4)


def test_a_frozen_project_makes_no_llm_call(hermes):
    pid = hermes()
    ks.freeze(pid, "test")
    llm = FakeLLM(good)
    with pytest.raises(ks.ProjectFrozen):
        run(pid, llm)
    assert llm.calls == []


def test_a_budget_that_cannot_cover_a_call_stops_before_the_call(hermes):
    pid = hermes(limit=0.01)                                           # bir çağrının rezervasyonu ~0.06 USD
    llm = FakeLLM(good)
    with pytest.raises(ks.BudgetExceeded):
        run(pid, llm)
    assert llm.calls == [] and ks.is_frozen(pid)


def test_unparsable_output_raises_but_the_money_already_spent_is_recorded(hermes):
    pid = hermes(limit=100)
    with pytest.raises(ValueError, match="şemaya uymadı"):
        run(pid, FakeLLM(lambda human, n: None))
    assert "commit" in [k for k, _, _ in ledger(pid)]


# ---------- yönlendirme ----------

def test_ingest_routes_to_the_llm_only_in_paid_llm_mode_and_passes_the_project(monkeypatch):
    from app.config import settings
    from app.hermes.ingest import parse_script
    seen = []
    monkeypatch.setattr(hermes_llm, "parse_with_llm", lambda t, p: seen.append(p) or {"cast": [], "shots": []})

    parse_script(SCRIPT, "proje-1")                                    # varsayılan: kural tabanlı, LLM'e gidilmez
    assert seen == []

    monkeypatch.setattr(settings, "hermes_mode", "llm")
    parse_script(SCRIPT, "proje-1")                                    # DRY_RUN=1: hâlâ kural tabanlı
    assert seen == []
    monkeypatch.setattr(settings, "dry_run", False)                    # (bellekte; parse_with_llm taklit edildi)
    parse_script(SCRIPT, "proje-1")
    assert seen == ["proje-1"]


def test_script_upload_answers_423_when_the_budget_runs_out_during_parsing(api, api_project, monkeypatch):
    pid = api_project()
    monkeypatch.setattr("app.api.panel.ingest", lambda *a, **k: (_ for _ in ()).throw(ks.BudgetExceeded(pid)))
    r = api.post(f"/api/projects/{pid}/script", json={"text": SCRIPT})
    assert r.status_code == 423
