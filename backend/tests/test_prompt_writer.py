"""Yönetmen istemi katmanı: LLM istem yazıcı (sahte istemciyle), elle istem (yönetmen geçersiz kılması) ve sade
kurallar istemi. Ücretli çağrı yok: LLM her testte sahte `prompt_writer.ASK` ile değiştirilir."""
import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.engine import prompt_writer, scene_parts
from app.models import PromptCache, Scene
from tests.fal_queue_fake import validate as schema_errors
from tests.test_reference_mode import nora, plan_for, ref_defaults  # noqa: F401 — fikstür


def _inp(user: str) -> dict:
    return json.loads(user.split("Input (JSON):\n\n", 1)[1].split("\n\nYour previous answer", 1)[0])


def good_text(inp: dict) -> str:
    """Yönetmen tarzı istem, kanonik [ImageN] yazımıyla (motor biçimine çeviri yazıcının işi)."""
    head = [inp["style"]["sentence"]]
    for r in inp["references"]:
        n = r["number"]
        if r["kind"] == "location":
            head.append(f"The farmyard [Image{n}] is {r['description']}. Use [Image{n}] only for the look of the place "
                        "(architecture, materials, colors, light); do not copy its camera position or viewpoint.")
        else:
            head.append(f"{r['name']} [Image{n}] is {r['description']}" +
                        (f"; always {'; '.join(r['invariants'])}" if r["invariants"] else "") + ".")
    shots = []
    for i, sh in enumerate(inp["shots"]):
        line = f"{sh['label']}, {sh['camera']}, {sh['camera_height']}: "
        if i == 0:
            line += " ".join(f"{r['name']} [Image{r['number']}] moves through the farmyard, turning toward the camera "
                             "with a quick, curious tilt of the head." for r in inp["references"])
        else:
            line += ("Nora [Image1] kneels slowly on the packed earth, holds out her open palm and smiles; the chick "
                     "[Image2] hesitates, then hops forward with two quick bounces.")
        for d in sh["dialogue"]:
            line += f' {d["who"]} says: "{d["line"]}".'
        shots.append(line)
    tail = ("Bouncy cartoony timing with clear anticipation and soft overshoot on every hop. " +
            ("Sound: hens clucking, grain pattering on the ground, a light breeze; voices close and warm. "
             if inp["audio"] else "") + "Exactly one girl and one chick; no subtitles or text on screen.")
    return "\n\n".join([" ".join(head), *shots, tail])


@pytest.fixture
def llm(monkeypatch):
    """Sahte LLM: çağrıları sayar; `answers` listesi verilirse sırayla onları döner (yoksa geçerli bir istem)."""
    class Fake:
        calls: list = []
        answers: list = []

        def __call__(self, project_id, system, user):
            self.calls.append({"system": system, "user": user})
            inp = _inp(user)
            text = self.answers.pop(0)(inp) if self.answers else good_text(inp)
            return {"text": text, "model": "fake-claude", "input_tokens": 3000, "output_tokens": 600, "usd": 0.018}
    fake = Fake()
    fake.calls, fake.answers = [], []
    monkeypatch.setattr(prompt_writer, "ASK", fake)
    monkeypatch.setattr(settings, "prompt_writer", "llm")
    return fake


def write_for(sid, engine=None):
    with session_scope() as s:
        plan = scene_parts.plan_part(s, s.get(Scene, sid), engine)
        inp = plan.writer_input
    return prompt_writer.write(inp["_project"], inp), inp


# ---------- biçim doğrulama ----------

