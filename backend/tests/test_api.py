"""API uç noktaları: süreç içi TestClient, gerçek veritabanı, Redis ve nesne deposu. Celery'ye hiçbir şey gönderilmez."""
import json
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import event, select, text

from app import storage
from app.budget import killswitch as ks
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.db import engine, session_scope
from app.hermes import rules
from app.hermes.ingest import dialogue_hash, music_hash
from app.models import AudioTrack, Scene, Variation
from tests.conftest import BOLUM_A
from tests.media_utils import make_av
from tests.scene_utils import add_scene_music, add_variation, load_scenes, mark_finished, scene_row, track_rows

FULL = ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"]       # kare henüz yok: önce storyboard
CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]


@pytest.fixture
def sample(api, api_project, dispatched):
    """Örnek senaryo yüklenmiş proje (otomatik render kapalı): (proje, {kod: sahne kimliği})."""
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False,
                                                      "auto_music": False})
    assert r.status_code == 200, r.text
    add_scene_music(pid)                        # Bölüm A: her çekimin kendi müziği
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return pid, ids


def freeze(pid):
    ks.freeze(pid, "test")


# ---------- genel ----------

def test_healthz_reports_free_mode(api):
    assert api.get("/healthz").json() == {"ok": True, "dry_run": True}


def test_meta_exposes_presets_defaults_and_the_sample_script(api):
    meta = api.get("/api/meta").json()
    assert meta["dry_run"] is True and meta["sample_script"] == rules.SAMPLE_SCRIPT
    assert meta["presets"]["preview"] == {"probe_n": 4, "full_n": 8, "wave_size": 4, "max_correction_rounds": 2}
    assert meta["presets"]["brief"] == {} and meta["defaults"] == DEFAULT_PROJECT_SETTINGS


def test_root_redirects_to_the_panel(api):
    r = api.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/panel/"


def test_panel_is_served_as_a_static_page(api):
    r = api.get("/panel/")
    # İmajda derlenmiş Next.js paneli (200); derleme yoksa açıklama sayfası (503).
    assert r.status_code in (200, 503) and "text/html" in r.headers["content-type"]
    assert '<html lang="tr"' in r.text


