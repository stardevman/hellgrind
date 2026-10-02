"""HTTP API: gerçek PostgreSQL, Redis ve MinIO üzerinde, süreç içi TestClient ile.

Celery hiçbir testte gerçek kuyruğa görev göndermez: `dispatched` planı kaydeder, `eager` işi aynı süreçte yürütür.
Servisler yoksa bu dosyadaki testler atlanır.
"""
import uuid

import pytest
from sqlalchemy import text

from app import storage
from app.budget import killswitch as ks
from app.config import DEFAULT_PROJECT_SETTINGS
from app.core.rerender import fingerprints
from app.db import session_scope
from app.engine.iteration import load_characters
from app.hermes.rules import SAMPLE_SCRIPT
from app.models import AudioTrack, Scene
from sqlalchemy import select

ONE_SHOT = "İÇ. ODA - GECE\n\nKerem masaya oturur.\n\nKEREM\nBurada kimse yok mu?\n"
UNKNOWN = str(uuid.uuid4())


def sql(query, **params):
    with session_scope() as s:
        return s.execute(text(query), params).all()


def scene_ids(pid):
    return [str(r[0]) for r in sql("SELECT id FROM scenes WHERE project_id = :p ORDER BY seq_order", p=pid)]


def one_scene(api, api_project, script=ONE_SHOT):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/script", json={"text": script, "auto_render": False}).status_code == 200
    return pid, scene_ids(pid)[0]


def finish(sid):
    """Sahneyi tamamlanmış göster: özetler güncel, ara ve final video anahtarları dolu."""
    with session_scope() as s:
        sc = s.get(Scene, sid)
        tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sid, AudioTrack.is_active)).all()
        fp = fingerprints(sc, load_characters(s, sid), tracks)
        sc.visual_hash, sc.dialogue_hash, sc.bed_hash = fp.visual, fp.dialogue, fp.bed
        sc.lipsync_video_key, sc.final_video_key, sc.status = f"lipsync/{sid}.mp4", f"final/{sid}.mp4", "done"


def add_pool(sid, n=3):
    ids = []
    with session_scope() as s:
        for rank in range(1, n + 1):
            vid = str(uuid.uuid4())
            s.execute(text("""INSERT INTO variations (id, scene_id, tier, engine, seed, prompt_revision, status, video_key,
                                                       refined_rank, composite_score)
                              VALUES (:v, :s, 'full', 'fal_wan', :seed, 0, 'refined', :k, :r, :c)"""),
                      {"v": vid, "s": sid, "seed": rank, "k": f"variations/{sid}/{vid}.mp4", "r": rank, "c": 0.97 - rank / 100})
            ids.append(vid)
    return ids


def ranks(sid):
    return {str(r[0]): r[1] for r in sql("SELECT id, refined_rank FROM variations WHERE scene_id = :s", s=sid)}


# ---------- uygulama ----------

def test_healthz_reports_free_mode(api):
    assert api.get("/healthz").json() == {"ok": True, "dry_run": True}


def test_root_redirects_to_the_panel_and_the_panel_is_served(api):
    r = api.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/panel/"
    page = api.get("/panel/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"] and "Hell Grind" in page.text


def test_meta_exposes_mode_presets_defaults_and_sample_script(api):
    meta = api.get("/api/meta").json()
    assert meta["dry_run"] is True and meta["defaults"] == DEFAULT_PROJECT_SETTINGS and meta["sample_script"] == SAMPLE_SCRIPT
    assert meta["presets"]["preview"] == {"probe_n": 4, "full_n": 8, "wave_size": 4, "max_correction_rounds": 2,
                                          "probe_resolution": "480p", "full_resolution": "720p"}
    assert meta["presets"]["brief"] == {}


# ---------- projeler ----------

def test_create_project_merges_defaults(api, api_project):
    pid = api_project(40, probe_n=3)
    body = api.get(f"/api/projects/{pid}").json()
    assert body["status"] == "draft" and body["budget_limit_usd"] == 40 and body["spent_usd"] == 0 and body["frozen"] is False
    assert body["settings"] == {**DEFAULT_PROJECT_SETTINGS, "probe_n": 3} and body["scene_status_counts"] == {}


