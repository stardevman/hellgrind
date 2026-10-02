"""Ücretsiz ses üretimi: espeak-ng konuşma, yapay ton yedeği, prosedürel müzik. Veritabanı gerektirmez."""
import shutil
import subprocess
import wave

import numpy as np
import pytest

from app.post import audio
from app.post.audio import (CHORDS, EMOTION_VOICE, VOICE_VARIANTS, speech_duration, synthesize, synthesize_dry,
                            synthesize_music)
from app.quality.anomaly import probe_duration
from tests.media_utils import SR, pcm, probe, rms, tone_amp

needs_espeak = pytest.mark.skipif(not shutil.which("espeak-ng"), reason="espeak-ng kurulu değil")


def sample_rate(path):
    return int(probe(path)[0][0]["sample_rate"])


# espeak-ng 22050 Hz, yapay ton 44100 Hz üretir; hangisinin kullanıldığı buradan anlaşılır
def is_espeak(path):
    return sample_rate(path) == 22050


# ---------- konuşma süresi tahmini ve yapay ton ----------

def test_speech_duration_estimate():
    assert speech_duration("bir iki üç dört beş") == 2.0
    assert speech_duration("") == 1.0 and speech_duration(None) == 1.0       # en az 1 sn
    assert speech_duration("tek") == 1.0


def test_free_audio(tmp_path):
    d = synthesize("Dayan bırakma", str(tmp_path / "v.wav"), emotion="fear", engine="tone")
    assert d >= 1.0
    m = synthesize_music(str(tmp_path / "m.wav"), "sadness", duration=3)
    assert abs(probe_duration(m) - 3) < 0.2


def test_tone_fallback_has_the_estimated_length(tmp_path):
    out = str(tmp_path / "t.wav")
    dur = synthesize_dry("bir iki üç dört beş altı yedi sekiz", out)
    assert dur == 3.2 and abs(probe_duration(out) - 3.2) < 0.1 and not is_espeak(out)


# ---------- espeak-ng komut satırı (taklit) ----------

REAL_RUN, REAL_WHICH = subprocess.run, shutil.which


def _write_wav(path, seconds=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(22050)
        w.writeframes(b"\x01\x00" * 22050 * seconds)


@pytest.fixture
def espeak_calls(monkeypatch):
    """Yalnızca espeak-ng çağrısını yakalar (küçük geçerli bir WAV yazar); ffmpeg/ffprobe gerçek çalışır.
    Not: audio.subprocess global subprocess modülüdür, bu yüzden diğer komutlar gerçek run'a devredilir."""
    calls = []

    def fake_run(cmd, **kw):
        if cmd[0] != "espeak-ng":
            return REAL_RUN(cmd, **kw)
        calls.append(cmd)
        _write_wav(cmd[cmd.index("-w") + 1])
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else REAL_WHICH(name))
    return calls


@pytest.mark.parametrize("emotion", sorted(EMOTION_VOICE))
def test_emotion_sets_pitch_and_speed_on_the_command_line(espeak_calls, tmp_path, emotion):
    synthesize("Merhaba", str(tmp_path / "o.wav"), emotion=emotion, voice_seed=12)
    cmd = espeak_calls[0]
    pitch, speed = EMOTION_VOICE[emotion]
    assert cmd[0] == "espeak-ng"
    assert cmd[cmd.index("-p") + 1] == str(pitch) and cmd[cmd.index("-s") + 1] == str(speed)   # seed % 25 - 12 = 0


def test_voice_variant_and_pitch_offset_come_from_the_seed(espeak_calls, tmp_path):
    for seed in range(40):
        synthesize("Merhaba", str(tmp_path / "o.wav"), emotion="neutral", voice_seed=seed)
    for seed, cmd in enumerate(espeak_calls):
        assert cmd[cmd.index("-v") + 1] == f"tr+{VOICE_VARIANTS[seed % len(VOICE_VARIANTS)]}"
        assert cmd[cmd.index("-p") + 1] == str(min(99, max(0, EMOTION_VOICE["neutral"][0] + seed % 25 - 12)))
    assert {c[c.index("-v") + 1] for c in espeak_calls} == {f"tr+{v}" for v in VOICE_VARIANTS}


def test_text_goes_after_the_option_terminator(espeak_calls, tmp_path):
    synthesize("- Nereye gidiyorsun?", str(tmp_path / "o.wav"))
    cmd = espeak_calls[0]
    assert cmd[-2:] == ["--", "- Nereye gidiyorsun?"] and cmd.index("-w") < cmd.index("--")


@pytest.mark.parametrize("kwargs", [dict(engine="tone"), dict(text="  ")])
def test_tone_engine_and_blank_text_never_call_espeak(espeak_calls, tmp_path, kwargs):
    synthesize(kwargs.get("text", "Merhaba"), str(tmp_path / "o.wav"), engine=kwargs.get("engine", "espeak"))
    assert espeak_calls == []