def test_the_next_panel_export_is_served_when_built_and_explains_itself_when_not(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import panel_app

    def client(directory):
        app = FastAPI()
        app.mount("/panel", panel_app(directory))
        return TestClient(app)

    missing = client(tmp_path).get("/panel/")
    assert missing.status_code == 503 and "Panel derlenmemiş" in missing.text and "make web-build" in missing.text
    (tmp_path / "index.html").write_text('<!doctype html><html lang="tr"><body>yeni panel</body></html>')
    (tmp_path / "_next").mkdir()
    (tmp_path / "_next" / "app.js").write_text("console.log(1)")
    built = client(tmp_path)
    page = built.get("/panel/")
    assert page.text.endswith("yeni panel</body></html>") and page.headers["cache-control"] == "no-cache"
    asset = built.get("/panel/_next/app.js")
    assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"        # içerik özetli değil
    (tmp_path / "_next" / "static").mkdir()
    (tmp_path / "_next" / "static" / "a1b2.js").write_text("1")
    assert "immutable" in built.get("/panel/_next/static/a1b2.js").headers["cache-control"]


# ---------- projeler ----------

def test_the_project_returns_its_script_so_the_panel_can_edit_it(api, api_project):
    pid = api_project(25.0)
    assert api.get(f"/api/projects/{pid}").json()["script_source"] is None
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    assert api.get(f"/api/projects/{pid}").json()["script_source"] == rules.SAMPLE_SCRIPT


def test_create_project_returns_defaults_merged_and_starts_as_draft(api, api_project):
    pid = api_project(25.0, probe_n=4)
    body = api.get(f"/api/projects/{pid}").json()
    assert body["status"] == "draft" and body["budget_limit_usd"] == 25.0 and body["spent_usd"] == 0
    assert body["settings"] == {**DEFAULT_PROJECT_SETTINGS, **BOLUM_A, "probe_n": 4}
    assert body["frozen"] is False and body["soft_limited"] is False and body["scene_status_counts"] == {}


def test_project_list_is_newest_first(api, api_project):
    first, second = api_project(), api_project()
    ids = [p["id"] for p in api.get("/api/projects").json()]
    assert ids.index(second) < ids.index(first)


def test_unknown_project_is_404_and_a_malformed_id_is_422(api):
    assert api.get("/api/projects/00000000-0000-0000-0000-000000000000").status_code == 404
    assert api.get("/api/projects/not-a-uuid").status_code == 422


@pytest.mark.parametrize("body", [
    {"title": "", "budget_limit_usd": 10}, {"title": "   ", "budget_limit_usd": 10},
    {"title": "x", "budget_limit_usd": 0}, {"title": "x", "budget_limit_usd": -5},
    {"title": "x", "budget_limit_usd": 1e9}, {"title": "x"},
])
def test_project_basics_are_validated(api, body):
    assert api.post("/api/projects", json=body).status_code == 422


@pytest.mark.parametrize("key,value", [
    ("refined_k", 4), ("refined_k", 0), ("wave_size", 0), ("probe_n", 3.5), ("probe_n", True), ("full_n", 0),
    ("clip_threshold", 1.5), ("min_pass_rate", -0.1), ("soft_budget_ratio", 0), ("loudness_lufs", 0),
    ("loudness_lufs", -80), ("probe_resolution", "4k"), ("max_spend_per_10min", 0), ("max_correction_rounds", 11),
    ("no_such_setting", 1),
])
def test_project_settings_are_validated_with_a_turkish_message(api, key, value):
    r = api.post("/api/projects", json={"title": "x", "budget_limit_usd": 10, "settings": {key: value}})
    assert r.status_code == 422 and key in json.dumps(r.json(), ensure_ascii=False)


def test_settings_at_the_edges_of_their_ranges_are_accepted(api, api_project):
    api_project(10, refined_k=3, wave_size=1, loudness_lufs=-5, max_correction_rounds=0, probe_resolution="1080p")


# ---------- karakter ve sahne ----------

def test_character_is_created_with_its_own_vector_namespace_and_names_are_unique(api, api_project):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem", "lora_url": "https://x/k.safetensors", "lora_scale": 0.8})
    assert r.status_code == 201
    with session_scope() as s:
        row = s.execute(text("SELECT pinecone_namespace, embedding_version, lora_scale FROM characters WHERE id = :i"),
                        {"i": r.json()["id"]}).one()
    assert row[0] == r.json()["id"] and row[1] == 1 and row[2] == pytest.approx(0.8)
    assert api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem"}).status_code == 409
    assert api.post(f"/api/projects/{pid}/characters", json={"name": "Ayşe", "lora_scale": 9}).status_code == 422
    assert api.post("/api/projects/00000000-0000-0000-0000-000000000000/characters", json={"name": "A"}).status_code == 404


def test_scene_is_created_with_a_routed_engine_and_unique_code(api, api_project):
    pid = api_project()
    cid = api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem"}).json()["id"]
    body = {"scene_code": "S001_SH01", "seq_order": 1, "action_complexity": 0.9, "character_ids": [cid]}
    r = api.post(f"/api/projects/{pid}/scenes", json=body)
    assert r.status_code == 201 and r.json()["engine"] == "seedance"
    assert api.post(f"/api/projects/{pid}/scenes", json=body).status_code == 409
    calm = api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "S001_SH02", "action_complexity": 0.2}).json()
    assert calm["engine"] == "fal_wan"
    pinned = api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "S001_SH03", "engine": "higgsfield"}).json()
    assert pinned["engine"] == "higgsfield"
    assert scene_row(r.json()["id"]).status == "pending"


@pytest.mark.parametrize("patch", [
    {"scene_code": "S1"}, {"emotion": "joyful"}, {"duration_s": 1}, {"duration_s": 11}, {"action_complexity": 1.5},
    {"camera": {"shot": "dutch"}}, {"camera": {"movement": "whip"}}, {"camera": {"lens_mm": 1}}, {"seq_order": -1},
])
def test_scene_fields_are_validated(api, api_project, patch):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/scenes", json={"scene_code": "S001_SH01", "seq_order": 1, **patch}).status_code == 422


def test_a_scene_cannot_use_a_character_of_another_project(api, api_project):
    a, b = api_project(), api_project()
    cid = api.post(f"/api/projects/{a}/characters", json={"name": "Kerem"}).json()["id"]
    r = api.post(f"/api/projects/{b}/scenes", json={"scene_code": "S001_SH01", "seq_order": 1, "character_ids": [cid]})
    assert r.status_code == 422


# ---------- senaryo yükleme ----------

def test_script_upload_ingests_and_queues_the_full_chain_for_every_scene(api, api_project, dispatched):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT})
    body = r.json()
    assert r.status_code == 200 and body["cast"] == ["Kerem", "Ayşe"] and body["shots"] == 4 and body["busy"] == []
    assert body["planned"] == {code: FULL for code in CODES}
    assert [(p, stages) for p, _, stages in dispatched] == [(pid, FULL)] * 4


def test_uploading_the_same_script_again_plans_and_queues_nothing(sample, api, dispatched):
    pid, _ = sample
    api.post(f"/api/projects/{pid}/render-all")
    mark_finished(pid)
    dispatched.clear()
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).json()
    assert body["planned"] == {code: [] for code in CODES} and dispatched == []


