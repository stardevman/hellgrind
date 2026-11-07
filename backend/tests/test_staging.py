"""Hareketli animatik (post/staging.py): sahneleme planı, kesik kâğıt, hareketler, kamera, kukla ve animatiğe bağlanması.

Kabul:
  - Beyaz sayfalı ilk kare kesik kâğıda döner ve kendi yerinde durur; sayfa olmayan kare arka plan olur.
  - Kurallar planı kadrajdan boy, kamera hareketinden anahtar kare, duygudan hareket çıkarır.
  - Çizici aynı plandan aynı kareyi verir; hareket kareleri değiştirir; kukla sürümü görünüş taşımaz.
  - Görseli olan çekim animatikte hareketli klip olur, olmayan kart kalır; yönetmenin planı doğrulanır ve kullanılır.
"""
import subprocess
import uuid

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.post import staging


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
