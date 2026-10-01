"""FFmpeg gerektirir (Docker imajında var)."""
import os
import subprocess

import pytest

from app.post.audio import synthesize_dry
from app.post.mux import AudioInput, mux
from app.quality.anomaly import detect_anomalies, probe_duration
from scripts.make_samples import make


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    make(str(d), duration=3.0)
    return d


def test_good_sample_has_no_anomalies(samples):
    assert detect_anomalies(str(samples / "sample_00.mp4")) == []


def test_black_sample_detected(samples):
    assert any(a["type"] == "black" for a in detect_anomalies(str(samples / "sample_06.mp4")))


def test_frozen_sample_detected(samples):
    assert any(a["type"] == "freeze" for a in detect_anomalies(str(samples / "sample_07.mp4")))


def test_mux_dialogue_and_music(samples, tmp_path):
    dlg = str(tmp_path / "d.wav")
    synthesize_dry("Dayan bırakma elimi", dlg)
    out = mux(str(samples / "sample_00.mp4"), [AudioInput(dlg, 0.5)],
              [AudioInput(str(samples / "music.wav"), 0.0, -6)], str(tmp_path / "out.mp4"))
    assert abs(probe_duration(out) - 3.0) < 0.2
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", out],
                             capture_output=True, text=True).stdout.split()
    assert streams.count("video") == 1 and streams.count("audio") == 1


def test_mux_without_audio(samples, tmp_path):
    out = mux(str(samples / "sample_01.mp4"), [], [], str(tmp_path / "silent.mp4"))
    assert os.path.getsize(out) > 0