def test_a_director_style_prompt_passes_and_its_faults_are_named(ref_defaults, mk_project, s3, llm):
    pid, ids = nora(mk_project)
    with session_scope() as s:
        inp = scene_parts.plan_part(s, s.get(Scene, ids["S001_SH01"])).writer_input
    assert [x["label"] for x in inp["shots"]] == ["Shot 1 (0-3s)", "Shot 2 (3-6s)", "Shot 3 (6-10s)"]
    assert "_project" not in prompt_writer.user_prompt(inp)               # önbellek anahtarı LLM'e gitmez
    good = prompt_writer.finalize(good_text(inp), inp)
    errors, _ = prompt_writer.validate(good, inp)
    assert errors == []
    no_tag = good.replace("Pip Image 2 moves", "Pip moves").replace("the chick Image 2", "the chick")
    assert any("Pip (Image 2) hiçbir çekimde" in e for e in prompt_writer.validate(no_tag, inp)[0])
    wrong_time = good.replace("Shot 2 (3-6s)", "Shot 2 (3-7s)")
    assert any("Shot 2 zamanı planla eşleşmiyor" in e for e in prompt_writer.validate(wrong_time, inp)[0])
    missing_shot = good.replace("Shot 3 (6-10s)", "Then")
    assert any("3 çekim bekleniyordu, 2" in e for e in prompt_writer.validate(missing_shot, inp)[0])
    assert any("Mood" in e for e in prompt_writer.validate(good + " Mood: joy.", inp)[0])
    assert any("olmayan referans: Image 7" in e for e in prompt_writer.validate(good + " Image 7 waves.", inp)[0])
    assert any("uzunluk" in e for e in prompt_writer.validate(good[:500], inp)[0])
    no_view = good.replace("only for the look of the place", "for the place")
    assert any("mekân bakış kuralı" in e for e in prompt_writer.validate(no_view, inp)[0])


def test_the_example_and_system_prompt_use_the_engines_reference_format():
    assert "@Image1" in prompt_writer.system_prompt("seedance25", True) and "[Image" not in \
        prompt_writer.system_prompt("seedance25", True)
    h3 = prompt_writer.system_prompt("h3max", True)
    assert "Image 1" in h3 and "@Image" not in h3 and "Shot 3 (7-10s)" in h3
    assert "Sound:" not in prompt_writer.example("h3max", audio=False)
    assert 1500 <= len(prompt_writer.example("h3max", True)) <= 2600            # örnek ~2.000 karakter


# ---------- yazım, motor biçimi, önbellek ----------

def test_the_writer_converts_references_to_each_engines_format_and_caches(ref_defaults, mk_project, s3, llm):
    pid, ids = nora(mk_project)
    h3, inp = write_for(ids["S001_SH01"])
    assert h3.prompt and "[Image" not in h3.prompt and "Nora Image 1 moves" in h3.prompt and "@Image" not in h3.prompt
    assert (h3.input_tokens, h3.output_tokens, h3.usd, h3.attempts) == (3000, 600, 0.018, 1)
    sd, _ = write_for(ids["S001_SH01"], "seedance25")
    assert "Nora @Image1 moves" in sd.prompt and "Image 1" not in sd.prompt.replace("@Image1", "")
    assert len(llm.calls) == 2 and "@Image1" in llm.calls[1]["system"]
    again, _ = write_for(ids["S001_SH01"])                                   # aynı girdi: çağrı yok
    assert len(llm.calls) == 2 and again.prompt == h3.prompt
    with session_scope() as s:
        s.get(Scene, ids["S001_SH02"]).duration_s = 4.0                          # girdi değişti: yeniden yazılır
    write_for(ids["S001_SH01"])
    assert len(llm.calls) == 3
    with session_scope() as s:
        assert s.get(PromptCache, h3.input_hash).project_id is not None


def test_a_failed_validation_is_retried_once_then_falls_back_to_rules(ref_defaults, mk_project, s3, llm):
    pid, ids = nora(mk_project)
    llm.answers = [lambda inp: "Shot 1 (0-3s): Nora runs. Mood: joy.", good_text]
    ok, _ = write_for(ids["S001_SH01"])
    assert ok.prompt and ok.attempts == 2 and len(llm.calls) == 2
    assert "Your previous answer had these problems" in llm.calls[1]["user"] and "Mood" in llm.calls[1]["user"]

    pid2, ids2 = nora(mk_project)
    llm.answers = [lambda inp: "Nora runs.", lambda inp: "Nora runs again."]
    bad, _ = write_for(ids2["S001_SH01"])
    assert bad.prompt is None and bad.attempts == 2 and bad.errors
    plan = plan_for(ids2["S001_SH01"])
    assert plan["prompt_source"] == "rules" and plan["body"]["prompt"] == plan["prompt_rules"]
    assert any("iki denemede doğrulamadan geçmedi" in w for w in plan["warnings"])
    assert plan["prompt_writer"]["llm"]["ok"] is False


