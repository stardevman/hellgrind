"""2D animasyon dizisi: stil kılavuzunun çizim türü (2D / 3D) ve kare tutma (on twos).

Kabul:
  - Video istemindeki ortam cümlesi kılavuzun çizim türünden gelir; kılavuz yoksa ya da 030 öncesi sürümse eski 3D cümlesi.
  - Yeni stil sürümünün varsayılanı 2D; çizim türü değişikliği yeni bir sürümdür (etki önizlemesi ve onay akışı aynen).
  - Kare tutma renk aşamasında uygulanır: bitmiş çekimlerde yalnızca GRADE + MUX çalışır, görüntü motoru çağrılmaz.
  - FFmpeg kare tutma her çizimi iki kare gösterir; kare sayısı ve süre değişmez.
"""
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from app.core.hashing import film_grade_token, grade_fingerprint
from app.core.rerender import Stage, plan_rerender
from app.db import session_scope
from app.engine import fal_plan
from app.engine.procedural import render_clip
from app.engine.providers import LocalProvider
from app.hermes import rules
from app.models import CurationEvent, Project, Scene
from app.post import grade
from app.style import bible
from app.style.composer import STYLE_TEXT, compose_prompt
from tests.media_utils import probe
from tests.scene_utils import plans, scene_row

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


def _bible(**kw):
    return SimpleNamespace(**{**dict(version=1, lighting_prefix="Golden hour", motion_style="Cartoony motion", rules={},
                                     negative="", palette=[]), **kw})


def _asset(type_, name):
    return SimpleNamespace(id=name, type=type_, name=name, description=None, render_style=None, version=1)


# ---------- istem (veritabanı gerekmez) ----------

def test_the_environment_sentence_follows_the_medium_and_old_versions_stay_3d():
    assert fal_plan.style_line(_bible(medium="2d")) == "Golden hour. Cartoony motion. " + fal_plan.MEDIUM_SENTENCE["2d"]
    assert fal_plan.style_line(_bible(medium="3d")).endswith(fal_plan.STYLE_SENTENCE)
    assert fal_plan.style_line(_bible()).endswith(fal_plan.STYLE_SENTENCE)          # 030 öncesi sürüm: alan yok
    assert fal_plan.style_line(None) == fal_plan.STYLE_SENTENCE                      # kılavuz yok: eski davranış
    assert "3D" not in fal_plan.MEDIUM_SENTENCE["2d"] and "2D" in fal_plan.MEDIUM_SENTENCE["2d"]


def test_a_2d_bible_defaults_characters_and_props_to_cel_lines_and_locations_to_painted_backgrounds():
    two = _bible(medium="2d")
    assert bible.effective_style(_asset("character", "Kerem"), two) == "stylized_2d"
    assert bible.effective_style(_asset("prop", "Halat"), two) == "stylized_2d"
    assert bible.effective_style(_asset("location", "Köprü"), two) == "painterly"
    assert bible.effective_style(_asset("prop", "Halat"), _bible(medium="2d", rules={"prop": "clean_3d"})) == "clean_3d"
    assert bible.effective_style(_asset("prop", "Halat"), _bible(medium="3d")) == "clean_3d"
    assert bible.effective_style(_asset("prop", "Halat"), None) == "clean_3d"       # kılavuz yok: eski davranış
    c = compose_prompt({"prompt": "Köprü sarsılır", "characters": ["Kerem"]},
                       [_asset("character", "Kerem"), _asset("location", "Köprü")], two)
    assert "Character Kerem: " + STYLE_TEXT["stylized_2d"] in c.prompt and STYLE_TEXT["clean_3d"] not in c.prompt


# ---------- kare tutma (ffmpeg, veritabanı gerekmez) ----------

def _frames(path) -> np.ndarray:
    """Bütün kareler, gri, (kare, yükseklik, genişlik)."""
    info = probe(path)["video"][0]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, int(info["height"]), int(info["width"])).astype(float)


def _steps(path) -> list[float]:
    """Ardışık karelerin ortalama mutlak farkı: 0'a yakın = aynı çizim (yalnızca kodlama gürültüsü)."""
    f = _frames(path)
    return [float(np.abs(f[i] - f[i + 1]).mean()) for i in range(len(f) - 1)]


