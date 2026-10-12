"""Sahne sınırı karma testi bulguları (S001 H3 Max, S002 Seedance taslak, upscale): montaj gerçek segment uzunluğunu
kullanır; senaryoyu yeniden kaydetmek değişmeyen çekimleri sıfırlamaz; seçim asla görüntü üretmez ve stil değişikliği
yalnızca "eski stil" işaretler; otomatik renk eşleme tek turdur ve eksik montaj kendiliğinden üretilmez; "Bu çekimi ayrı
üret" onayı maliyet tahmini verir. Ücretli çağrı yok (ücretsiz mod, yerel render, sahte dağıtım)."""
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import storage
from app.budget import killswitch
from app.db import session_scope
from app.hermes import rules
from app.hermes.ingest import ingest
from app.models import Scene, Variation
from app.post import pipeline
from app.quality.anomaly import probe_duration
from app.style import bible
from app.worker import tasks
from tests.test_mixed_engines import _two_generations
from tests.test_reference_mode import nora, ref_defaults  # noqa: F401 — fikstür


def events(pid, kind=None):
    rows = [json.loads(x) for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    return [e for e in rows if kind is None or e["kind"] == kind]


def produced(mk_project):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    return pid, ids


def winner(sid):
    with session_scope() as s:
        return s.scalar(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1))


# ---------- 1. montaj segmentin gerçek uzunluğunu kullanır ----------

def test_a_reference_segment_keeps_its_real_length_through_lipsync(ref_defaults, mk_project, fast, tmp_path):
    pid, ids = produced(mk_project)
    sid = ids["S001_SH01"]
    real = float(winner(sid).params["duration_s"])
    with session_scope() as s:
        s.get(Scene, sid).duration_s = round(real - 1.0, 2)     # duration_s yalnızca plandır
    key = pipeline.voice_and_lipsync(sid)
    assert key.endswith(".mp4") and "_full" in key
    path = storage.download(key, str(tmp_path / "ls.mp4"))
    assert probe_duration(path) == pytest.approx(real, abs=0.15)          # kırpılmadı (3,917 → 3,00 değil)


def test_only_reference_clips_skip_the_trim():
    assert pipeline.keeps_real_length({"segment": [0.0, 3.917], "duration_s": 3.917})
    assert pipeline.keeps_real_length({"input_mode": "reference"})
    assert pipeline.keeps_real_length({"segment": [3.0, 6.1], "upscaled_from": "x"})       # upscale kopyası da
    assert not pipeline.keeps_real_length({"input_mode": "keyframe"}) and not pipeline.keeps_real_length(None)


# ---------- 2. senaryoyu yeniden kaydetmek ----------