def test_the_plan_uses_the_cached_llm_prompt_and_shows_its_cost(ref_defaults, mk_project, s3, llm):
    pid, ids = nora(mk_project)
    before = plan_for(ids["S001_SH02"])
    assert before["prompt_source"] == "rules" and before["prompt_writer"]["estimate"]["usd"] > 0
    assert any("Ücretsiz mod: LLM istem yazıcı çağrılmaz" in w for w in before["warnings"])
    w, _ = write_for(ids["S001_SH02"])
    plan = plan_for(ids["S001_SH02"])
    assert plan["prompt_source"] == "llm" and plan["prompt_source_label"] == "LLM istemi"
    assert plan["body"]["prompt"] == w.prompt and schema_errors(plan["endpoint"], plan["body"]) == []
    assert plan["prompt_writer"]["llm"]["usd"] == 0.018 and plan["prompt_writer"]["llm"]["output_tokens"] == 600
    assert plan["plan_hash"] != before["plan_hash"]                          # onay yazılan isteme bağlı


def test_free_mode_never_queues_or_calls_the_writer(ref_defaults, mk_project, s3, llm, api):
    pid, ids = nora(mk_project)
    with session_scope() as s:
        plan = scene_parts.plan_part(s, s.get(Scene, ids["S001_SH01"]))
    assert prompt_writer.needed(plan) is False                                # DRY_RUN=1: kilitli
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/prompt/write?dry_run=true")
    assert r.status_code == 200 and r.json()["estimate"]["usd"] > 0 and r.json()["available"] is False
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/prompt/write")
    assert r.status_code == 409 and "Ücretsiz mod" in r.json()["detail"]
    assert llm.calls == []


def test_without_approval_the_prompt_is_written_right_before_generation(ref_defaults, mk_project, fast, llm,
                                                                       monkeypatch):
    from app.engine.iteration import run_tiered_generation
    from app.models import Variation
    monkeypatch.setattr(prompt_writer, "unavailable", lambda: None)          # anahtar varmış gibi (çağrı sahte)
    pid, ids = nora(mk_project, require_plan_approval=False)
    assert run_tiered_generation(pid, ids["S001_SH01"]) == "refined"
    assert len(llm.calls) == 1
    with session_scope() as s:
        v = s.scalar(select(Variation).where(Variation.scene_id == ids["S001_SH01"], Variation.refined_rank == 1))
        assert "Nora Image 1 moves" in v.params["prompt"]


# ---------- elle istem ----------

def test_a_director_prompt_is_sent_verbatim_with_only_the_reference_format_converted(ref_defaults, mk_project, s3,
                                                                                     api):
    pid, ids = nora(mk_project)
    text = ("Golden light. The girl [Image1] scatters grain; the chick @Image2 peeps.\n\nShot 1 (0-3s), wide shot: "
            "the hens Image 3 rush in. Shot 2 (3-6s): Pip [Image2] alone. Shot 3 (6-10s): Nora [Image1] kneels.")
    before = plan_for(ids["S001_SH01"])
    r = api.patch(f"/api/scenes/{ids['S001_SH02']}", json={"prompt_override": text})
    assert r.status_code == 200, r.text and r.json()["prompt_override"]["leader"] == "S001_SH01"
    h3 = plan_for(ids["S001_SH03"])
    assert h3["prompt_source"] == "override" and h3["prompt_source_label"] == "Yönetmen istemi"
    assert h3["body"]["prompt"] == text.replace("[Image1]", "Image 1").replace("@Image2", "Image 2") \
        .replace("[Image2]", "Image 2")
    assert h3["plan_hash"] != before["plan_hash"] and not any("eski olabilir" in w for w in h3["warnings"])
    sd = plan_for(ids["S001_SH01"], "seedance25")
    assert sd["body"]["prompt"] == text.replace("[Image1]", "@Image1").replace("[Image2]", "@Image2") \
        .replace("Image 3", "@Image3")
    with session_scope() as s:                       # görsel özet değişmez: kaydetmek yeniden üretim başlatmaz
        from app.core.hashing import visual_fingerprint
        from app.core.state import load_assets, load_characters
        sc = s.get(Scene, ids["S001_SH01"])
        spec = dict(sc.prompt_spec)
        fp_with = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id))
        sc.prompt_spec = {k: v for k, v in spec.items() if k != "override"}
        assert visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id)) == fp_with
        s.rollback()