def test_editing_one_line_queues_only_the_lipsync_and_mux_of_that_scene(sample, api, dispatched):
    pid, ids = sample
    mark_finished(pid)
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT.replace("Burada kimse yok mu?", "Orada kimse yok mu?")}).json()
    assert body["planned"]["S001_SH01"] == ["LIPSYNC", "MUX"]
    assert [v for k, v in body["planned"].items() if k != "S001_SH01"] == [[], [], []]
    assert dispatched == [(pid, ids["S001_SH01"], ["LIPSYNC", "MUX"])]


def test_auto_render_can_be_turned_off(api, api_project, dispatched):
    pid = api_project()
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).json()
    assert body["planned"] == {} and dispatched == []


def test_script_upload_validates_and_refuses_a_frozen_project_before_ingesting(api, api_project, dispatched):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/script", json={"text": "kısa"}).status_code == 422
    assert api.post("/api/projects/00000000-0000-0000-0000-000000000000/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 404
    freeze(pid)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 423
    assert load_scenes(pid) == {} and dispatched == []


def test_a_busy_scene_is_skipped_and_reported_instead_of_failing_the_whole_upload(sample, api, dispatched):
    pid, ids = sample
    with session_scope() as s:
        s.get(Scene, ids["S001_SH01"]).status = "generating"
    ks.register_task(pid, "running-task")
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).json()
    assert body["busy"] == ["S001_SH01"] and body["planned"]["S001_SH01"] == []
    assert body["planned"]["S001_SH02"] == FULL and len(dispatched) == 3


def test_a_project_frozen_while_dispatching_skips_the_rest_instead_of_failing_the_upload(sample, api, dispatched, monkeypatch):
    """Eager/yerel çalışmada ilk sahne bütçeyi bitirebilir: senaryo zaten işlendi, kalanı atlanır ve `frozen` bildirilir."""
    pid, ids = sample
    from app.api import plan
    real = plan.plan_and_dispatch

    def freeze_after_first(s, sc, **kw):
        out = real(s, sc, **kw)
        ks.freeze(pid, "bütçe")
        return out

    monkeypatch.setattr("app.api.plan.plan_and_dispatch", freeze_after_first)
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).json()
    assert body["frozen"] is True and body["planned"]["S001_SH01"] == FULL
    assert [v for k, v in body["planned"].items() if k != "S001_SH01"] == [[], [], []] and len(dispatched) == 1


def test_the_dispatch_lock_is_released_when_the_visual_task_starts(sample, api, dispatched):
    """Kilit yalnızca "kuyruğa verildi, görev başlamadı" aralığı içindir; sahne bitince baştan üret engellenmemeli."""
    from app.worker.dispatch import DISPATCH_LOCK
    pid, ids = sample
    sid = ids["S002_SH01"]
    assert api.post(f"/api/scenes/{sid}/render").status_code == 200
    assert ks.r().exists(DISPATCH_LOCK.format(sid))
    from app.worker import tasks
    tasks.release_dispatch(sid)                       # generate_scene'in ilk işi
    assert not ks.r().exists(DISPATCH_LOCK.format(sid))
    with session_scope() as s:
        s.get(Scene, sid).status = "done"
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").status_code == 200


def test_render_all_queues_every_scene_and_respects_the_kill_switch(sample, api, dispatched):
    pid, _ = sample
    body = api.post(f"/api/projects/{pid}/render-all").json()
    assert body["planned"] == {code: FULL for code in CODES} and len(dispatched) == 4
    dispatched.clear()
    mark_finished(pid)
    assert api.post(f"/api/projects/{pid}/render-all").json()["planned"] == {code: [] for code in CODES}
    freeze(pid)
    assert api.post(f"/api/projects/{pid}/render-all").status_code == 423


# ---------- render ----------

