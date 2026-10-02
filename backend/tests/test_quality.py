"""Quality Gate bileşenleri: gerçek FFmpeg anomali tespiti, skor kapısı, test turu özeti, küçük resim.

Veritabanı ve nesne deposu gerektirmez.
"""
import io
import statistics
import subprocess

import pytest
from PIL import Image

from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.engine import iteration
from app.engine.iteration import VarResult, make_thumb, summarize
from app.engine.procedural import render_clip
from app.quality import gate
from app.quality.anomaly import detect_anomalies, probe_duration

# FFmpeg lavfi: ilk seçenek "=" ile, sonrakiler ":" ile gelir
SOURCES = {"black": "color=c=black:", "still": "color=c=0x606060:", "motion": "testsrc2="}


def build(tmp_path, name, *segments, size="160x90", fps=24):
    """segments: ("black" | "still" | "motion", saniye) ... -> art arda eklenmiş tek klip."""
    args, chains = [], []
    for i, (kind, secs) in enumerate(segments):
        args += ["-f", "lavfi", "-t", str(secs), "-i", f"{SOURCES[kind]}size={size}:rate={fps}"]
        chains.append(f"[{i}:v]format=yuv420p,setsar=1[v{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=0[out]"
    out = str(tmp_path / name)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", out], check=True)
    return out


def types(path):
    return {a["type"] for a in detect_anomalies(path)}


# ---------- anomali tespiti: eşikler spesifikasyondaki gibi (siyah 0,5 sn; donma 2 sn) ----------

def test_moving_content_has_no_anomaly(tmp_path):
    assert detect_anomalies(build(tmp_path, "ok.mp4", ("motion", 3))) == []


def test_black_shorter_than_half_a_second_is_ignored(tmp_path):
    assert "black" not in types(build(tmp_path, "b.mp4", ("motion", 1), ("black", 0.3), ("motion", 1)))


def test_black_longer_than_half_a_second_is_flagged_with_times(tmp_path):
    found = detect_anomalies(build(tmp_path, "b.mp4", ("motion", 1), ("black", 0.8), ("motion", 1)))
    (black,) = [a for a in found if a["type"] == "black"]
    assert 0.8 <= black["start"] <= 1.2 and black["end"] > black["start"]


def test_still_shorter_than_two_seconds_is_ignored(tmp_path):
    assert "freeze" not in types(build(tmp_path, "f.mp4", ("motion", 1), ("still", 1.5), ("motion", 1)))


def test_still_longer_than_two_seconds_is_flagged(tmp_path):
    found = detect_anomalies(build(tmp_path, "f.mp4", ("motion", 1), ("still", 2.6), ("motion", 1)))
    assert [a["type"] for a in found] == ["freeze"] and 0.8 <= found[0]["start"] <= 1.3


def test_probe_duration(tmp_path):
    assert abs(probe_duration(build(tmp_path, "d.mp4", ("motion", 2.5))) - 2.5) < 0.1


# ---------- skor ----------

def test_dry_run_clip_score_is_reproducible_per_seed():
    assert gate.score_clip_dry(1234) == gate.score_clip_dry(1234)
    assert len({gate.score_clip_dry(s) for s in range(50)}) > 40


def test_dry_run_clip_score_follows_the_configured_gaussian():
    scores = [gate.score_clip_dry(s) for s in range(3000)]
    assert abs(statistics.mean(scores) - settings.dry_run_score_mean) < 0.01
    assert abs(statistics.pstdev(scores) - settings.dry_run_score_sd) < 0.01


@pytest.mark.parametrize("mean,expected", [(5.0, 1.0), (-5.0, 0.0)])
def test_dry_run_clip_score_is_clamped_to_unit_interval(monkeypatch, mean, expected):
    monkeypatch.setattr(settings, "dry_run_score_mean", mean)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    assert gate.score_clip_dry(1) == expected


@pytest.fixture
def stub_gate(monkeypatch):
    def configure(clip, anomalies=()):
        monkeypatch.setattr(gate, "score_clip_dry", lambda seed: clip)
        monkeypatch.setattr(gate, "detect_anomalies", lambda path: list(anomalies))
    return configure


def cfg(**over):
    return {**DEFAULT_PROJECT_SETTINGS, **over}


def test_clean_clip_above_threshold_passes(stub_gate):
    stub_gate(0.9)
    score = gate.score_video("x.mp4", 1, cfg())
    assert (score.passed, score.clip, score.composite, score.face, score.anomalies) == (True, 0.9, 0.9, None, [])


def test_clip_below_threshold_is_a_low_clip_anomaly(stub_gate):
    stub_gate(0.8)
    score = gate.score_video("x.mp4", 1, cfg())
    assert not score.passed
    assert score.anomalies == [{"type": "low_clip", "score": 0.8, "threshold": 0.85}]


def test_threshold_boundary_is_inclusive(stub_gate):
    stub_gate(0.85)
    assert gate.score_video("x.mp4", 1, cfg()).passed


def test_threshold_comes_from_project_settings(stub_gate):
    stub_gate(0.9)
    assert not gate.score_video("x.mp4", 1, cfg(clip_threshold=0.95)).passed


def test_video_anomaly_fails_the_gate_even_with_a_perfect_score(stub_gate):
    stub_gate(0.99, [{"type": "black", "start": 0.0, "end": 1.0}])
    score = gate.score_video("x.mp4", 1, cfg())
    assert not score.passed and [a["type"] for a in score.anomalies] == ["black"]


# ---------- test turu özeti ----------

def test_summarize_reports_mean_and_pass_rate():
    rep = summarize([VarResult("a", True, 0.9), VarResult("b", False, 0.8), VarResult("c", False, None)])
    assert rep == {"n": 3, "mean_composite": 0.85, "pass_rate": 0.333}


def test_summarize_edge_cases():
    assert summarize([]) == {"n": 0, "mean_composite": 0.0, "pass_rate": 0.0}
    assert summarize([VarResult("a", False, None)]) == {"n": 1, "mean_composite": 0.0, "pass_rate": 0.0}


# ---------- küçük resim ----------

def test_thumbnail_is_320px_wide_jpeg_taken_from_the_second_second(tmp_path, monkeypatch):
    uploaded = {}

    def fake_put(path, key, content_type="video/mp4"):
        uploaded.update(key=key, type=content_type, image=Image.open(io.BytesIO(open(path, "rb").read())))
        return key

    monkeypatch.setattr(iteration.storage, "put_file", fake_put)
    clip = render_clip(str(tmp_path / "c.mp4"), seed=1, width=640, height=360, duration=3.0, fps=8)

    assert make_thumb(clip, "thumbs/s/v.jpg") == "thumbs/s/v.jpg"
    assert uploaded["type"] == "image/jpeg" and uploaded["image"].format == "JPEG"
    assert uploaded["image"].width == 320
    assert not (tmp_path / "c.jpg").exists()                  # geçici dosya temizlenir


def test_thumbnail_is_skipped_when_clip_is_shorter_than_two_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(iteration.storage, "put_file", lambda *a, **k: pytest.fail("yüklenmemeli"))
    clip = render_clip(str(tmp_path / "c.mp4"), seed=1, width=160, height=90, duration=1.0, fps=8)
    assert make_thumb(clip, "thumbs/s/v.jpg") is None