def test_unknown_setting_and_non_positive_budget_are_rejected(api):
    assert api.post("/api/projects", json={"title": "pytest-x", "budget_limit_usd": 10, "settings": {"nope": 1}}).status_code == 422
    for limit in (0, -5):
        assert api.post("/api/projects", json={"title": "pytest-x", "budget_limit_usd": limit}).status_code == 422


@pytest.mark.parametrize("setting", [
    {"probe_n": 0}, {"full_n": -1}, {"wave_size": 0}, {"refined_k": 0}, {"max_correction_rounds": -1}, {"probe_n": "abc"},
    {"probe_n": 2.5}, {"clip_threshold": 1.5}, {"excellent_threshold": -0.1}, {"min_pass_rate": 2}, {"probe_resolution": "4k"},
    {"full_resolution": 720}, {"soft_budget_ratio": 0}, {"max_spend_per_10min": 0}, {"loudness_lufs": 5},
])
def test_invalid_setting_values_are_rejected(api, setting):
    """wave_size=0 gibi değerler worker'da sonsuz döngüye ya da anlamsız üretime yol açar."""
    r = api.post("/api/projects", json={"title": "pytest-x", "budget_limit_usd": 10, "settings": setting})
    assert r.status_code == 422, r.text


def test_valid_setting_values_are_accepted(api, api_project):
    api_project(10, probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0, clip_threshold=0.5,
                probe_resolution="480p", full_resolution="720p", soft_budget_ratio=1.0, loudness_lufs=-16)


def test_projects_are_listed_newest_first(api, api_project):
    first, second = api_project(), api_project()
    ids = [p["id"] for p in api.get("/api/projects").json()]
    assert ids.index(second) < ids.index(first)


@pytest.mark.parametrize("path", ["/api/projects/{}", "/api/projects/{}/overview", "/api/projects/{}/budget", "/api/projects/{}/scenes"])
def test_unknown_project_is_404_and_malformed_id_is_422(api, path):
    assert api.get(path.format(UNKNOWN)).status_code == 404
    assert api.get(path.format("bu-bir-uuid-degil")).status_code == 422


def test_characters_are_unique_per_project(api, api_project):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem"}).status_code == 201
    dup = api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem"})
    assert dup.status_code == 409 and "Kerem" in dup.json()["detail"]
    assert api.post(f"/api/projects/{api_project()}/characters", json={"name": "Kerem"}).status_code == 201     # başka projede serbest
    assert api.post(f"/api/projects/{UNKNOWN}/characters", json={"name": "X"}).status_code == 404


def test_scene_creation_links_characters_and_validates(api, api_project):
    pid, other = api_project(), api_project()
    kerem = api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem"}).json()["id"]
    stranger = api.post(f"/api/projects/{other}/characters", json={"name": "Yabancı"}).json()["id"]
    body = {"scene_code": "S001_SH01", "seq_order": 1, "character_ids": [kerem]}

    made = api.post(f"/api/projects/{pid}/scenes", json=body)
    assert made.status_code == 201 and made.json()["scene_code"] == "S001_SH01"
    assert sql("SELECT count(*) FROM scene_characters WHERE scene_id = :s", s=made.json()["id"])[0][0] == 1
    assert api.post(f"/api/projects/{pid}/scenes", json=body).status_code == 409                          # aynı kod
    assert api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "kötü"}).status_code == 422
    assert api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "S001_SH02", "duration_s": 99}).status_code == 422
    assert api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "S001_SH03", "character_ids": [stranger]}).status_code == 422
    assert api.post(f"/api/projects/{pid}/scenes", json={**body, "scene_code": "S001_SH04", "character_ids": [UNKNOWN]}).status_code == 422
    assert [s["scene_code"] for s in api.get(f"/api/projects/{pid}/scenes").json()] == ["S001_SH01"]


# ---------- senaryo ve planlama ----------

def test_script_ingests_and_plans_every_scene_when_asked(api, api_project, dispatched):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/script", json={"text": SAMPLE_SCRIPT})
    body = r.json()
    assert r.status_code == 200 and body["cast"] == ["Kerem", "Ayşe"] and body["shots"] == 4
    assert list(body["planned"]) == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
    assert all(stages == ["VISUAL", "LIPSYNC", "MUX"] for stages in body["planned"].values())
    assert len(dispatched) == 4 and {j[0] for j in dispatched} == {pid}
    assert {r[0] for r in sql("SELECT status::text FROM scenes WHERE project_id = :p", p=pid)} == {"pending"}