def test_a_script_change_keeps_the_director_prompt_but_warns_it_may_be_stale(ref_defaults, mk_project, s3, api):
    pid, ids = nora(mk_project)
    api.patch(f"/api/scenes/{ids['S001_SH01']}", json={"prompt_override": "Shot 1 (0-3s): Nora [Image1] waves."})
    with session_scope() as s:
        s.get(Scene, ids["S001_SH03"]).description = "Nora kneels and Pip hops onto her shoulder."
        sc = s.get(Scene, ids["S001_SH03"])
        sc.prompt_spec = {**(sc.prompt_spec or {}), "prompt": "Nora kneels and Pip hops onto her shoulder."}
    plan = plan_for(ids["S001_SH01"])
    assert plan["prompt_source"] == "override" and plan["body"]["prompt"] == "Shot 1 (0-3s): Nora Image 1 waves."
    assert any("Yönetmen istemi eski olabilir" in w for w in plan["warnings"])
    r = api.patch(f"/api/scenes/{ids['S001_SH01']}", json={"prompt_override": ""})
    assert r.status_code == 200 and r.json()["prompt_override"]["override"] is None
    assert plan_for(ids["S001_SH01"])["prompt_source"] in ("rules", "llm")


# ---------- sade kurallar istemi ----------

def test_the_rules_prompt_tags_references_in_the_action_without_mood_or_constraint_lists(ref_defaults, mk_project,
                                                                                        s3, monkeypatch):
    monkeypatch.setattr(settings, "prompt_writer", "rules")
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH01"])
    prompt = plan["body"]["prompt"]
    assert plan["prompt_source"] == "rules" and not any("LLM" in w for w in plan["warnings"])
    shots = prompt[prompt.index("Shot 1"):]
    assert "Nora (Image 1) scatters" in shots and "Pip (Image 2)" in shots and "hens (Image 3)" in shots
    assert "Mood:" not in prompt and "Visual constraints" not in prompt
    assert prompt.endswith("no other people, animals or figures appear. No subtitles or text on screen.")


def test_the_location_heading_is_not_repeated_in_every_shot(mk_project):
    from app.hermes import rules
    from app.hermes.ingest import ingest
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        sc = s.scalar(select(Scene).where(Scene.project_id == pid, Scene.scene_code == "S001_SH01"))
        assert sc.prompt_spec["prompt"].startswith("TERK EDİLMİŞ DÖKÜMHANE")
        assert scene_parts.shot_text(sc).startswith("Kızıl bir ışık")


def test_a_server_approved_switch_writes_the_llm_prompt_first(ref_defaults, mk_project, fast, api, dispatched, llm,
                                                             monkeypatch):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    monkeypatch.setattr(prompt_writer, "unavailable", lambda: None)
    plan = api.post(f"/api/scenes/{ids['S001_SH01']}/scene-engine?engine=seedance25&dry_run=true").json()
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/scene-engine?engine=seedance25&expected_usd={plan['total_usd']}")
    assert r.status_code == 200, r.text
    assert len(llm.calls) == 1 and "@Image1" in llm.calls[0]["system"]
    after = plan_for(ids["S001_SH01"])
    assert after["prompt_source"] == "llm" and after["approved"] and "Nora @Image1 moves" in after["body"]["prompt"]
