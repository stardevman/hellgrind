"""Hareketli animatik (post/staging.py): sahneleme planı, kesik kâğıt, hareketler, kamera, kukla ve animatiğe bağlanması.

Kabul:
  - Beyaz sayfalı ilk kare kesik kâğıda döner ve kendi yerinde durur; sayfa olmayan kare arka plan olur.
  - Kurallar planı kadrajdan boy, kamera hareketinden anahtar kare, duygudan hareket çıkarır.
  - Çizici aynı plandan aynı kareyi verir; hareket kareleri değiştirir; kukla sürümü görünüş taşımaz.
  - Görseli olan çekim animatikte hareketli klip olur, olmayan kart kalır; yönetmenin planı doğrulanır ve kullanılır.
  - Kukla rehberi açıkken sahnenin isteğine referans videosu olarak girer (şemaya uygun, istemde "Video 1"), bedeli
    referans token'ıyla artar, parçanın her çekiminde açık değilse gönderilmez; üretimde yerel olarak çizilir.
"""
import subprocess
import uuid

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.budget import pricing
from app.config import settings
from app.engine import fal_plan
from app.post import staging
from tests.fal_queue_fake import FakeQueue, validate
from tests.test_reference_mode import nora, plan_for, ref_defaults  # noqa: F401 (fikstür)


def page(path, box=(500, 300, 700, 620), color=(220, 90, 40)):
    im = Image.new("RGB", (1280, 720), (255, 255, 255))
    ImageDraw.Draw(im).ellipse(box, fill=color)
    im.save(path)
    return path


# ---------- saf hesaplar ----------

def test_actions_move_the_figure_and_poses_hold_after_they_end():
    assert staging.motion([], 1.0) == (0.0, 0.0, 1.0, 1.0, 0.0)
    droop = [{"type": "droop", "t0": 0.5, "t1": 1.5, "tilt": -3, "sink": 0.02}]
    end, later = staging.motion(droop, 1.5), staging.motion(droop, 4.0)
    assert end == later and end[4] == pytest.approx(-3) and end[1] == pytest.approx(0.02)
    laugh = [{"type": "laugh", "t0": 0, "t1": 3, "freq": 4, "amp": 0.02}]
    assert any(staging.motion(laugh, t)[1] < -0.005 for t in np.arange(0.3, 1.5, 0.05))


def test_camera_moves_follow_the_shot_camera():
    assert staging.camera_keys("static", 4) == [{"t": 0, "zoom": 1.0, "cx": 0.5, "cy": 0.5}]
    dolly = staging.camera_keys("dolly_in", 4, (0.3, 0.4))
    assert dolly[0]["zoom"] == 1.0 and dolly[-1]["zoom"] > 1.2 and dolly[-1]["cx"] == 0.3 and dolly[-1]["t"] == 4
    pan = staging.camera_keys("pan", 3)
    assert pan[0]["cx"] < pan[-1]["cx"]
    assert staging.keyed(dolly, 2.0, "zoom", 1.0) == pytest.approx(1.15)


