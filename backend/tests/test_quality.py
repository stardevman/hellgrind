"""Anomali tespiti ve skorlama. Gerçek FFmpeg ile, veritabanı gerektirmez."""
import statistics

import pytest

from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.engine.procedural import render_clip, render_defect
from app.quality import gate
from app.quality.anomaly import detect_anomalies
from tests.media_utils import make_mixed

CFG = dict(DEFAULT_PROJECT_SETTINGS)


def kinds(path):
    return [a["type"] for a in detect_anomalies(str(path))]


# ---------- anomali tespiti ----------

def test_a_healthy_clip_has_no_anomalies(tmp_path):
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12, emotion="tension", characters=["Kerem"])
    assert detect_anomalies(path) == []


def test_black_clip_is_reported_with_its_time_range(tmp_path):
    path = render_defect(str(tmp_path / "b.mp4"), "black", 1, 160, 90, 3.0, 12)
    black = [a for a in detect_anomalies(path) if a["type"] == "black"]
    assert len(black) == 1 and black[0]["start"] == 0 and black[0]["end"] == pytest.approx(3.0, abs=0.2)


def test_frozen_clip_is_reported_and_not_as_black(tmp_path):
    path = render_defect(str(tmp_path / "f.mp4"), "freeze", 3, 160, 90, 3.0, 12)
    assert kinds(path) == ["freeze"]


@pytest.mark.parametrize("seconds,flagged", [(0.3, False), (0.8, True)])
def test_black_section_must_last_half_a_second_to_count(tmp_path, seconds, flagged):
    path = make_mixed(tmp_path / "m.mp4", [("moving", 1.0), ("black", seconds), ("moving", 1.5)])
    assert ("black" in kinds(path)) is flagged


@pytest.mark.parametrize("seconds,flagged", [(1.4, False), (2.6, True)])
def test_frozen_section_must_last_two_seconds_to_count(tmp_path, seconds, flagged):
    path = make_mixed(tmp_path / "m.mp4", [("moving", 1.0), ("flat", seconds), ("moving", 1.5)])
    assert ("freeze" in kinds(path)) is flagged


def test_a_broken_file_is_an_anomaly_not_a_crash(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"bu bir video degil" * 100)
    assert kinds(bad) == ["unreadable"]


def test_a_file_without_video_is_an_anomaly_not_a_crash(tmp_path):
    import subprocess
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(wav)], check=True)
    assert kinds(wav) == ["unreadable"]


# ---------- skor ----------

def test_clip_score_is_repeatable_per_seed_and_differs_between_seeds():
    assert gate.clip_score_dry(42) == gate.clip_score_dry(42)
    assert len({gate.clip_score_dry(s) for s in range(50)}) > 40


def test_clip_scores_follow_the_configured_gaussian_and_stay_within_zero_and_one(monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.89)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.05)
    scores = [gate.clip_score_dry(s) for s in range(3000)]
    assert statistics.mean(scores) == pytest.approx(0.89, abs=0.005)
    assert statistics.pstdev(scores) == pytest.approx(0.05, abs=0.005)
    monkeypatch.setattr(settings, "dry_run_score_mean", 1.2)
    assert max(gate.clip_score_dry(s) for s in range(200)) <= 1.0
    monkeypatch.setattr(settings, "dry_run_score_mean", -0.5)
    assert min(gate.clip_score_dry(s) for s in range(200)) >= 0.0


def test_healthy_clip_above_threshold_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.95)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12)
    score = gate.score_video(path, 1, CFG)
    assert score.passed and score.anomalies == [] and score.clip == score.composite == 0.95 and score.face is None


def test_score_below_threshold_is_a_low_clip_anomaly_and_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.80)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12)
    score = gate.score_video(path, 1, CFG)
    assert not score.passed
    assert score.anomalies == [{"type": "low_clip", "score": 0.8, "threshold": 0.85}]


def test_threshold_is_inclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.85)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12)
    assert gate.score_video(path, 1, CFG).passed


def test_a_great_score_cannot_save_a_black_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.99)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    path = render_defect(str(tmp_path / "b.mp4"), "black", 1, 160, 90, 3.0, 12)
    score = gate.score_video(path, 1, CFG)
    assert not score.passed and score.composite == 0.99 and "black" in [a["type"] for a in score.anomalies]


def test_threshold_comes_from_the_project_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.80)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12)
    assert gate.score_video(path, 1, {**CFG, "clip_threshold": 0.75}).passed


# ---------- ücretli modda sahte skor kullanılmaz ----------

def test_paid_mode_refuses_to_run_without_a_real_scorer(monkeypatch):
    from app.quality import scorer
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(scorer, "unavailable", lambda: "torch paketi kurulu değil")
    with pytest.raises(NotImplementedError, match="kalite skoru kullanılamıyor: torch"):
        gate.ensure_scorer()


def test_paid_mode_never_accepts_the_fake_scorer_even_when_asked(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    with pytest.raises(NotImplementedError, match="Sahte kalite skoru"):
        gate.ensure_scorer()


def test_free_mode_has_a_scorer(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    gate.ensure_scorer()
