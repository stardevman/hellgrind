"""Kare hızı kilidi (proje ayarı fps_lock).

Kabul:
  - Kilit kapalıyken renk özetleri ve şartname eski hâliyle kalır.
  - Kilit açıkken her çekim renk aşamasında kilitli hıza çevrilir; süre korunur; kare tutma kilitli hız üzerinde yapılır.
  - Teslim şartnamesinin kare hızı kilittir; kilitle çelişen şartname reddedilir.
  - Kilit değişince bitmiş çekimlerde yalnızca renk ve montaj çalışır (görüntü motoru çağrılmaz); film ve kesme listesi
    kilitli hızdadır.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.hashing import film_grade_token, grade_fingerprint
from app.db import session_scope
from app.engine.procedural import render_clip
from app.engine.providers import LocalProvider
from app.hermes import rules
from app.models import Project, Scene
from app.post import delivery, framerate, grade
from tests.media_utils import probe
from tests.scene_utils import scene_row


# ---------- veritabanı gerekmez ----------

def test_lock_off_keeps_old_fingerprints_and_lock_on_changes_them():
    assert film_grade_token(None, {"fps_lock": None}) is None
    assert film_grade_token("lut", {"on_twos": True}) == film_grade_token("lut", {"on_twos": True, "fps_lock": None})
    sc = SimpleNamespace(grade={})
    assert grade_fingerprint(sc, film_grade_token(None, {"fps_lock": "24"})) is not None
    tokens = {film_grade_token(None, {"fps_lock": f}) for f in ("23.976", "24", "25")}
    tokens.add(film_grade_token(None, {"fps_lock": "24", "on_twos": True}))
    assert len(tokens) == 4


def test_lock_of_ignores_unknown_values():
    assert framerate.lock_of({"fps_lock": "25"}) == "25"
    assert framerate.lock_of({"fps_lock": "30"}) is None and framerate.lock_of(None) is None
    assert framerate.matches("24000/1001", "23.976") and not framerate.matches("24/1", "25")


def test_the_delivery_spec_follows_the_lock():
    raw = {"resolution": "1920x1080", "fps": "25"}
    assert delivery.spec_of(SimpleNamespace(delivery=raw, settings={}))["fps"] == "25"
    assert delivery.spec_of(SimpleNamespace(delivery=raw, settings={"fps_lock": "24"}))["fps"] == "24"
    assert delivery.spec_of(SimpleNamespace(delivery=None, settings={"fps_lock": "24"})) is None


@pytest.fixture
def clip16(tmp_path, monkeypatch):
    """Wan gibi 16 fps üreten bir modelin 2 saniyelik çekimi."""
    monkeypatch.setattr("app.config.settings.work_dir", str(tmp_path))
    return render_clip(str(tmp_path / "a.mp4"), 3, 320, 180, 2.0, 16, emotion="joy",
                       camera={"shot": "wide", "movement": "pan"}, characters=["Kerem"])


def test_grading_conforms_a_16_fps_shot_to_the_locked_rate_and_keeps_its_length(clip16, tmp_path):
    for lock, expect in (("24", "24/1"), ("25", "25/1"), ("23.976", "24000/1001")):
        out = grade.grade_video(clip16, str(tmp_path / f"{lock}.mp4"), {}, None, fps=framerate.ffmpeg_rate(lock))
        assert grade.probe_fps(out) == expect
        assert abs(probe(out)["duration"] - 2.0) < 0.1


def test_on_twos_holds_on_the_locked_rate(clip16, tmp_path):
    out = grade.grade_video(clip16, str(tmp_path / "t.mp4"), {}, None, on_twos=True, fps="24")
    assert grade.probe_fps(out) == "24/1" and abs(probe(out)["duration"] - 2.0) < 0.1
    strip = grade.strip_jpeg(clip16, 0.5, str(tmp_path / "s.jpg"), None, None, on_twos=True, fps="24")
    assert strip


# ---------- API ve uçtan uca ----------

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


@pytest.fixture
def produced(api, api_project, fast, eager):
    """Örnek senaryonun yalnızca ilk çekimi üretilir (yerel, ücretsiz)."""
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    assert "VISUAL" in api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()["stages"]
    return pid, ids


def test_locking_the_frame_rate_regrades_without_rendering_and_drives_the_film(api, produced, monkeypatch):
    pid, ids = produced
    sid = ids["S001_SH01"]
    before = scene_row(sid)
    assert before.status == "done", before.last_error
    with session_scope() as s:
        cost = s.get(Project, pid).spent_usd
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)

    r = api.post(f"/api/projects/{pid}/fps-lock", json={"fps": "25"})
    assert r.status_code == 200 and r.json()["regraded"] == ["S001_SH01"], r.text
    after = scene_row(sid)
    assert calls == [] and after.lipsync_video_key == before.lipsync_video_key and after.graded_video_key
    with session_scope() as s:
        p = s.get(Project, pid)
        assert p.spent_usd == cost and p.settings["fps_lock"] == "25"

    st = api.get(f"/api/projects/{pid}/fps-lock").json()
    assert st["fps"] == "25" and st["sources"] and st["sources"][0]["fps"] != "25" and not st["sources"][0]["matches"]

    # şartname kilitle çelişemez; kilit kurulunca var olan şartname kilide çekilir
    assert api.put(f"/api/projects/{pid}/delivery", json={"fps": "24"}).status_code == 409
    assert api.put(f"/api/projects/{pid}/delivery", json={"fps": "25"}).status_code == 200
    assert api.post(f"/api/projects/{pid}/fps-lock", json={"fps": "24"}).status_code == 200
    assert api.get(f"/api/projects/{pid}/delivery").json()["spec"]["fps"] == "24"
    assert api.post(f"/api/projects/{pid}/fps-lock", json={"fps": "30"}).status_code == 422

    r = api.post(f"/api/projects/{pid}/fps-lock", json={"fps": None})       # kapatınca da yalnızca renk + montaj
    assert r.status_code == 200 and scene_row(sid).graded_video_key is None
    assert scene_row(sid).final_video_key == before.final_video_key


def test_the_film_and_cut_list_use_the_locked_rate_without_a_delivery_spec(api, produced):
    from app import storage
    from app.post.timeline import assemble
    pid, _ = produced
    assert api.post(f"/api/projects/{pid}/fps-lock", json={"fps": "25"}).status_code == 200
    out = assemble(pid)
    assert "error" not in out, out
    with session_scope() as s:
        key = s.get(Project, pid).timeline_key
    import json
    import os
    import tempfile
    cut = json.loads(storage.read_bytes(key.rsplit(".", 1)[0] + ".json"))
    assert cut["format"]["timebase"] == 25 and cut["format"]["fps_lock"] == "25"
    with tempfile.TemporaryDirectory() as tmp:
        film = storage.download(key, os.path.join(tmp, "f.mp4"))
        assert grade.probe_fps(film) == "25/1"
    otio = api.get(f"/api/projects/{pid}/export?format=otio").json()
    assert otio["metadata"]["hellgrind"]["fps"] == 25