def test_a_white_page_becomes_a_cutout_that_stays_in_place(tmp_path):
    im = Image.open(page(tmp_path / "p.png"))
    assert staging.is_page(im)
    cut = staging.cutout(im)
    x0, y0, x1, y1 = cut.info["box"]
    assert abs(x0 - 500) <= 5 and abs(y0 - 300) <= 5 and abs(x1 - 700) <= 5 and abs(y1 - 620) <= 5
    assert cut.getpixel((2, 2))[3] < 40 and cut.getpixel((cut.width // 2, cut.height // 2))[3] == 255
    plan = staging.rules_plan({"duration_s": 3, "camera": {"shot": "wide", "movement": "dolly_in"}, "emotion": "joy",
                               "frame": (1280, 720, True, cut.info["box"])})
    (layer,) = plan["layers"]
    assert plan["background"] == {"kind": "paper"} and layer["image"] == "frame"
    assert layer["x"] == pytest.approx(600 / 1280, abs=0.01) and layer["y"] == pytest.approx(620 / 720, abs=0.01)
    assert layer["actions"][0]["type"] == "laugh" and plan["camera"][-1]["zoom"] > 1


def test_a_painted_frame_is_the_background_and_cast_without_a_frame_is_spread_by_framing():
    dark = Image.new("RGB", (640, 360), (40, 80, 120))
    assert not staging.is_page(dark)
    plate = staging.rules_plan({"duration_s": 4, "camera": {"movement": "pan"}, "frame": (640, 360, False, None)})
    assert plate["background"] == {"kind": "image", "image": "frame"} and plate["layers"] == []
    cast = [{"name": "Ali", "has_image": True}, {"name": "Ela", "has_image": False, "position": "right"}]
    plan = staging.rules_plan({"duration_s": 4, "camera": {"shot": "close_up"}, "emotion": "sadness", "cast": cast,
                               "location": "Ev"})
    ali, ela = plan["layers"]
    assert ali["image"] == "character:Ali" and ela["image"] == "dummy" and ela["x"] == 0.7
    assert ali["h"] == staging.FRAMING["close_up"][0] and plan["background"]["image"] == "location:Ev"
    assert ali["actions"][0]["type"] == "droop"


def test_plans_written_by_hand_are_validated_in_turkish():
    assert staging.validate({"layers": [{"id": "a", "x": 0.5, "actions": [{"type": "breathe"}]}],
                             "camera": [{"t": 0, "zoom": 1.2}]}) == []
    errs = staging.validate({"layers": [{"id": "a", "actions": [{"type": "dance"}]}, {"id": "a"}],
                             "camera": [{"t": 0, "zoom": 0.5}]})
    assert any("bilinmeyen hareket" in e for e in errs) and any("iki kez" in e for e in errs)
    assert any("zoom" in e for e in errs)
    assert staging.validate([]) == ["Plan bir JSON nesnesi olmalı."]


def test_the_renderer_is_deterministic_moves_the_figure_and_the_dummy_hides_the_look(tmp_path):
    images = {"frame": str(page(tmp_path / "p.png"))}
    cut = staging.cutout(Image.open(images["frame"]))
    plan = staging.rules_plan({"duration_s": 2, "camera": {"shot": "wide", "movement": "static"}, "emotion": "surprise",
                               "frame": (1280, 720, True, cut.info["box"])})
    st = staging.Stage(plan, images, 2.0)
    a, b = np.asarray(st.frame(0.0)), np.asarray(st.frame(0.6))
    assert np.array_equal(a, np.asarray(staging.Stage(plan, images, 2.0).frame(0.0)))
    assert not np.array_equal(a, b)                                     # sıçrama: figür yer değiştirdi
    assert tuple(a[5, 640]) == staging.PAPER
    dummy = np.asarray(staging.Stage(plan, images, 2.0, dummy=True).frame(0.0))
    orange = (np.abs(dummy.astype(int) - (220, 90, 40)).sum(2) < 30).sum()
    assert orange == 0 and tuple(dummy[5, 3]) == staging.DUMMY_BG    # görünüş (turuncu) taşınmaz
    painted = tmp_path / "plate.png"
    Image.new("RGB", (640, 360), (40, 80, 120)).save(painted)
    plate = staging.Stage({"background": {"kind": "image", "image": "frame"}}, {"frame": str(painted)}, 1.0, dummy=True)
    px = np.asarray(plate.frame(0.0)).reshape(-1, 3)
    assert not (np.abs(px.astype(int) - (40, 80, 120)).sum(1) < 30).any()          # boyalı kare: yalnızca çizgiler
    out = staging.render(plan, images, 1.0, str(tmp_path / "o.mp4"), captions=[{"t0": 0, "t1": 1, "text": "Merhaba"}])
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                               capture_output=True, text=True, check=True).stdout)
    assert abs(dur - 1.0) < 0.1


# ---------- animatik ve API ----------