def test_render_queues_the_planned_stages_and_marks_the_scene_pending(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S002_SH01"]
    with session_scope() as s:
        s.get(Scene, sid).last_error = "eski hata"
    assert api.post(f"/api/scenes/{sid}/render").json() == {"stages": FULL}
    assert dispatched == [(pid, sid, FULL)]
    sc = scene_row(sid)
    assert sc.status == "pending" and sc.last_error is None


def test_render_with_nothing_changed_queues_nothing(sample, api, dispatched):
    pid, ids = sample
    mark_finished(pid)
    assert api.post(f"/api/scenes/{ids['S001_SH01']}/render").json() == {"stages": []} and dispatched == []
    assert scene_row(ids["S001_SH01"]).status == "done"


def test_force_visual_regenerates_with_a_new_revision_so_the_seeds_change(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    mark_finished(pid)
    r = api.post(f"/api/scenes/{sid}/render?force_visual=true")
    assert r.json() == {"stages": FULL} and dispatched == [(pid, sid, FULL)]
    assert scene_row(sid).prompt_revision == 1


def test_a_visual_edit_replans_everything(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    mark_finished(pid)
    api.patch(f"/api/scenes/{sid}", json={"camera": {"shot": "close_up", "movement": "dolly_in"}})
    assert api.post(f"/api/scenes/{sid}/render").json() == {"stages": FULL}


def test_render_on_a_frozen_project_is_423_and_changes_nothing(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    freeze(pid)
    before = scene_row(sid)
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").status_code == 423
    after = scene_row(sid)
    assert dispatched == []
    assert (after.status, after.prompt_revision, after.last_error) == (before.status, before.prompt_revision, before.last_error)
    assert after.prompt_revision == 0


def test_render_is_refused_while_the_scene_is_really_being_processed(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    with session_scope() as s:
        s.get(Scene, sid).status = "generating"
    assert api.post(f"/api/scenes/{sid}/render").status_code == 200           # çalışan görev yok: durum bayat, izin verilir
    with session_scope() as s:
        s.get(Scene, sid).status = "generating"
    ks.register_task(pid, "running-task")
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").status_code == 409
    assert scene_row(sid).prompt_revision == 0


def test_a_double_click_cannot_queue_the_visual_stage_twice(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S002_SH01"]
    assert api.post(f"/api/scenes/{sid}/render").status_code == 200
    assert api.post(f"/api/scenes/{sid}/render").status_code == 409
    assert len(dispatched) == 1


def test_render_of_an_unknown_scene_is_404(api):
    assert api.post("/api/scenes/00000000-0000-0000-0000-000000000000/render").status_code == 404


# ---------- sahne okuma ve düzenleme ----------

def test_scene_can_be_read_in_full(sample, api):
    _, ids = sample
    body = api.get(f"/api/scenes/{ids['S002_SH01']}").json()
    assert body["scene_code"] == "S002_SH01" and body["engine"] == "seedance" and body["camera"]["shot"] == "wide"
    assert {"visual_hash", "dialogue_hash", "bed_hash", "mux_hash", "lipsync_video_key", "final_video_key"} <= set(body)


def test_patch_updates_fields_keeps_prompt_extras_and_reroutes_the_engine(sample, api):
    _, ids = sample
    sid = ids["S001_SH01"]
    with session_scope() as s:
        s.execute(text("""UPDATE scenes SET prompt_spec = prompt_spec || '{"loras": ["x"]}'::jsonb WHERE id = :i"""), {"i": sid})
    r = api.patch(f"/api/scenes/{sid}", json={"description": "yeni", "emotion": "fear", "duration_s": 7.5,
                                              "prompt_spec": {"prompt": "yeni istem"}, "action_complexity": 0.9})
    assert r.status_code == 200 and "yeniden üret" in r.json()["hint"]
    sc = scene_row(sid)
    assert (sc.description, sc.emotion, sc.duration_s, sc.engine) == ("yeni", "fear", 7.5, "seedance")
    assert sc.prompt_spec["prompt"] == "yeni istem" and sc.prompt_spec["loras"] == ["x"]


def test_an_explicit_engine_is_kept_when_complexity_changes_with_it(sample, api):
    _, ids = sample
    sid = ids["S001_SH01"]
    api.patch(f"/api/scenes/{sid}", json={"action_complexity": 0.95, "engine": "higgsfield"})
    assert scene_row(sid).engine == "higgsfield"


def test_patch_validates_and_404s(api, sample):
    _, ids = sample
    sid = ids["S001_SH01"]
    assert api.patch(f"/api/scenes/{sid}", json={"emotion": "joyful"}).status_code == 422
    assert api.patch(f"/api/scenes/{sid}", json={"camera": {"shot": "dutch"}}).status_code == 422
    assert api.patch("/api/scenes/00000000-0000-0000-0000-000000000000", json={}).status_code == 404


# ---------- ses ----------

def test_changing_only_the_music_queues_only_the_mux_and_keeps_the_dialogue(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    mark_finished(pid)
    dialogue_before = [t.id for t in track_rows(sid) if t.kind == "dialogue"]
    r = api.put(f"/api/scenes/{sid}/audio", json={"kind": "music", "emotion": "joy", "gain_db": -12})
    assert r.status_code == 200 and r.json()["stages"] == ["MUX"] and r.json()["version"] == 2
    assert dispatched == [(pid, sid, ["MUX"])]
    music = [t for t in track_rows(sid) if t.kind == "music"]
    assert len(music) == 1 and music[0].params == {"mood": "joy"} and music[0].gain_db == -12 and music[0].version == 2
    assert music[0].content_hash == music_hash("joy") and music[0].source == "procedural"
    assert [t.id for t in track_rows(sid) if t.kind == "dialogue"] == dialogue_before
    assert len([t for t in track_rows(sid, active=False) if t.kind == "music"]) == 2          # eski sürüm geçmişte kalır


def test_a_gain_only_change_keeps_the_content_hash(sample, api):
    pid, ids = sample
    sid = ids["S001_SH01"]
    mood = scene_row(sid).emotion
    api.put(f"/api/scenes/{sid}/audio", json={"kind": "music", "emotion": mood, "gain_db": -20})
    old = next(t for t in track_rows(sid, active=False) if t.kind == "music" and not t.is_active)
    new = next(t for t in track_rows(sid) if t.kind == "music")
    assert new.content_hash == old.content_hash and new.gain_db == -20 and old.gain_db == -8


def test_replacing_a_dialogue_line_queues_lipsync_and_mux_and_shares_the_ingest_hash(sample, api, dispatched):
    pid, ids = sample
    sid = ids["S001_SH01"]
    mark_finished(pid)
    old, = [t for t in track_rows(sid) if t.kind == "dialogue"]
    r = api.put(f"/api/scenes/{sid}/audio", json={"kind": "dialogue", "text": "Yeni replik.", "emotion": "fear",
                                                  "character_id": str(old.character_id), "start_offset_s": 0.4,
                                                  "replaces": str(old.id)})
    assert r.json()["stages"] == ["LIPSYNC", "MUX"] and r.json()["version"] == old.version + 1
    new, = [t for t in track_rows(sid) if t.kind == "dialogue"]
    assert new.text == "Yeni replik." and new.source == "tts"
    assert new.content_hash == dialogue_hash(old.character_id, {"line": "Yeni replik.", "emotion": "fear"})
    assert old.id not in [t.id for t in track_rows(sid)]


def test_a_dialogue_without_replaces_adds_a_line(sample, api):
    _, ids = sample
    sid = ids["S001_SH01"]
    api.put(f"/api/scenes/{sid}/audio", json={"kind": "dialogue", "text": "Bir de ben.", "start_offset_s": 3.0})
    assert [t.text for t in track_rows(sid) if t.kind == "dialogue"][-1] == "Bir de ben." and len([t for t in track_rows(sid) if t.kind == "dialogue"]) == 2


def test_uploaded_audio_needs_an_existing_file(sample, api, tmp_path, s3):
    _, ids = sample
    sid = ids["S001_SH01"]
    body = {"kind": "sfx", "source": "upload", "file_key": f"uploads/{sid}/boom.wav"}
    assert api.put(f"/api/scenes/{sid}/audio", json=body).status_code == 422
    storage.put_file(make_av(tmp_path / "x.mp4", freq=300), body["file_key"], "audio/wav")
    assert api.put(f"/api/scenes/{sid}/audio", json=body).status_code == 200
    assert [t.file_key for t in track_rows(sid) if t.kind == "sfx"] == [body["file_key"]]


@pytest.mark.parametrize("body", [
    {"kind": "dialogue"}, {"kind": "dialogue", "text": "  "}, {"kind": "music", "source": "tts"},
    {"kind": "dialogue", "source": "procedural", "text": "x"}, {"kind": "sfx"}, {"kind": "music", "gain_db": 99},
    {"kind": "music", "start_offset_s": -1}, {"kind": "music", "params": {"mood": "funky"}}, {"kind": "violin"},
])
def test_audio_input_is_validated(sample, api, body):
    _, ids = sample
    assert api.put(f"/api/scenes/{ids['S001_SH01']}/audio", json=body).status_code == 422


def test_audio_refuses_a_stranger_character_a_missing_target_and_a_frozen_project(sample, api, api_project):
    pid, ids = sample
    sid = ids["S001_SH01"]
    other = api_project()
    foreign = api.post(f"/api/projects/{other}/characters", json={"name": "Yabancı"}).json()["id"]
    assert api.put(f"/api/scenes/{sid}/audio", json={"kind": "dialogue", "text": "x", "character_id": foreign}).status_code == 422
    assert api.put(f"/api/scenes/{sid}/audio", json={"kind": "dialogue", "text": "x",
                                                     "replaces": "00000000-0000-0000-0000-000000000000"}).status_code == 404
    before = [(t.id, t.is_active) for t in track_rows(sid, active=False)]
    freeze(pid)
    assert api.put(f"/api/scenes/{sid}/audio", json={"kind": "music", "emotion": "joy"}).status_code == 423
    assert [(t.id, t.is_active) for t in track_rows(sid, active=False)] == before             # 423 kanalları değiştirmedi


# ---------- küratör seçimi ----------

@pytest.fixture
def pool(sample):
    pid, ids = sample
    sid = ids["S002_SH01"]
    first, _ = add_variation(sid, 0.95, status="refined", rank=1)
    second, _ = add_variation(sid, 0.93, status="refined", rank=2)
    third, _ = add_variation(sid, 0.91, status="refined", rank=3)
    loser, _ = add_variation(sid, 0.80, status="purged", file=False)
    mark_finished(pid)
    return pid, sid, {"first": first, "second": second, "third": third, "loser": loser}


def ranks(sid):
    with session_scope() as s:
        return {str(v.id): v.refined_rank for v in s.scalars(select(Variation).where(Variation.scene_id == sid))}


def test_promote_swaps_the_ranks_and_queues_only_lipsync_and_mux(pool, api, dispatched):
    pid, sid, v = pool
    r = api.post(f"/api/scenes/{sid}/promote/{v['second']}")
    assert r.status_code == 200 and r.json() == {"stages": ["LIPSYNC", "MUX"]}
    assert ranks(sid)[v["second"]] == 1 and ranks(sid)[v["first"]] == 2 and ranks(sid)[v["third"]] == 3
    assert dispatched == [(pid, sid, ["LIPSYNC", "MUX"])]
    assert scene_row(sid).lipsync_video_key is None and scene_row(sid).visual_hash


def test_promote_from_the_third_place_moves_the_old_winner_to_the_third(pool, api):
    _, sid, v = pool
    api.post(f"/api/scenes/{sid}/promote/{v['third']}")
    assert (ranks(sid)[v["third"]], ranks(sid)[v["first"]], ranks(sid)[v["second"]]) == (1, 3, 2)


def test_promoting_the_current_winner_changes_nothing(pool, api, dispatched):
    _, sid, v = pool
    assert api.post(f"/api/scenes/{sid}/promote/{v['first']}").json() == {"stages": []}
    assert ranks(sid)[v["first"]] == 1 and dispatched == [] and scene_row(sid).lipsync_video_key == "l.mp4"


def test_only_pool_members_of_that_scene_can_be_promoted(pool, api, sample):
    pid, sid, v = pool
    other = sample[1]["S001_SH01"]
    assert api.post(f"/api/scenes/{sid}/promote/{v['loser']}").status_code == 404
    assert api.post(f"/api/scenes/{other}/promote/{v['second']}").status_code == 404
    assert api.post(f"/api/scenes/{sid}/promote/00000000-0000-0000-0000-000000000000").status_code == 404


def test_promote_on_a_frozen_project_is_423_and_keeps_the_order(pool, api):
    pid, sid, v = pool
    freeze(pid)
    assert api.post(f"/api/scenes/{sid}/promote/{v['second']}").status_code == 423
    assert ranks(sid)[v["first"]] == 1 and ranks(sid)[v["second"]] == 2


def test_variations_are_listed_with_their_fate(pool, api):
    _, sid, v = pool
    with session_scope() as s:
        s.execute(text("""UPDATE variations SET params = params || '{"purge_reason": "quality_gate", "quarantine_key": "quarantine/x.mp4"}'::jsonb
                          WHERE id = :i"""), {"i": v["loser"]})
    rows = {r["id"]: r for r in api.get(f"/api/scenes/{sid}/variations").json()}
    assert set(rows) == set(v.values()) and rows[v["first"]]["refined_rank"] == 1
    assert rows[v["loser"]]["purge_reason"] == "quality_gate" and rows[v["loser"]]["quarantine_key"] == "quarantine/x.mp4"


# ---------- bütçe ve Kill-Switch ----------

def test_budget_reports_money_ledger_and_recent_events(sample, api):
    pid, ids = sample
    with ks.spend(pid, 1.0, "fal_wan:720p", scene_id=ids["S001_SH01"]) as charge:
        charge.actual = 0.75
    b = api.get(f"/api/projects/{pid}/budget").json()
    assert b["limit"] == 60.0 and b["spent"] == pytest.approx(0.75) and b["reserved"] == 0 and b["remaining"] == pytest.approx(59.25)
    assert {(e["provider"], e["kind"], e["count"]) for e in b["ledger"]} == {("fal_wan:720p", "reserve", 1), ("fal_wan:720p", "commit", 1)}
    assert b["frozen"] is False and b["soft_limited"] is False and b["recent_events"] == []


def test_killswitch_freezes_and_resumes_through_the_api(sample, api):
    pid, _ = sample
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze", "reason": "deneme"}).json() == {"frozen": True}
    body = api.get(f"/api/projects/{pid}").json()
    assert body["frozen"] is True and body["status"] == "paused"
    assert [e["kind"] for e in api.get(f"/api/projects/{pid}/budget").json()["recent_events"]] == ["frozen"]
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "resume"}).json() == {"frozen": False}
    assert api.get(f"/api/projects/{pid}").json()["status"] == "producing"


def test_killswitch_validates_its_action_and_the_project(api, api_project):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "explode"}).status_code == 422
    assert api.post("/api/projects/00000000-0000-0000-0000-000000000000/killswitch", json={"action": "freeze"}).status_code == 404
    assert api.get("/api/projects/00000000-0000-0000-0000-000000000000/budget").status_code == 404


# ---------- genel görünüm ----------

def test_overview_feeds_the_whole_panel_in_one_call(pool, api):
    pid, sid, v = pool
    with session_scope() as s:
        s.execute(text("UPDATE variations SET keyframe_key = :k WHERE id = :i"), {"k": f"thumbs/{sid}/a.jpg", "i": v["first"]})
        s.execute(text("""UPDATE variations SET params = params || '{"purge_reason": "quality_gate", "quarantine_key": "quarantine/v.mp4"}'::jsonb,
                          anomalies = '[{"type": "black"}, {"type": "low_clip"}, {"type": "black"}]'::jsonb WHERE id = :i"""), {"i": v["loser"]})
    ks.event(pid, "probe_wave", scene_id=sid)
    body = api.get(f"/api/projects/{pid}/overview").json()

    assert body["project"]["id"] == pid and body["project"]["dry_run"] is True and body["project"]["settings"]["probe_n"] == 10
    assert [s["scene_code"] for s in body["scenes"]] == CODES
    scene = next(s for s in body["scenes"] if s["id"] == sid)
    assert scene["status"] == "done" and scene["engine"] == "seedance" and scene["characters"] == ["Ayşe", "Kerem"]
    assert scene["heading"].startswith("DIŞ. ÇÖKEN KÖPRÜ") and scene["final_url"] == "/api/media?key=f.mp4"
    assert {t["kind"] for t in scene["tracks"]} == {"dialogue", "music"}
    assert [t["character"] for t in scene["tracks"] if t["kind"] == "dialogue"] == ["Kerem"] and [t["character"] for t in scene["tracks"] if t["kind"] == "music"] == [None]
    byid = {x["id"]: x for x in scene["variations"]}
    assert byid[v["first"]]["rank"] == 1 and byid[v["first"]]["thumb_url"] == f"/api/media?key=thumbs/{sid}/a.jpg"
    assert byid[v["first"]]["video_url"].startswith("/api/media?key=variations/")
    assert byid[v["loser"]]["reason"] == "quality_gate" and byid[v["loser"]]["anomalies"] == ["black", "low_clip"]
    assert byid[v["loser"]]["video_url"] == "/api/media?key=quarantine/v.mp4"                # karantinadakiler de önizlenir
    assert [e["kind"] for e in body["events"]] == ["probe_wave"] and body["timeline"] is None


def test_overview_includes_the_film_and_its_cut_list(sample, api, tmp_path, s3):
    pid, _ = sample
    key, cut = f"timeline/{pid}/abc.mp4", f"timeline/{pid}/abc.json"
    (tmp_path / "c.json").write_text(json.dumps({"duration_s": 3, "clips": [{"scene_code": "S001_SH01", "in_s": 0, "out_s": 3}]}))
    storage.put_file(make_av(tmp_path / "f.mp4"), key)
    storage.put_file(str(tmp_path / "c.json"), cut, "application/json")
    with session_scope() as s:
        s.execute(text("UPDATE projects SET timeline_key = :k WHERE id = :p"), {"k": key, "p": pid})
    t = api.get(f"/api/projects/{pid}/overview").json()["timeline"]
    assert t["url"] == f"/api/media?key={key}" and t["cut"]["clips"][0]["scene_code"] == "S001_SH01"


def test_overview_of_an_empty_project_and_unknown_project(api, api_project):
    pid = api_project()
    body = api.get(f"/api/projects/{pid}/overview").json()
    assert body["scenes"] == [] and body["events"] == [] and body["timeline"] is None
    assert api.get("/api/projects/00000000-0000-0000-0000-000000000000/overview").status_code == 404


def test_overview_runs_the_same_number_of_queries_for_one_scene_or_many(sample, api, api_project):
    pid, ids = sample
    for sid in ids.values():
        for score in (0.9, 0.8, 0.7):
            add_variation(sid, score, file=False)
    small = api_project()
    one = api.post(f"/api/projects/{small}/scenes", json={"scene_code": "S001_SH01", "seq_order": 1}).json()["id"]
    add_variation(one, 0.9, file=False)

    def count(project):
        statements = []
        listener = lambda conn, cursor, statement, *a: statements.append(statement)   # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            assert api.get(f"/api/projects/{project}/overview").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(statements)

    assert count(pid) == count(small)


# ---------- medya ----------

def put(tmp_path, key, content: bytes, content_type="application/octet-stream"):
    path = tmp_path / "blob"
    path.write_bytes(content)
    storage.put_file(str(path), key, content_type)


def test_small_files_are_served_directly_with_an_immutable_cache(api, tmp_path, s3):
    put(tmp_path, "thumbs/x/a.jpg", b"\xff\xd8\xff jpeg", "image/jpeg")
    put(tmp_path, "timeline/x/h.json", b'{"a": 1}', "application/json")
    img, doc = api.get("/api/media?key=thumbs/x/a.jpg"), api.get("/api/media?key=timeline/x/h.json")
    assert img.status_code == 200 and img.content == b"\xff\xd8\xff jpeg" and img.headers["content-type"] == "image/jpeg"
    assert doc.json() == {"a": 1} and doc.headers["content-type"] == "application/json"
    assert "immutable" in img.headers["cache-control"] and "max-age=31536000" in img.headers["cache-control"]


def test_videos_redirect_to_a_signed_url_for_the_address_the_browser_uses(api, tmp_path, s3):
    put(tmp_path, "final/x/v.mp4", b"0123456789" * 100, "video/mp4")
    r = api.get("/api/media?key=final/x/v.mp4", follow_redirects=False)
    assert r.status_code == 307
    target = urlsplit(r.headers["location"])
    assert f"{target.scheme}://{target.netloc}" == settings.s3_public_endpoint_url and "X-Amz-Signature=" in target.query


def test_the_signed_url_works_for_the_browser_host_and_supports_ranges(api, tmp_path, s3):
    """SigV4 imzası Host'a bağlıdır: tarayıcı localhost:9000'e gider, imza da o adrese göre atılmalı."""
    put(tmp_path, "final/x/v.mp4", b"0123456789" * 100, "video/mp4")
    target = urlsplit(api.get("/api/media?key=final/x/v.mp4", follow_redirects=False).headers["location"])
    inside = f"{settings.s3_endpoint_url}{target.path}?{target.query}"                  # konteynerden MinIO'ya
    browser_host = {"Host": urlsplit(settings.s3_public_endpoint_url).netloc}
    full = httpx.get(inside, headers=browser_host)
    part = httpx.get(inside, headers={**browser_host, "Range": "bytes=10-19"})
    assert full.status_code == 200 and len(full.content) == 1000 and part.status_code == 206 and part.content == b"0123456789"


def test_a_url_signed_for_the_internal_host_is_rejected_for_the_browser_host(tmp_path, s3):
    """Neden ayrı bir "genel" istemci gerekiyor: dahili adresle imzalanan URL tarayıcıda 403 verir."""
    put(tmp_path, "final/x/v.mp4", b"abc", "video/mp4")
    internal = storage.client().generate_presigned_url("get_object", Params={"Bucket": settings.s3_bucket, "Key": "final/x/v.mp4"}, ExpiresIn=60)
    r = httpx.get(internal, headers={"Host": urlsplit(settings.s3_public_endpoint_url).netloc})
    assert r.status_code == 403


@pytest.mark.parametrize("key", ["../etc/passwd", "thumbs/../secret", "/etc/passwd", "thumbs//x.jpg", "bilinmeyen/x.jpg", "thumbs/./x.jpg"])
def test_media_refuses_keys_outside_the_known_prefixes_or_with_path_tricks(api, key):
    assert api.get("/api/media", params={"key": key}).status_code == 400


def test_missing_media_is_404_not_a_dead_redirect(api, s3):
    assert api.get("/api/media?key=thumbs/none/none.jpg").status_code == 404
    assert api.get("/api/media?key=final/none/none.mp4", follow_redirects=False).status_code == 404
    assert api.get("/api/media").status_code == 422


# ---------- film ----------

def test_assemble_queues_the_film_only_when_there_is_something_to_assemble(sample, api, dispatched):
    pid, _ = sample
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 409 and dispatched == []
    mark_finished(pid)
    assert api.post(f"/api/projects/{pid}/assemble").json() == {"queued": True, "scenes": 4}
    assert dispatched == [(pid, None, ["ASSEMBLE"])]


def test_assemble_works_on_a_frozen_project_because_it_costs_nothing(sample, api, dispatched):
    pid, _ = sample
    mark_finished(pid)
    freeze(pid)
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 200
    assert api.post("/api/projects/00000000-0000-0000-0000-000000000000/assemble").status_code == 404
