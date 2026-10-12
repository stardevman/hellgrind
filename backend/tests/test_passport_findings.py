"""Passport Rush taksi testinin bulguları (d7acd6f üzerinde ücretli koşu, 2026-10-12). Ücretli çağrı yok: sahte kuyruk
istemcisi, `no_network`; gövdeler fal OpenAPI tanımıyla doğrulanır."""
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.db import session_scope
from app.engine import fal_plan, scene_parts
from app.models import Asset, Scene, Variation
from tests.fal_queue_fake import FakeQueue, HTTPError, validate
from tests.test_reference_mode import NORA, _winner, nora, plan_for, ref_defaults  # noqa: F401 — fikstür

SEEDANCE_REF = "bytedance/seedance-2.5/reference-to-video"


# ---------- 1) taslakta ses ----------

def test_a_seedance_draft_always_sends_generate_audio_true():
    """fal, draft: true + generate_audio: false için 422 "Invalid parameters" döndü (gövde şemaya uygundu); true geçti."""
    endpoint, body = fal_plan.body("seedance25", "ref", prompt="p", duration_s=10, resolution_="480p", audio_=False,
                                   seed=1, reference_urls=["https://x/a.png"], draft_=True)
    assert body["draft"] is True and body["generate_audio"] is True and validate(endpoint, body) == []
    _, full = fal_plan.body("seedance25", "ref", prompt="p", duration_s=10, resolution_="720p", audio_=False, seed=1)
    assert full["generate_audio"] is False                       # taslak değilse istenen ayar


def test_the_models_audio_is_stripped_when_it_was_not_wanted(tmp_path):
    from app.live.fal_engines import strip_audio
    path = str(tmp_path / "with_audio.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac", path], check=True)
    strip_audio(path)
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                             capture_output=True, text=True).stdout.split()
    assert streams == ["video"]


# ---------- 2) 4xx: yeniden deneme yok ----------

@pytest.fixture
def falq(ref_defaults, paid, s3, monkeypatch, tmp_path, fast):
    from app.engine.procedural import render_clip
    from app.live import fal_queue, fal_video
    from app.quality import gate
    fake = FakeQueue()
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(fal_queue, "POLL_S", 0)
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    monkeypatch.setattr(gate, "ensure_scorer", lambda: None)
    fal_queue._local_uploads.clear()
    for key in fal_queue._r().scan_iter("hg:fal:*"):
        fal_queue._r().delete(key)
    for key in fal_queue._r().scan_iter("hg:part_failed:*"):
        fal_queue._r().delete(key)

    def download(url, suffix=".mp4"):
        path = str(tmp_path / f"{uuid.uuid4().hex}{suffix}")
        render_clip(path, 1, 160, 90, 10.5, 8, emotion="neutral", camera={"shot": "wide"}, characters=["Nora"])
        return path
    monkeypatch.setattr(fal_video, "_download", download)
    return fake


def test_a_422_fails_every_shot_of_the_part_at_once_and_no_sibling_retries(falq, mk_project, api, dispatched):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project, require_plan_approval=False)
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).engine = "seedance25"
    falq.behaviour[SEEDANCE_REF] = [HTTPError(422, [{"msg": "Invalid parameters"}])]
    assert run_tiered_generation(pid, ids["S001_SH01"]) == "failed"
    with session_scope() as s:
        states = {s.get(Scene, sid).status for sid in ids.values()}
        errors = {s.get(Scene, sid).last_error for sid in ids.values()}
    assert states == {"failed"} and len(errors) == 1 and "Invalid parameters" in errors.pop()
    for sid in (ids["S001_SH02"], ids["S001_SH03"]):             # kilidi bekleyen kardeş görevler
        assert run_tiered_generation(pid, sid) == "failed"
    assert len(falq.submits) == 1                                   # hiçbir yeniden gönderim yok
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()   # yönetmen yeniden ister: işaret kalkar
    assert "VISUAL" in r["stages"]
    assert run_tiered_generation(pid, ids["S001_SH01"]) == "refined" and len(falq.submits) == 2
    body = falq.submits[-1][1]
    assert body["draft"] is True and body["generate_audio"] is True and validate(SEEDANCE_REF, body) == []


# ---------- 3) kuru çalıştırma = dağıtım: referans modunda durağan görsel onayı yeter ----------

def test_still_approved_is_enough_in_reference_mode_and_the_dry_run_agrees(ref_defaults, mk_project, s3, api, dispatched):
    pid, ids = nora(mk_project, lint_strictness="block", require_plan_approval=True)   # asset_min_status: locked
    with session_scope() as s:
        s.execute(text("UPDATE assets SET status = 'still_approved' WHERE project_id = :p"), {"p": pid})
    plan = api.get(f"/api/projects/{pid}/render-plan?dry_run=true").json()
    assert all(r["blocked"] is False and r["lint"] == [] for r in plan["scenes"])
    one = plan["scenes"][0]
    ok = api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve?render=true", json={"plan_hash": one["plan_hash"]})
    assert ok.status_code == 200 and all("VISUAL" in d["stages"] for d in ok.json()["dispatched"].values())


def test_a_draft_asset_blocks_both_the_dry_run_and_the_dispatch(ref_defaults, mk_project, s3, api, dispatched):
    pid, ids = nora(mk_project, lint_strictness="block", require_plan_approval=True)
    with session_scope() as s:
        s.execute(text("UPDATE assets SET status = 'draft' WHERE project_id = :p AND name = 'Hens'"), {"p": pid})
    plan = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    assert plan["blocked"] is True and [m["name"] for m in plan["lint"]] == ["Hens"]
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve?render=true", json={"plan_hash": plan["plan_hash"]})
    assert r.status_code == 409 and "Hens" in r.json()["detail"] and dispatched == []