def test_a_shot_with_a_frame_is_staged_in_the_animatic_and_the_director_can_edit_its_plan(api, api_project, eager, s3,
                                                                                          tmp_path):
    from sqlalchemy import select

    from app import storage
    from app.db import session_scope
    from app.hermes import rules
    from app.models import Scene
    pid = api_project(1000.0)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    key = f"keyframes/{pid}/{uuid.uuid4().hex}.png"
    storage.put_file(str(page(tmp_path / "k.png")), key, "image/png")
    with session_scope() as s:
        first = s.scalars(select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order)).first()
        first.keyframe_key, first.keyframe_hash, first.emotion = key, "k1", "joy"
        sid, code = str(first.id), first.scene_code

    got = api.get(f"/api/scenes/{sid}/staging").json()
    assert got["source"] == "rules" and "frame" in got["images"] and got["plan"]["layers"][0]["image"] == "frame"

    assert api.post(f"/api/projects/{pid}/animatic").json() == {"queued": True}
    cut = api.get(f"/api/projects/{pid}/overview").json()["animatic"]["cut"]
    kinds = {c["scene_code"]: c["still"] for c in cut["clips"]}
    assert kinds[code] == "motion" and cut["motion"] == 1 and not cut["errors"]
    assert all(v == "card" for c, v in kinds.items() if c != code)

    bad = api.put(f"/api/scenes/{sid}/staging", json={"plan": {"layers": [{"id": "x", "actions": [{"type": "uç"}]}]}})
    assert bad.status_code == 422 and "bilinmeyen hareket" in bad.json()["detail"]
    plan = {**got["plan"], "camera": [{"t": 0, "zoom": 1.0}, {"t": 2, "zoom": 1.5, "cx": 0.45, "cy": 0.5}]}
    put = api.put(f"/api/scenes/{sid}/staging", json={"plan": plan}).json()
    assert put["source"] == "director" and put["plan"]["camera"][-1]["zoom"] == 1.5

    guide = api.post(f"/api/scenes/{sid}/staging/preview?dummy=true").json()
    assert guide["dummy"] and "/guide/" in guide["key"] and storage.exists(guide["key"])
    again = api.post(f"/api/scenes/{sid}/staging/preview?dummy=true").json()
    assert again["key"] == guide["key"]                                 # içerik-adresli: yeniden çizilmez

    assert api.delete(f"/api/scenes/{sid}/staging").json()["source"] == "rules"
    other = next(c for c in cut["clips"] if c["scene_code"] != code)
    assert api.post(f"/api/scenes/{other['scene_id']}/staging/preview").status_code == 409


# ---------- kukla rehberi: video modeline hareket referansı ----------

def test_the_guide_is_a_reference_video_in_the_body_and_costs_reference_tokens():
    kw = {"prompt": "x", "duration_s": 5.0, "resolution_": "480P", "audio_": False, "seed": 1}
    endpoint, body = fal_plan.body("h3max", "ref", video_urls=["https://v/g.mp4"], **kw)
    assert body["reference_video_urls"] == ["https://v/g.mp4"] and validate(endpoint, body) == []
    endpoint, body = fal_plan.body("h3max", "i2v", image_url="https://v/f.png", video_urls=["https://v/g.mp4"], **kw)
    assert "reference_video_urls" not in body and validate(endpoint, body) == []           # image-to-video almaz
    endpoint, body = fal_plan.body("seedance25", "ref", video_urls=["https://v/g.mp4"], **{**kw, "resolution_": "720p"})
    assert body["video_urls"] == ["https://v/g.mp4"] and validate(endpoint, body) == []
    assert pricing.h3max_video_tokens("768P", 5) == 32256 and pricing.h3max_video_tokens("480P", 0) == 0
    assert pricing.h3max_video_tokens("768P", 7.5) == round((32256 + 67536) / 2)
    assert pricing.h3max_video_tokens("768P", 12, out_seconds=5) == 32256               # çıktı süresiyle sınırlı
    plain, guided = fal_plan.cost("h3max", "768P", 5, 2), fal_plan.cost("h3max", "768P", 5, 2, video_s=5)
    assert guided["usd"] - plain["usd"] == pytest.approx((32256 + 2048 - 4096) / 1000 * 0.02 * 1.05, abs=1e-3)
    assert fal_plan.cost("h3max", "768P", 5, 2, "i2v", video_s=5)["usd"] == fal_plan.cost("h3max", "768P", 5, 2, "i2v")["usd"]