def test_script_without_auto_render_only_ingests(api, api_project, dispatched):
    pid = api_project()
    body = api.post(f"/api/projects/{pid}/script", json={"text": ONE_SHOT, "auto_render": False}).json()
    assert body["planned"] == {} and dispatched == [] and body["shots"] == 1


def test_script_validation(api, api_project):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/script", json={"text": "kısa"}).status_code == 422
    assert api.post(f"/api/projects/{UNKNOWN}/script", json={"text": ONE_SHOT}).status_code == 404


def test_render_all_plans_each_scene_and_404s_for_unknown_project(api, api_project, dispatched):
    pid = api_project()
    api.post(f"/api/projects/{pid}/script", json={"text": SAMPLE_SCRIPT, "auto_render": False})
    out = api.post(f"/api/projects/{pid}/render-all").json()
    assert len(out) == 4 and len(dispatched) == 4
    assert api.post(f"/api/projects/{UNKNOWN}/render-all").status_code == 404


def test_render_of_an_up_to_date_scene_does_nothing_unless_forced(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    finish(sid)
    assert api.post(f"/api/scenes/{sid}/render").json() == {"stages": []} and dispatched == []
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").json() == {"stages": ["VISUAL", "LIPSYNC", "MUX"]}
    assert [j[2] for j in dispatched] == [["VISUAL", "LIPSYNC", "MUX"]]


def test_patching_a_visual_input_replans_the_visual_stage(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    finish(sid)
    assert api.patch(f"/api/scenes/{sid}", json={"prompt_spec": {"prompt": "tamamen yeni"}}).json()["ok"] is True
    assert api.post(f"/api/scenes/{sid}/render").json() == {"stages": ["VISUAL", "LIPSYNC", "MUX"]}


def test_scene_get_patch_and_validation(api, api_project):
    pid, sid = one_scene(api, api_project)
    assert api.get(f"/api/scenes/{sid}").json()["scene_code"] == "S001_SH01"
    assert api.patch(f"/api/scenes/{sid}", json={"emotion": "joy", "duration_s": 6}).status_code == 200
    got = api.get(f"/api/scenes/{sid}").json()
    assert (got["emotion"], got["duration_s"]) == ("joy", 6)
    assert api.patch(f"/api/scenes/{sid}", json={"duration_s": 1}).status_code == 422
    assert api.patch(f"/api/scenes/{sid}", json={"emotion": "öfke"}).status_code == 422
    assert api.get(f"/api/scenes/{UNKNOWN}").status_code == 404
    assert api.patch(f"/api/scenes/{UNKNOWN}", json={}).status_code == 404
    assert api.post(f"/api/scenes/{UNKNOWN}/render").status_code == 404
    assert api.get("/api/scenes/xyz").status_code == 422


# ---------- ses ----------

def test_replacing_the_music_replans_only_the_mux_and_versions_the_track(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    finish(sid)
    before = sql("SELECT id, content_hash, version FROM audio_tracks WHERE scene_id = :s AND kind = 'music' AND is_active", s=sid)[0]

    r = api.put(f"/api/scenes/{sid}/audio", json={"kind": "music", "source": "procedural", "params": {"mood": "fear"}, "gain_db": -6})

    assert r.json() == {"version": before.version + 1, "stages": ["MUX"]} and dispatched[-1][2] == ["MUX"]
    rows = sql("SELECT id, is_active, content_hash, gain_db FROM audio_tracks WHERE scene_id = :s AND kind = 'music'", s=sid)
    active = [x for x in rows if x.is_active]
    assert len(rows) == 2 and len(active) == 1 and active[0].id != before.id and active[0].content_hash != before.content_hash
    assert active[0].gain_db == -6


def test_replacing_a_dialogue_line_replans_voice_and_mux_but_not_the_visual(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    finish(sid)
    kerem = sql("SELECT character_id FROM audio_tracks WHERE scene_id = :s AND kind = 'dialogue' AND is_active", s=sid)[0][0]
    r = api.put(f"/api/scenes/{sid}/audio", json={"kind": "dialogue", "source": "dry_run", "text": "Yeni bir söz.",
                                                  "emotion": "joy", "character_id": str(kerem)})
    assert r.json()["stages"] == ["LIPSYNC", "MUX"]


def test_audio_validation(api, api_project):
    pid, sid = one_scene(api, api_project)
    assert api.put(f"/api/scenes/{sid}/audio", json={"kind": "radyo"}).status_code == 422
    assert api.put(f"/api/scenes/{UNKNOWN}/audio", json={"kind": "music"}).status_code == 404


# ---------- Rafine Havuz: küratör seçimi ----------

def test_promote_swaps_ranks_and_replans_only_voice_and_mux(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    first, second, third = add_pool(sid)
    finish(sid)

    r = api.post(f"/api/scenes/{sid}/promote/{third}")

    assert r.json() == {"stages": ["LIPSYNC", "MUX"]} and dispatched[-1][2] == ["LIPSYNC", "MUX"]
    assert ranks(sid) == {third: 1, second: 2, first: 3}
    assert sql("SELECT lipsync_video_key FROM scenes WHERE id = :s", s=sid)[0][0] is None


def test_promoting_the_current_first_is_a_no_op(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    first, second, third = add_pool(sid)
    finish(sid)
    assert api.post(f"/api/scenes/{sid}/promote/{first}").json() == {"stages": []}
    assert ranks(sid) == {first: 1, second: 2, third: 3} and dispatched == []


def test_promote_rejects_variations_outside_the_pool(api, api_project):
    pid, sid = one_scene(api, api_project)
    add_pool(sid, 1)
    outsider = str(uuid.uuid4())
    with session_scope() as s:
        s.execute(text("""INSERT INTO variations (id, scene_id, tier, engine, seed, prompt_revision, status)
                          VALUES (:v, :s, 'probe', 'fal_wan', 9, 0, 'purged')"""), {"v": outsider, "s": sid})
    assert api.post(f"/api/scenes/{sid}/promote/{outsider}").status_code == 404
    assert api.post(f"/api/scenes/{sid}/promote/{UNKNOWN}").status_code == 404
    other_pid, other_sid = one_scene(api, api_project)
    (foreign,) = add_pool(other_sid, 1)
    assert api.post(f"/api/scenes/{sid}/promote/{foreign}").status_code == 404           # başka sahnenin varyasyonu
    assert api.post(f"/api/scenes/{sid}/promote/not-a-uuid").status_code == 422


def test_variations_are_listed_with_scores_and_pool_rank(api, api_project):
    pid, sid = one_scene(api, api_project)
    first, *_ = add_pool(sid, 2)
    listed = api.get(f"/api/scenes/{sid}/variations").json()
    assert [v["refined_rank"] for v in listed] == [1, 2] and listed[0]["id"] == first and listed[0]["tier"] == "full"


# ---------- Kill-Switch ----------

def test_frozen_project_refuses_new_work_with_423_and_changes_nothing(api, api_project, dispatched):
    pid, sid = one_scene(api, api_project)
    first, second, third = add_pool(sid)
    finish(sid)
    music_before = sql("SELECT id FROM audio_tracks WHERE scene_id = :s AND kind = 'music' AND is_active", s=sid)
    api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze"})
    dispatched.clear()

    assert api.post(f"/api/scenes/{sid}/render").status_code == 423
    assert api.post(f"/api/projects/{pid}/script", json={"text": SAMPLE_SCRIPT}).status_code == 423
    assert api.put(f"/api/scenes/{sid}/audio", json={"kind": "music", "params": {"mood": "fear"}}).status_code == 423
    assert api.post(f"/api/scenes/{sid}/promote/{third}").status_code == 423

    assert dispatched == []
    assert ranks(sid) == {first: 1, second: 2, third: 3}                                   # 423 ama durum değişmedi
    assert sql("SELECT id FROM audio_tracks WHERE scene_id = :s AND kind = 'music' AND is_active", s=sid) == music_before
    assert sql("SELECT count(*) FROM scenes WHERE project_id = :p", p=pid)[0][0] == 1       # yeni senaryo işlenmedi


def test_killswitch_freeze_and_resume_roundtrip(api, api_project):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze", "reason": "yönetmen"}).json() == {"frozen": True}
    body = api.get(f"/api/projects/{pid}").json()
    assert body["frozen"] is True and body["status"] == "paused"
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "resume"}).json() == {"frozen": False}
    assert api.get(f"/api/projects/{pid}").json()["status"] == "producing"
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "patla"}).status_code == 422
    assert api.post(f"/api/projects/{UNKNOWN}/killswitch", json={"action": "freeze"}).status_code == 404


def test_budget_endpoint_reports_ledger_and_events(api, api_project):
    pid = api_project(10)
    rid = ks.reserve(pid, 2.0, "fal_wan:720p")
    ks.commit(pid, rid, 2.0, 1.5, "fal_wan:720p")
    ks.reserve(pid, 1.0, "seedance:720p")
    b = api.get(f"/api/projects/{pid}/budget").json()
    assert (b["limit"], b["spent"], b["reserved"], b["remaining"]) == (10, 1.5, 1.0, 7.5)
    assert {(x["provider"], x["kind"], x["count"]) for x in b["ledger"]} == \
        {("fal_wan:720p", "reserve", 1), ("fal_wan:720p", "commit", 1), ("seedance:720p", "reserve", 1)}
    ks.freeze(pid, "deneme")
    b = api.get(f"/api/projects/{pid}/budget").json()
    assert b["frozen"] is True and b["recent_events"][0]["kind"] == "frozen"


# ---------- medya ----------

@pytest.fixture
def objects(s3, tmp_path):
    from tests.media_utils import make_video
    base = f"apitest/{uuid.uuid4()}"
    storage.put_file(str(make_video(tmp_path / "v.mp4", 1)), f"{base}/v.mp4")
    (tmp_path / "t.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    storage.put_file(str(tmp_path / "t.jpg"), f"{base}/t.jpg", "image/jpeg")
    (tmp_path / "c.json").write_text('{"a": 1}')
    storage.put_file(str(tmp_path / "c.json"), f"{base}/c.json", "application/json")
    return base


def test_images_and_json_are_served_directly_with_immutable_caching(api, objects):
    for name, ctype, body in (("t.jpg", "image/jpeg", b"\xff\xd8\xff\xd9"), ("c.json", "application/json", b'{"a": 1}')):
        r = api.get("/api/media", params={"key": f"{objects}/{name}"})
        assert r.status_code == 200 and r.headers["content-type"] == ctype and r.content == body
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_missing_image_is_404(api, s3):
    assert api.get("/api/media", params={"key": f"apitest/{uuid.uuid4()}/yok.jpg"}).status_code == 404


@pytest.mark.parametrize("key", ["../etc/passwd", "a/../b.jpg", "/mutlak/yol.mp4"])
def test_path_tricks_are_rejected(api, key):
    assert api.get("/api/media", params={"key": key}).status_code == 400


def test_video_redirects_to_a_url_signed_for_the_public_host(api, objects):
    """SigV4 imzası Host'a bağlıdır: tarayıcının eriştiği adresle (localhost:9000) imzalanmalı, minio:9000 ile değil."""
    import httpx
    from urllib.parse import urlsplit, urlunsplit

    r = api.get("/api/media", params={"key": f"{objects}/v.mp4"}, follow_redirects=False)
    assert r.status_code == 307
    url = urlsplit(r.headers["location"])
    assert url.netloc == "localhost:9000" and "X-Amz-Signature" in url.query and url.path.endswith(f"{objects}/v.mp4")

    # Konteynerin içinden localhost:9000 yok; aynı imzalı isteği MinIO'ya, Host başlığı korunarak gönder.
    internal = urlunsplit(("http", "minio:9000", url.path, url.query, ""))
    got = httpx.get(internal, headers={"Host": "localhost:9000"})
    assert got.status_code == 200 and got.headers["content-type"] == "video/mp4" and len(got.content) > 500
    tampered = httpx.get(internal.replace("X-Amz-Signature=", "X-Amz-Signature=0"), headers={"Host": "localhost:9000"})
    assert tampered.status_code == 403
    assert httpx.get(internal).status_code == 403                                   # Host'u değişince imza geçersiz
