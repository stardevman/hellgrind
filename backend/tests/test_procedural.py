"""Yerel prosedürel render motoru: gerçek FFmpeg ile üretilen kliplerin ölçülebilir özellikleri.

Küçük çözünürlük ve kısa süre kullanılır; veritabanı gerektirmez.
"""
import io
import json
import subprocess

import numpy as np
import pytest
from PIL import Image

from app.engine.procedural import EMBER_EMOTIONS, PALETTES, RESOLUTIONS, SHOT_ZOOM, render_clip, render_defect
from app.quality.anomaly import detect_anomalies, probe_duration

MOVEMENTS = ["dolly_in", "dolly_out", "pan", "tilt", "crane", "orbit", "tracking", "handheld"]
EMOTIONS = ["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]


def make(tmp_path, name="c.mp4", **kw):
    args = dict(seed=3, width=160, height=90, duration=1.0, fps=12)
    return render_clip(str(tmp_path / name), **{**args, **kw})


def frame(path, t=0.5):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.int16)


def diff(a, b):
    return float(np.abs(a - b).mean())


def video_stream(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


# ---------- çıktı biçimi ----------

def test_render_clip_is_valid_and_clean(tmp_path):
    out = make(tmp_path, width=320, height=180, duration=2.5, emotion="anger",
               camera={"shot": "medium", "movement": "dolly_in"}, characters=["Kerem", "Ayşe"], action=0.8,
               label="S001_SH01")
    assert abs(probe_duration(out) - 2.5) < 0.2
    assert detect_anomalies(out, freeze_min_s=1.5) == []


def test_stream_matches_requested_format(tmp_path):
    s = video_stream(make(tmp_path, width=320, height=180, duration=2.0, fps=12))
    assert (s["codec_name"], s["pix_fmt"], s["width"], s["height"]) == ("h264", "yuv420p", 320, 180)
    assert s["r_frame_rate"] == "12/1" and s["nb_frames"] == "24"


def test_moov_atom_precedes_media_data_for_streaming(tmp_path):
    data = open(make(tmp_path), "rb").read()
    assert 0 < data.find(b"moov") < data.find(b"mdat")


def test_resolution_table():
    assert RESOLUTIONS["480p"] == (854, 480) and RESOLUTIONS["720p"] == (1280, 720)


# ---------- seed ----------

def test_seed_changes_output(tmp_path):
    a = make(tmp_path, "a.mp4", seed=1, duration=1, fps=6)
    b = make(tmp_path, "b.mp4", seed=2, duration=1, fps=6)
    assert open(a, "rb").read() != open(b, "rb").read()


def test_same_seed_renders_identical_frames(tmp_path):
    kw = dict(emotion="anger", characters=["Ayşe"], camera={"shot": "medium", "movement": "handheld"}, action=0.8)
    a, b = make(tmp_path, "a.mp4", **kw), make(tmp_path, "b.mp4", **kw)
    assert diff(frame(a, 0.3), frame(b, 0.3)) == 0 and diff(frame(a, 0.9), frame(b, 0.9)) == 0


# ---------- duygu paletleri ----------

def test_every_emotion_has_a_four_color_palette():
    assert set(PALETTES) == set(EMOTIONS)
    for colors in PALETTES.values():
        assert len(colors) == 4 and all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in colors)


def test_embers_only_for_anger_tension_and_fear():
    assert EMBER_EMOTIONS == {"anger", "tension", "fear"}


@pytest.mark.parametrize("emotion", EMOTIONS)
def test_every_emotion_renders(tmp_path, emotion):
    out = make(tmp_path, emotion=emotion, characters=["Kerem"])
    assert probe_duration(out) > 0.8


def test_palette_sets_the_mood(tmp_path):
    def red_minus_blue(emotion):
        f = frame(make(tmp_path, f"{emotion}.mp4", emotion=emotion, width=320, height=180))
        return f[..., 0].mean() - f[..., 2].mean()
    assert red_minus_blue("anger") > 20 > -20 > red_minus_blue("sadness")


def test_unknown_emotion_falls_back_to_neutral_palette(tmp_path):
    a = make(tmp_path, "a.mp4", emotion="???")
    b = make(tmp_path, "b.mp4", emotion="neutral")
    assert diff(frame(a), frame(b)) == 0


# ---------- kamera ----------

def test_shot_scale_table_matches_spec():
    assert SHOT_ZOOM["extreme_wide"] == 1.0 and SHOT_ZOOM["extreme_close_up"] == 3.0
    ordered = [SHOT_ZOOM[k] for k in ("extreme_wide", "wide", "medium", "close_up", "extreme_close_up")]
    assert ordered == sorted(ordered) and len(set(ordered)) == 5