def test_resaving_the_script_keeps_duration_camera_emotion_and_engine_of_unchanged_shots(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    camera = {"shot": "close_up", "movement": "static", "angle": "eye_level", "lens_mm": 85}
    with session_scope() as s:
        rows = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        for code, d in (("S001_SH01", 3.0), ("S001_SH02", 3.0), ("S002_SH01", 4.0)):
            rows[code].duration_s = d
        rows["S001_SH01"].camera, rows["S001_SH01"].emotion, rows["S001_SH01"].engine = camera, "joy", "seedance25"
    result = ingest(pid, rules.SAMPLE_SCRIPT)
    assert sorted(result["unchanged"]) == sorted(result["updated"])
    with session_scope() as s:
        rows = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        assert [rows[c].duration_s for c in ("S001_SH01", "S001_SH02", "S002_SH01")] == [3.0, 3.0, 4.0]
        assert rows["S001_SH01"].camera == camera and rows["S001_SH01"].emotion == "joy"
        assert rows["S001_SH01"].engine == "seedance25"


def test_a_shot_whose_text_changed_is_reparsed(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        for sc in s.scalars(select(Scene).where(Scene.project_id == pid)):
            sc.duration_s = 9.5
    line = "Kızıl bir ışık paslı kazanların arasından süzülüyor."
    assert line in rules.SAMPLE_SCRIPT
    edited = rules.SAMPLE_SCRIPT.replace(line, "Mavi bir ışık paslı kazanların arasından süzülüyor.", 1)
    result = ingest(pid, edited)
    changed = sorted(set(result["updated"]) - set(result["unchanged"]))
    assert len(changed) == 1
    with session_scope() as s:
        rows = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        assert rows[changed[0]].duration_s != 9.5                     # metni değişen çekim yeniden ayrıştırıldı
        assert all(rows[c].duration_s == 9.5 for c in result["unchanged"])


# ---------- 3. seçim asla görüntü üretmez; stil yalnızca işaretler ----------

def _new_style(pid) -> int:
    with session_scope() as s:
        return bible.create(s, pid, lighting_prefix="Late-afternoon golden-hour lighting, low warm sun.",
                            motion_style="", rules={}, negative="", palette=[]).version


def test_the_reported_flow_never_queues_visual_and_style_only_marks_stale(ref_defaults, mk_project, fast, api,
                                                                          dispatched):
    """Stil güncellendi (render:false) → promote?override=true → plan render=false ile onaylandı → promote."""
    pid, ids, g1 = _two_generations(mk_project)
    version = _new_style(pid)
    r = api.post(f"/api/projects/{pid}/style/activate", json={"version": version, "render": False})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["style_stale"]) == sorted(ids)
    overview = {sc["scene_code"]: sc for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert all(overview[c]["style_stale"] for c in ids)

    sid = ids["S001_SH02"]
    r = api.post(f"/api/scenes/{sid}/promote/{g1['S001_SH02']}?override=true")
    assert r.status_code == 200, r.text
    assert "VISUAL" not in r.json()["stages"] and not r.json().get("awaiting_plan")
    with session_scope() as s:
        assert s.get(Scene, sid).status != "plan_review"
    with session_scope() as s:
        s.get(Scene, sid).status = "plan_review"                # raporda: promote sahneyi plan_review'a düşürmüştü
    r = api.post(f"/api/scenes/{sid}/plan/approve?render=false", json={})
    assert r.status_code == 200, r.text
    with session_scope() as s:
        assert s.get(Scene, sid).status == "refined"             # klibi olan çekim bitmiş hâline döner, pending değil
    rank2 = None
    with session_scope() as s:
        rank2 = s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 2))
    target = rank2 or winner(sid).id
    r = api.post(f"/api/scenes/{sid}/promote/{target}")
    assert r.status_code == 200, r.text and "VISUAL" not in r.json()["stages"]
    assert all("VISUAL" not in stages for _, _, stages in dispatched)
    overview = {sc["scene_code"]: sc for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert overview["S001_SH02"]["style_stale"]                     # seçim yeni stil saymaz


def test_bulk_dispatch_leaves_style_stale_shots_and_an_explicit_render_regenerates(ref_defaults, mk_project, fast,
                                                                                   api, dispatched):
    pid, ids = produced(mk_project)
    version = _new_style(pid)
    assert api.post(f"/api/projects/{pid}/style/activate", json={"version": version, "render": False}).status_code == 200
    r = api.post(f"/api/projects/{pid}/render-all")
    assert r.status_code == 200, r.text
    assert all("VISUAL" not in (stages or []) for stages in r.json()["planned"].values())
    assert not r.json()["awaiting_plan"]
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render")            # yönetmenin açık isteği
    assert r.status_code == 200, r.text
    assert "VISUAL" in r.json()["stages"] or r.json().get("awaiting_plan")


def test_going_back_to_the_old_style_clears_the_mark(ref_defaults, mk_project, fast, api):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    old = _new_style(pid)
    assert api.post(f"/api/projects/{pid}/style/activate", json={"version": old, "render": False}).status_code == 200
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    with session_scope() as s:
        new = bible.create(s, pid, lighting_prefix="Cold blue moonlight.", motion_style="", rules={}, negative="",
                           palette=[]).version
    assert sorted(api.post(f"/api/projects/{pid}/style/activate",
                           json={"version": new, "render": False}).json()["style_stale"]) == sorted(ids)
    overview = api.get(f"/api/projects/{pid}/overview").json()["scenes"]
    assert all(sc["style_stale"] == {"version": old} for sc in overview)
    r = api.post(f"/api/projects/{pid}/style/activate", json={"version": old, "render": False})
    assert r.json()["style_stale"] == []
    overview = api.get(f"/api/projects/{pid}/overview").json()["scenes"]
    assert all(sc["style_stale"] is None for sc in overview)


# ---------- 4. renk eşleme tek tur; eksik montaj yok ----------

@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(tasks.assemble_timeline, "delay", lambda *a: calls.append(("assemble", *a)))
    monkeypatch.setattr(tasks.continuity, "delay", lambda *a: calls.append(("continuity", *a)))
    return calls


def _statuses(ids, status="done", **override):
    with session_scope() as s:
        for code, sid in ids.items():
            s.get(Scene, sid).status = override.get(code, status)


def test_auto_match_is_one_round_and_the_film_is_assembled_once_at_the_end(ref_defaults, mk_project, s3, queued,
                                                                         monkeypatch):
    pid, ids = nora(mk_project)
    _statuses(ids)
    from app.api import plan as plan_api
    outcome = {ids["S001_SH01"]: {"stages": ["GRADE", "MUX"]}, ids["S001_SH02"]: {"stages": ["GRADE", "MUX"]},
               ids["S001_SH03"]: HTTPException(409, "S001_SH03 işleniyor (grading); bitince yeniden deneyin.")}

    def fake(s, sc, **kw):
        assert kw.get("post_only") is True                      # eşleme görüntü üretmez
        o = outcome[str(sc.id)]
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(plan_api, "dispatch_result", fake)
    codes = tasks.apply_auto_match(pid, {"changed": list(outcome), "skipped": [], "references": {}})
    assert codes == ["S001_SH01", "S001_SH02"] and tasks.round_active(pid)
    failed = events(pid, "color_auto_failed")
    assert failed and failed[0]["code"] == "S001_SH03" and "işleniyor (grading)" in failed[0]["error"]

    with session_scope() as s:
        s.get(Scene, ids["S001_SH03"]).grade_auto = {"pending": True}
    assert tasks.maybe_rematch(pid) is False                     # tur sürerken yeni tur yok
    assert tasks.round_step(pid, ids["S001_SH01"]) is False
    assert tasks.round_step(pid, ids["S001_SH03"]) is None       # turda değil
    assert tasks.round_step(pid, ids["S001_SH02"]) is True and not tasks.round_active(pid)
    tasks.end_round(pid)
    assert queued == [("assemble", pid, "final", False)]         # bir kez, yeniden eşleme olmadan


def test_the_round_end_assembly_does_not_start_another_round(monkeypatch, queued, mk_project, s3):
    pid = mk_project()
    monkeypatch.setattr("app.post.timeline.assemble", lambda p, k: {"key": "timeline/x.mp4", "clips": 3})
    tasks.assemble_timeline(pid, "final", False)
    assert queued == [("continuity", pid, None, False)]
    tasks.assemble_timeline(pid, "final")                        # elle birleştirme: eşleme turu açılabilir
    assert queued[-1] == ("continuity", pid, None, True)


def test_no_automatic_assembly_while_a_shot_is_not_finished(ref_defaults, mk_project, s3, queued):
    pid, ids = nora(mk_project)
    _statuses(ids, S001_SH03="plan_review")
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).cut_tier = "draft"
    tasks.maybe_assemble_draft(pid, ids["S001_SH01"])
    assert queued == []
    skipped = events(pid, "auto_assemble_skipped")
    assert skipped and skipped[0]["waiting"] == ["S001_SH03"] and skipped[0]["cut"] == "draft"
    _statuses(ids)
    tasks.maybe_assemble_draft(pid, ids["S001_SH01"])
    assert queued == [("assemble", pid, "draft", False)]