@pytest.fixture
def clip(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.work_dir", str(tmp_path))
    return render_clip(str(tmp_path / "a.mp4"), 3, 320, 180, 1.0, 24, emotion="joy",
                       camera={"shot": "wide", "movement": "pan"}, characters=["Kerem"])


def test_on_twos_holds_every_drawing_for_two_frames_and_keeps_the_timing(clip, tmp_path):
    plain = grade.grade_video(clip, str(tmp_path / "plain.mp4"), {}, None)
    twos = grade.grade_video(clip, str(tmp_path / "twos.mp4"), {}, None, on_twos=True)
    a, b = _steps(plain), _steps(twos)
    assert len(a) == len(b) == 23                                             # 24 kare
    assert min(a) > 1                                                         # kaynak her karede hareket ediyor
    assert max(b[0::2]) < 0.5                                                 # her çizim iki kare (kare 0=1, 2=3 …)
    assert min(b[1:-1:2]) > 1                                                 # çizimler arasında hareket var
    assert abs(probe(twos)["duration"] - probe(plain)["duration"]) < 0.05
    assert grade.probe_fps(twos) == "24/1"


def test_the_strip_shows_six_consecutive_frames_side_by_side(clip, tmp_path):
    from PIL import Image
    out = grade.strip_jpeg(clip, 0.3, str(tmp_path / "s.jpg"), None, None, on_twos=True)
    w, h = Image.open(out).size
    assert w >= 6 * grade.STRIP_W and h < w / 4


def test_on_twos_only_changes_the_grade_fingerprint():
    sc = SimpleNamespace(grade={})
    assert film_grade_token(None, {}) is None and film_grade_token("lut", {"on_twos": False}) == "lut"   # eski özetler aynı
    assert grade_fingerprint(sc, film_grade_token(None, {"on_twos": True})) is not None
    assert film_grade_token("a", {"on_twos": True}) != film_grade_token("b", {"on_twos": True})


# ---------- API ve uçtan uca ----------

def test_new_style_versions_default_to_2d_and_the_switch_goes_through_preview_and_activation(api, api_project):
    pid = api_project(100.0)
    st = api.get(f"/api/projects/{pid}/style").json()
    assert st["medium_default"] == "2d" and st["example"]["medium"] == "2d" and st["defaults"] == bible.DEFAULT_RULES_2D
    assert st["defaults_by_medium"]["3d"] == bible.DEFAULT_RULES and set(st["video_lines"]) == {"2d", "3d"}
    v1 = api.post(f"/api/projects/{pid}/style", json={"lighting_prefix": "Golden hour", "medium": "3d"}).json()
    v2 = api.post(f"/api/projects/{pid}/style", json={"lighting_prefix": "Golden hour"}).json()
    assert (v1["medium"], v2["medium"]) == ("3d", "2d")
    assert v2["video_line"].endswith(fal_plan.MEDIUM_SENTENCE["2d"]) and v2["rules"] == bible.DEFAULT_RULES_2D
    assert api.post(f"/api/projects/{pid}/style", json={"medium": "4d"}).status_code == 422
    assert api.post(f"/api/projects/{pid}/style/activate", json={"version": v1["version"]}).status_code == 200
    assert api.get(f"/api/projects/{pid}/style").json()["active"]["medium"] == "3d"


@pytest.fixture
def produced(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    assert "VISUAL" in api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()["stages"]
    return pid, ids


def test_switching_on_twos_regrades_finished_shots_without_rendering(api, produced, monkeypatch):
    pid, ids = produced
    sid = ids["S001_SH01"]
    before = scene_row(sid)
    with session_scope() as s:
        cost = s.get(Project, pid).spent_usd
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)
    r = api.post(f"/api/projects/{pid}/on-twos", json={"enabled": True})
    assert r.status_code == 200 and r.json()["regraded"] == ["S001_SH01"], r.text      # üretilmemişler beklemez
    after = scene_row(sid)
    assert calls == [] and after.lipsync_video_key == before.lipsync_video_key and after.graded_video_key
    assert after.final_video_key != before.final_video_key and plans(pid)["S001_SH01"] == Stage.NONE
    with session_scope() as s:
        assert s.get(Project, pid).spent_usd == cost and s.get(Project, pid).settings["on_twos"] is True
        actions = [e.action for e in s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid))]
    assert "on_twos_change" in actions
    strip = api.get(f"/api/scenes/{sid}/grade/strip.jpg?twos=true")
    raw = api.get(f"/api/scenes/{sid}/grade/strip.jpg?before=true")
    assert strip.status_code == raw.status_code == 200 and strip.content != raw.content
    assert api.get(f"/api/scenes/{ids['S002_SH01']}/grade/strip.jpg").status_code == 409
    r = api.post(f"/api/projects/{pid}/on-twos", json={"enabled": False})            # kapatınca da yalnızca renk + montaj
    assert r.json()["regraded"] == ["S001_SH01"] and scene_row(sid).graded_video_key is None
    assert scene_row(sid).final_video_key == before.final_video_key                  # içerik-adresli: aynı montaj
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"on_twos": "evet"}}).status_code == 422


def test_plan_rerender_with_on_twos_plans_grade_and_mux_only():
    sc = SimpleNamespace(prompt_spec={"prompt": "p"}, camera={}, engine="fal_wan", duration_s=5.0, grade={}, grade_hash=None,
                         graded_video_key=None, visual_hash=None, dialogue_hash=None, bed_hash=None,
                         lipsync_video_key=None, final_video_key=None, board_key=None, board_hash=None)
    _, fp = plan_rerender(sc, [], [])
    done = SimpleNamespace(**{**vars(sc), "visual_hash": fp.visual, "dialogue_hash": fp.dialogue, "bed_hash": fp.bed,
                              "lipsync_video_key": "l.mp4", "final_video_key": "f.mp4", "board_key": "b",
                              "board_hash": fp.visual})
    assert plan_rerender(done, [], [])[0] == Stage.NONE
    assert plan_rerender(done, [], [], lut_hash=film_grade_token(None, {"on_twos": True}))[0] == Stage.GRADE | Stage.MUX