def test_missing_espeak_falls_back_to_tone(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    out = str(tmp_path / "o.wav")
    assert synthesize("Merhaba dünya", out) == 1.0 and not is_espeak(out)


def test_espeak_that_writes_nothing_falls_back_to_tone(tmp_path, monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/espeak-ng" if name == "espeak-ng" else REAL_WHICH(name))
    monkeypatch.setattr(audio.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0) if cmd[0] == "espeak-ng" else REAL_RUN(cmd, **kw))
    out = str(tmp_path / "o.wav")
    assert synthesize("Merhaba dünya", out) == 1.0 and not is_espeak(out)


# ---------- gerçek espeak-ng ----------

@needs_espeak
def test_real_espeak_speaks_turkish(tmp_path):
    out = str(tmp_path / "s.wav")
    dur = synthesize("Burada kimse yok mu?", out)
    assert is_espeak(out) and 1.0 < dur < 6.0 and abs(probe_duration(out) - dur) < 0.05
    assert rms(pcm(out)) > 0.01


@needs_espeak
@pytest.mark.parametrize("text", ["- Nereye gidiyorsun?", "-Gel buraya.", "--sessiz ol"])
def test_real_espeak_speaks_lines_that_start_with_a_dash(tmp_path, text):
    """Regresyon: tireyle başlayan satır seçenek sanılıp dosya üretilmiyor, LIPSYNC aşaması çöküyordu."""
    out = str(tmp_path / "s.wav")
    assert synthesize(text, out) > 0.5 and is_espeak(out)


@needs_espeak
def test_real_espeak_sad_is_slower_than_fearful(tmp_path):
    text = "Burada kimse yok mu, lütfen cevap verin"
    sad = synthesize(text, str(tmp_path / "a.wav"), emotion="sadness", voice_seed=3)
    scared = synthesize(text, str(tmp_path / "b.wav"), emotion="fear", voice_seed=3)
    assert sad > scared * 1.3


@needs_espeak
def test_real_espeak_voice_is_stable_per_seed_and_differs_between_seeds(tmp_path):
    def speak(name, seed):
        synthesize("Merhaba", str(tmp_path / name), voice_seed=seed)
        return open(tmp_path / name, "rb").read()
    assert speak("a.wav", 7) == speak("b.wav", 7) != speak("c.wav", 8)


# ---------- müzik ----------

def test_music_is_stereo_and_has_the_requested_length(tmp_path):
    out = synthesize_music(str(tmp_path / "m.wav"), "joy", duration=4)
    streams, duration = probe(out)
    assert streams[0]["channels"] == 2 and abs(duration - 4) < 0.1


@pytest.mark.parametrize("emotion", sorted(CHORDS))
def test_music_plays_the_emotion_chord(tmp_path, emotion):
    out = synthesize_music(str(tmp_path / "m.wav"), emotion, duration=7)
    steady = pcm(out, 2, 6)
    f1, f2, f3 = CHORDS[emotion]
    amps = {f: tone_amp(steady, f) for f in (f1, f2, f3)}
    off_chord = tone_amp(steady, f3 * 1.37 + 40)                  # akorda olmayan bir frekans
    assert all(a > 5 * off_chord for a in amps.values()), (amps, off_chord)


def test_unknown_emotion_plays_the_neutral_chord(tmp_path):
    a = synthesize_music(str(tmp_path / "a.wav"), "???", duration=2)
    b = synthesize_music(str(tmp_path / "b.wav"), "neutral", duration=2)
    assert np.allclose(pcm(a), pcm(b))


def test_music_fades_in_over_the_first_second(tmp_path):
    x = pcm(synthesize_music(str(tmp_path / "m.wav"), "neutral", duration=5))
    assert rms(x[: SR // 10]) < 0.25 * rms(x[SR : 2 * SR])


def test_music_is_lowpassed_at_2400_hz(tmp_path):
    x = pcm(synthesize_music(str(tmp_path / "m.wav"), "joy", duration=6), 2, 5)
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    assert spectrum[freqs > 3500].sum() < 1e-6 * spectrum.sum()


def test_music_swells_with_a_quarter_hertz_tremolo(tmp_path):
    x = pcm(synthesize_music(str(tmp_path / "m.wav"), "neutral", duration=10))
    levels = [rms(x[int(t * SR): int((t + 0.5) * SR)]) for t in np.arange(2, 9.5, 0.5)]    # fade-in sonrası
    assert max(levels) > 1.8 * min(levels)                                # 0.30 - 1.00 zarfı
    assert abs(rms(x[int(2.5 * SR): int(3 * SR)]) - rms(x[int(6.5 * SR): int(7 * SR)])) < 0.1 * max(levels)   # 4 sn periyot