def test_a_failed_match_names_its_reason(mk_project, s3):
    assert tasks._failure_reason(HTTPException(409, "S002_SH02 işleniyor")) == "S002_SH02 işleniyor"
    assert tasks._failure_reason(RuntimeError()) == "RuntimeError"
    assert tasks._not_dispatched({"stages": [], "awaiting_plan": True}) == "plan onayı bekliyor"


# ---------- 5. "Bu çekimi ayrı üret" maliyet tahmini ----------

def test_separate_shot_confirmation_has_a_cost_estimate_and_writes_nothing(ref_defaults, mk_project, fast, api,
                                                                          dispatched):
    pid, ids = produced(mk_project)
    sid = ids["S001_SH02"]
    r = api.post(f"/api/scenes/{sid}/shot-engine?engine=seedance25&dry_run=true")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["dry_run"] and plan["total_usd"] > 0 and plan["budget_ok"] and plan["remaining_usd"] > 0
    assert plan["relinked"] == ["S001_SH02", "S001_SH03"] and "S001_SH02" in plan["shots"]
    assert sum(p["usd"] for p in plan["parts"]) == pytest.approx(plan["total_usd"], abs=1e-3)
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.engine == "h3max" and sc.visual_hash is not None                    # hiçbir şey yazılmadı
    assert dispatched == []
    r = api.post(f"/api/scenes/{sid}/shot-engine?engine=seedance25&expected_usd={plan['total_usd'] / 2:.4f}")
    assert r.status_code == 409 and "Tahmin değişti" in r.json()["detail"]
    r = api.post(f"/api/scenes/{sid}/shot-engine?engine=seedance25&expected_usd={plan['total_usd']}")
    assert r.status_code == 200, r.text and r.json()["total_usd"] == plan["total_usd"]