def test_closer_shot_changes_framing(tmp_path):
    wide = make(tmp_path, "w.mp4", camera={"shot": "extreme_wide"})
    close = make(tmp_path, "c.mp4", camera={"shot": "close_up"})
    assert diff(frame(wide), frame(close)) > 10


@pytest.mark.parametrize("movement", MOVEMENTS)
def test_every_camera_movement_renders_and_moves_the_picture(tmp_path, movement):
    action = 0.8 if movement == "tracking" else 0.0             # tracking, koşan karakteri izler
    still = make(tmp_path, "still.mp4", camera={"shot": "medium", "movement": "static"})
    moving = make(tmp_path, "moving.mp4", camera={"shot": "medium", "movement": movement}, action=action)
    assert diff(frame(moving), frame(still)) > (0.5 if movement == "handheld" else 5)   # el kamerası yumuşak titreşir


@pytest.mark.parametrize("shot", sorted(SHOT_ZOOM))
@pytest.mark.parametrize("seed", range(1, 9))
def test_no_shot_scale_crashes_the_camera_window(tmp_path, shot, seed):
    """Regresyon: extreme_wide (zoom 1.0) seed sapmasıyla 1'in altına inip PIL'i çökertiyordu."""
    make(tmp_path, seed=seed, width=96, height=54, duration=0.5, fps=8, camera={"shot": shot, "movement": "dolly_out"})


def test_unknown_shot_and_movement_use_defaults(tmp_path):
    out = make(tmp_path, camera={"shot": "zzz", "movement": "zzz"})
    assert probe_duration(out) > 0.8


# ---------- karakterler, etiket, vinyet ----------

def test_characters_are_drawn(tmp_path):
    alone = make(tmp_path, "a.mp4")
    with_one = make(tmp_path, "b.mp4", characters=["Ayşe"])
    assert diff(frame(alone), frame(with_one)) > 5


def test_at_most_four_characters_are_drawn(tmp_path):
    four = make(tmp_path, "a.mp4", characters=list("ABCD"))
    five = make(tmp_path, "b.mp4", characters=list("ABCDE"))
    assert diff(frame(four), frame(five)) == 0


def test_label_is_drawn_bottom_left_only(tmp_path):
    a = frame(make(tmp_path, "a.mp4", label="S001_SH01 close_up"))
    b = frame(make(tmp_path, "b.mp4"))
    assert diff(a[-20:, :80], b[-20:, :80]) > 10
    assert diff(a[:20], b[:20]) < 1


def test_vignette_darkens_the_corners(tmp_path):
    luma = frame(make(tmp_path, emotion="joy", width=320, height=180)).mean(axis=2)
    corners = np.mean([luma[:15, :15].mean(), luma[:15, -15:].mean(), luma[-15:, :15].mean(), luma[-15:, -15:].mean()])
    assert corners < luma[75:105, 140:180].mean() - 2


# ---------- Quality Gate ile uyum ----------

@pytest.mark.parametrize("emotion", ["neutral", "joy", "sadness"])
def test_static_shot_without_characters_is_not_flagged_as_frozen(tmp_path, emotion):
    """Regresyon: Hermes'in "durağan orta plan, karaktersiz" çekimi hiçbir şey hareket etmediği için
    freezedetect'e takılıyor ve tüm varyasyonlar eleniyordu."""
    out = make(tmp_path, width=854, height=480, duration=3.0, fps=24, emotion=emotion,
               camera={"shot": "medium", "movement": "static"}, action=0.3)
    assert detect_anomalies(out) == []


def test_defect_black_is_caught(tmp_path):
    out = render_defect(str(tmp_path / "d.mp4"), kind="black", seed=1, width=160, height=90, duration=2.5)
    assert any(a["type"] == "black" for a in detect_anomalies(out))


def test_defect_freeze_is_caught_and_is_not_black(tmp_path):
    out = render_defect(str(tmp_path / "d.mp4"), kind="freeze", seed=1, width=160, height=90, duration=2.5)
    kinds = {a["type"] for a in detect_anomalies(out)}
    assert "freeze" in kinds and "black" not in kinds


@pytest.mark.parametrize("kind", ["black", "freeze"])
def test_defect_clip_has_requested_size_and_length(tmp_path, kind):
    out = render_defect(str(tmp_path / "d.mp4"), kind=kind, seed=1, width=160, height=90, duration=2.5, fps=12)
    s = video_stream(out)
    assert (s["width"], s["height"]) == (160, 90) and abs(probe_duration(out) - 2.5) < 0.2


# ---------- hata durumu ----------

def test_ffmpeg_failure_raises_readable_error(tmp_path):
    with pytest.raises(RuntimeError, match="ffmpeg"):
        render_clip(str(tmp_path / "yok" / "c.mp4"), seed=1, width=160, height=90, duration=1, fps=6)