def _guide(ids, on=True, codes=None):
    from app.db import session_scope
    from app.models import Scene
    with session_scope() as s:
        for code, sid in ids.items():
            if codes is None or code in codes:
                s.get(Scene, sid).motion_guide = on


def test_the_guide_goes_with_the_whole_scene_only_when_every_shot_has_it(ref_defaults, mk_project, s3, api, tmp_path):
    from app import storage
    from app.core.hashing import visual_fingerprint
    from app.db import session_scope
    from app.models import Scene
    from app.post import animatic
    pid, ids = nora(mk_project)
    before = plan_for(ids["S001_SH01"])
    assert "reference_video_urls" not in before["body"] and "Video 1" not in before["body"]["prompt"]
    _guide(ids, codes={"S001_SH01"})
    half = plan_for(ids["S001_SH01"])
    assert "reference_video_urls" not in half["body"] and any("bütün çekimlerinde açık değil" in w for w in half["warnings"])
    with session_scope() as s:
        sc = s.get(Scene, ids["S001_SH02"])
        fp_off = visual_fingerprint(sc, [], [])
        sc.motion_guide = True
        assert visual_fingerprint(sc, [], []) != fp_off                                # rehber görüntüyü belirler
    _guide(ids)
    plan = plan_for(ids["S001_SH01"])
    body = plan["body"]
    (url,) = body["reference_video_urls"]
    assert url.startswith(f"local:animatic/{pid}/guide/part_") and validate(plan["endpoint"], body) == []
    assert "Video 1 is a motion guide" in body["prompt"] and len(body["reference_image_urls"]) == 4
    assert plan["cost"]["video_tokens"] == pricing.h3max_video_tokens("480P", 10.0)
    assert plan["cost"]["usd"] > before["cost"]["usd"] and plan["plan_hash"] != before["plan_hash"]
    assert any(u["field"] == "reference_video_urls[0]" and "kukla rehberi" in u["note"] for u in plan["uploads"])

    view = api.get(f"/api/scenes/{ids['S001_SH02']}/staging").json()["guide"]
    assert view["on"] and view["seconds"] == 3.0 and view["extra_usd"] > 0
    off = api.put(f"/api/scenes/{ids['S001_SH02']}/staging/guide", json={"on": False}).json()["guide"]
    assert off["on"] is False and "reference_video_urls" not in plan_for(ids["S001_SH01"])["body"]
    assert api.put(f"/api/scenes/{ids['S001_SH02']}/staging/guide", json={"on": True}).json()["guide"]["on"]

    with session_scope() as s:                                                  # üretimde: yerel, ücretsiz çizim
        part = [s.get(Scene, ids[c]) for c in ("S001_SH01", "S001_SH02", "S001_SH03")]
        key = animatic.ensure_part_guide(s, part, "h3max", str(tmp_path))
    assert f"local:{key}" == url and storage.exists(key)
    path = storage.download(key, str(tmp_path / "g.mp4"))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True, check=True).stdout)
    assert abs(dur - 10.0) < 0.2


def test_a_paid_request_uploads_the_guide_as_video_1(ref_defaults, paid, s3, mk_project, monkeypatch, tmp_path, api,
                                                     fast):
    import sys
    import uuid as uuid_

    from app.engine.iteration import run_tiered_generation
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

    def download(url, suffix=".mp4"):
        path = str(tmp_path / f"{uuid_.uuid4().hex}{suffix}")
        render_clip(path, 1, 160, 90, 10.5, 8, emotion="neutral", camera={"shot": "wide"}, characters=["Nora"])
        return path
    monkeypatch.setattr(fal_video, "_download", download)
    pid, ids = nora(mk_project)
    _guide(ids)
    plan = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    assert api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve", json={"plan_hash": plan["plan_hash"]}).status_code == 200
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    (endpoint, body), = [(e, b) for e, b in fake.submits]
    assert endpoint == "minimax/h3-max/reference-to-video" and validate(endpoint, body) == []
    assert len(body["reference_video_urls"]) == 1 and len(body["reference_image_urls"]) == 4
    assert body["reference_video_urls"][0].startswith("https://") and "Video 1 is a motion guide" in body["prompt"]
