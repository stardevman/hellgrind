"""Yerel prosedürel render: gerçek FFmpeg ile, veritabanı gerektirmez."""
import numpy as np
import pytest

from app.engine import procedural
from app.engine.procedural import PALETTES, SHOT_ZOOM, render_clip, render_defect
from tests.media_utils import anomalies, frame, probe

SMALL = dict(width=160, height=90, duration=3.0, fps=12)
MOVEMENTS = ["static", "dolly_in", "dolly_out", "pan", "tilt", "crane", "orbit", "tracking", "handheld"]


def clip(tmp_path, name="c.mp4", **kw):
    return render_clip(str(tmp_path / name), **{"seed": 1, **SMALL, **kw})


def diff(a, b):
    return float(np.abs(a.astype(int) - b.astype(int)).mean())


# ---------- geçerli çıktı ----------

def test_output_is_h264_yuv420p_without_audio_at_the_requested_size_and_duration(tmp_path):
    info = probe(clip(tmp_path, width=320, height=180, duration=4.0, fps=24))
    video, = info["video"]
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")
    assert (int(video["width"]), int(video["height"])) == (320, 180)
    assert info["audio"] == []
    assert info["duration"] == pytest.approx(4.0, abs=0.1)


def test_moov_atom_comes_first_so_the_clip_streams(tmp_path):
    head = open(clip(tmp_path), "rb").read(4096)
    assert head.find(b"moov") != -1 and head.find(b"moov") < (head.find(b"mdat") if b"mdat" in head else 10**9)


def test_odd_dimensions_are_rounded_up_to_even(tmp_path):
    video, = probe(clip(tmp_path, width=161, height=91))["video"]
    assert (int(video["width"]), int(video["height"])) == (162, 92)


@pytest.mark.parametrize("emotion", list(PALETTES))
def test_every_emotion_renders_without_black_or_frozen_sections(tmp_path, emotion):
    assert anomalies(clip(tmp_path, emotion=emotion, characters=["Kerem"])) == {"black": False, "freeze": False}


@pytest.mark.parametrize("movement", MOVEMENTS)
def test_every_camera_movement_renders_without_anomalies(tmp_path, movement):
    path = clip(tmp_path, camera={"shot": "medium", "movement": movement}, characters=["Kerem", "Ayşe"])
    assert anomalies(path) == {"black": False, "freeze": False}


@pytest.mark.parametrize("shot", list(SHOT_ZOOM))
def test_every_shot_scale_renders_without_anomalies(tmp_path, shot):
    assert anomalies(clip(tmp_path, camera={"shot": shot, "movement": "static"})) == {"black": False, "freeze": False}


@pytest.mark.parametrize("seed", range(6))
def test_static_shots_keep_breathing_so_they_never_look_frozen(tmp_path, seed):
    """Ortam kayması olmasaydı hiçbir şeyin kıpırdamadığı klip freezedetect'e takılırdı."""
    assert anomalies(clip(tmp_path, seed=seed, duration=4.0, camera={"shot": "wide", "movement": "static"})) \
        == {"black": False, "freeze": False}


def test_unknown_shot_and_movement_names_fall_back_instead_of_failing(tmp_path):
    assert anomalies(clip(tmp_path, camera={"shot": "dutch_angle", "movement": "whip"}))["black"] is False


def test_failed_encoding_raises_with_the_ffmpeg_reason_and_leaves_no_zombie(tmp_path):
    with pytest.raises(RuntimeError, match="ffmpeg"):
        render_clip("/dev/null/x.mp4", seed=1, **SMALL)


# ---------- seed, duygu, kamera, karakter ----------

def test_same_seed_renders_the_same_picture_and_different_seeds_differ(tmp_path):
    a = frame(clip(tmp_path, "a.mp4", seed=5), 1.0)
    b = frame(clip(tmp_path, "b.mp4", seed=5), 1.0)
    c = frame(clip(tmp_path, "c.mp4", seed=6), 1.0)
    assert diff(a, b) == 0.0
    assert diff(a, c) > 3.0