# ---------- 4) tarif kırpılmaz, değişmezler istemde ----------

def test_full_descriptions_and_invariants_reach_the_prompt(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    long = ("Ada is a young flight attendant with a warm, charming face. She wears a fitted navy-blue uniform with a red "
            "silk scarf tied at the neck, a small golden wing pin on the left lapel, and black low heels.")
    with session_scope() as s:
        nora_asset = s.scalars(select(Asset).where(Asset.project_id == pid, Asset.name == "Nora")).one()
        nora_asset.description = long
        nora_asset.invariants = ["red silk scarf at the neck", "golden wing pin on the left lapel"]
    prompt = plan_for(ids["S001_SH01"])["body"]["prompt"]
    assert "red silk scarf tied at the neck, a small golden wing pin on the left lapel, and black low heels" in prompt
    assert "Nora must always have: red silk scarf at the neck; golden wing pin on the left lapel." in prompt


# ---------- 5) yükseltmede bütün parçanın kazananı ----------

def test_a_part_render_updates_every_shot_and_a_shot_without_a_task_gets_its_post_chain(ref_defaults, mk_project, fast,
                                                                                      api, dispatched):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).status = "done"                       # kardeşlerin kendi görevi yok
        s.get(Scene, ids["S001_SH02"]).status = "pending"           # yalnızca SH02 görevi kuyrukta
    assert run_tiered_generation(pid, ids["S001_SH02"]) == "refined"
    overview = {sc["scene_code"]: sc for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert {sc["winner_engine"] for sc in overview.values()} == {"h3max"}
    posted = {sid for _, sid, st in dispatched if set(st) >= {"LIPSYNC", "MUX"}}
    assert posted == {ids["S001_SH01"], ids["S001_SH03"]}            # SH02'nin zinciri kendi görevinde


# ---------- 6) renk eşleme montajdan sonra yeniden ----------

def test_changed_clips_trigger_colour_matching_after_the_last_mux(mk_project, mk_scene, monkeypatch):
    from app.worker import tasks
    pid = mk_project()
    sids = [mk_scene(pid, status="done") for _ in range(3)]
    with session_scope() as s:
        for sid in sids[:2]:
            s.get(Scene, sid).grade_auto = {"pending": True}
        s.get(Scene, sids[2]).status = "muxing"                     # biri hâlâ montajda
    monkeypatch.setattr(settings, "dry_run", False)
    queued = []
    monkeypatch.setattr(tasks.continuity, "delay", lambda p, scene_id=None: queued.append((p, scene_id)))
    assert tasks.maybe_rematch(pid) is False and queued == []
    with session_scope() as s:
        s.get(Scene, sids[2]).status = "done"
    assert tasks.maybe_rematch(pid) is True and queued == [(pid, None)]          # tam denetim: otomatik eşleme
    with session_scope() as s:
        assert all(not (s.get(Scene, sid).grade_auto or {}).get("pending") for sid in sids)


def test_a_new_clip_resets_an_old_auto_match_but_not_a_manual_grade(mk_project, mk_scene):
    from app.engine.finishing import _reset_auto_grade
    pid = mk_project()
    auto, manual = mk_scene(pid), mk_scene(pid)
    with session_scope() as s:
        a = s.get(Scene, auto)
        a.grade, a.grade_auto = {"exposure": 0.3}, {"applied": True, "previous": {"exposure": 0.1}}
        m = s.get(Scene, manual)
        m.grade = {"contrast": 1.2}
        _reset_auto_grade(s, a)
        _reset_auto_grade(s, m)
        assert (a.grade, a.grade_auto) == ({"exposure": 0.1}, {"pending": True})
        assert m.grade == {"contrast": 1.2} and not (m.grade_auto or {}).get("pending")


# ---------- 7) mekân referansının bakış açısı ----------

def test_location_references_give_the_look_not_the_camera_and_height_defaults_to_eye_level(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH01"])
    prompt = plan["body"]["prompt"]
    location = prompt[prompt.index("The location Farmyard"):prompt.index("The cast of this scene")]   # mekân satırı
    assert ("Use Image 4 only for the look of the place (architecture, materials, colors, light); do not copy its "
            "camera position or viewpoint — stage the action as described in each shot.") in location
    assert "camera at human eye level / street level" in prompt
    assert not any("yukarıdan" in w for w in plan["warnings"])
    with session_scope() as s:
        loc = s.scalars(select(Asset).where(Asset.project_id == pid, Asset.type == "location")).one()
        loc.description = "a busy city highway at dusk, aerial view from an overpass"
    plan = plan_for(ids["S001_SH01"])
    assert "Farmyard Behind The Old Red Barn: Bu mekân referansı yukarıdan çekilmiş; sokak seviyesinde bir görsel " \
           "yüklemeyi düşünün." in plan["warnings"]


def test_an_explicit_camera_height_is_not_overridden(ref_defaults, mk_project, s3):
    shots = [(c, d, cast, props, tr, p + (" Low angle from the ground." if c == "S001_SH02" else ""))
             for c, d, cast, props, tr, p in NORA]
    pid, ids = nora(mk_project, shots=shots)
    prompt = plan_for(ids["S001_SH01"])["body"]["prompt"]
    shot2 = prompt[prompt.index("Shot 2"):prompt.index("Shot 3")]
    assert "camera at human eye level" not in shot2