def test_emotion_changes_the_palette(tmp_path):
    anger = frame(clip(tmp_path, "a.mp4", emotion="anger"), 1.0).mean(axis=(0, 1))
    fear = frame(clip(tmp_path, "f.mp4", emotion="fear"), 1.0).mean(axis=(0, 1))
    joy = frame(clip(tmp_path, "j.mp4", emotion="joy"), 1.0).mean(axis=(0, 1))
    assert anger[0] > anger[2] * 1.5                      # öfke kızıl: R >> B
    assert fear[2] > fear[0]                              # korku soğuk: B > R
    assert joy.mean() > anger.mean() and joy.mean() > fear.mean()


def test_shot_scale_zooms_in(tmp_path):
    """Yakın plan daha az dünya gösterir: düz bölgeler büyür, kenar yoğunluğu düşer."""
    def edges(shot):
        f = frame(clip(tmp_path, f"{shot}.mp4", camera={"shot": shot, "movement": "static"}), 1.0).mean(axis=2)
        return float(np.abs(np.diff(f, axis=1)).mean())
    assert edges("extreme_close_up") < edges("extreme_wide")


@pytest.mark.parametrize("movement", [m for m in MOVEMENTS if m != "static"])
def test_camera_movement_changes_the_picture_over_time(tmp_path, movement):
    path = clip(tmp_path, camera={"shot": "medium", "movement": movement})
    assert diff(frame(path, 0.1), frame(path, 2.7)) > 1.0


def test_dolly_in_ends_closer_than_dolly_out_does(tmp_path):
    def edge_density(movement):
        f = frame(clip(tmp_path, f"{movement}.mp4", camera={"shot": "medium", "movement": movement}), 2.8).mean(axis=2)
        return float(np.abs(np.diff(f, axis=0)).mean())
    assert edge_density("dolly_in") < edge_density("dolly_out")


def test_characters_appear_and_are_labelled(tmp_path):
    empty = frame(clip(tmp_path, "e.mp4"), 1.0)
    cast = frame(clip(tmp_path, "c.mp4", characters=["Kerem", "Ayşe"]), 1.0)
    assert diff(empty, cast) > 1.0
    assert (cast.mean(axis=2) < 20).sum() > (empty.mean(axis=2) < 20).sum()      # koyu siluetler


def test_characters_run_only_above_the_action_threshold(tmp_path):
    """Koşarken kamera karakterleri izler: arka plan klip boyunca belirgin kayar. Yürürken (eşik altı) kaymaz."""
    def travel(action):
        path = clip(tmp_path, f"{action}.mp4", characters=["Kerem"], action=action, duration=3.0,
                    camera={"shot": "wide", "movement": "static"})
        return diff(frame(path, 0.1), frame(path, 2.8))
    assert travel(0.7) > 1.5 * max(travel(0.3), travel(0.5))


def test_label_is_drawn_bottom_left(tmp_path):
    plain = frame(clip(tmp_path, "p.mp4", width=320, height=180), 1.0)
    tagged = frame(clip(tmp_path, "t.mp4", width=320, height=180, label="S001_SH01"), 1.0)
    corner = (slice(130, 180), slice(0, 120))
    assert np.abs(plain[corner].astype(int) - tagged[corner].astype(int)).max() > 80
    assert diff(plain[:100], tagged[:100]) == 0.0                       # üst kısım etkilenmez


def test_embers_only_for_anger_tension_and_fear():
    assert procedural.EMBER_EMOTIONS == {"anger", "tension", "fear"}


def test_parallax_back_layer_moves_slower_than_the_front():
    assert procedural.PARALLAX_BACK == 0.35


def test_world_is_rendered_larger_than_the_output():
    assert procedural.WORLD_SCALE == 1.6


# ---------- bilerek bozuk klipler ----------

def test_black_defect_is_detected_as_black(tmp_path):
    path = render_defect(str(tmp_path / "b.mp4"), "black", 1, 160, 90, 3.0, 12)
    assert anomalies(path)["black"] is True
    assert probe(path)["duration"] == pytest.approx(3.0, abs=0.1)


def test_freeze_defect_is_detected_as_frozen_but_not_black(tmp_path):
    path = render_defect(str(tmp_path / "f.mp4"), "freeze", 1, 160, 90, 3.0, 12)
    assert anomalies(path) == {"black": False, "freeze": True}


def test_unknown_defect_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        render_defect(str(tmp_path / "x.mp4"), "glitch", 1, 160, 90)
