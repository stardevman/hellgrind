"""Ücretsiz ses üretimi: espeak-ng konuşma, yapay ton, prosedürel müzik. Veritabanı gerektirmez."""
import shutil

import numpy as np
import pytest

from app.post import audio
from app.post.audio import CHORDS, EMOTION_VOICE, VOICE_VARIANTS, synthesize, synthesize_music, voice_for
from tests.media_utils import band_db, pcm, probe, rms

needs_espeak = pytest.mark.skipif(not shutil.which("espeak-ng"), reason="espeak-ng kurulu değil")
SENTENCE = "Burada kimse yok mu? Sesini alçalt, makineler hâlâ dinliyor."


def samples(path, rate=16000):
    return pcm(path, rate)


# ---------- konuşma: espeak-ng ----------

@needs_espeak
def test_espeak_speaks_turkish_text_and_reports_the_real_duration(tmp_path):
    out = str(tmp_path / "s.wav")
    duration = synthesize(SENTENCE, out, engine="espeak")
    info = probe(out)
    assert info["audio"] and not info["video"]
    assert duration == pytest.approx(info["duration"], abs=0.02) and duration > 1.5
    assert rms(samples(out), 16000, 0, duration) > 0.01                     # gerçekten ses var


@needs_espeak
def test_emotion_sets_speed_sad_speech_is_slower_than_afraid_speech(tmp_path):
    sad = synthesize(SENTENCE, str(tmp_path / "sad.wav"), emotion="sadness")
    fear = synthesize(SENTENCE, str(tmp_path / "fear.wav"), emotion="fear")
    assert sad > fear * 1.2
    assert EMOTION_VOICE["sadness"][1] < EMOTION_VOICE["neutral"][1] < EMOTION_VOICE["fear"][1]


@needs_espeak
def test_emotion_sets_pitch_surprise_is_higher_than_anger(tmp_path):
    def centroid(emotion):
        out = str(tmp_path / f"{emotion}.wav")
        d = synthesize(SENTENCE, out, emotion=emotion)
        x = samples(out)[: int(d * 16000)]
        spectrum = np.abs(np.fft.rfft(x)) ** 2
        freqs = np.fft.rfftfreq(len(x), 1 / 16000)
        return float((spectrum[freqs < 1500] * freqs[freqs < 1500]).sum() / spectrum[freqs < 1500].sum())
    assert centroid("surprise") > centroid("anger")


@needs_espeak
def test_a_character_keeps_the_same_voice_and_different_characters_differ(tmp_path):
    a1 = synthesize(SENTENCE, str(tmp_path / "a1.wav"), voice_seed=123)
    a2 = synthesize(SENTENCE, str(tmp_path / "a2.wav"), voice_seed=123)
    b = synthesize(SENTENCE, str(tmp_path / "b.wav"), voice_seed=124)
    assert a1 == a2 and np.array_equal(samples(tmp_path / "a1.wav"), samples(tmp_path / "a2.wav"))
    assert not np.array_equal(samples(tmp_path / "a1.wav")[:8000], samples(tmp_path / "b.wav")[:8000])


@needs_espeak
def test_lines_starting_with_a_dash_are_spoken_not_swallowed_as_options(tmp_path):
    out = str(tmp_path / "d.wav")
    duration = synthesize("- Nereye gidiyorsun?", out)
    assert duration > 0.5 and rms(samples(out), 16000, 0, duration) > 0.01


def test_voice_variant_and_pitch_derive_from_the_seed_and_stay_in_range():
    seen = set()
    for seed in range(200):
        variant, pitch, speed = voice_for("neutral", seed)
        assert variant in VOICE_VARIANTS and 0 <= pitch <= 99 and speed == EMOTION_VOICE["neutral"][1]
        seen.add((variant, pitch))
    assert len({v for v, _ in seen}) == len(VOICE_VARIANTS) and len(seen) > 20
    assert voice_for("neutral", 77) == voice_for("neutral", 77)
    assert voice_for("no_such_emotion", 1)[2] == EMOTION_VOICE["neutral"][1]


# ---------- yapay ton (yedek) ----------

def test_tone_engine_never_calls_espeak(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = str(tmp_path / "t.wav")
    duration = synthesize(SENTENCE, out, engine="espeak")                     # espeak-ng yokmuş gibi
    assert duration == pytest.approx(audio.speech_duration(SENTENCE)) and probe(out)["audio"]


def test_tone_mode_is_selectable_and_has_a_duration_from_the_word_count(tmp_path):
    out = str(tmp_path / "t.wav")
    duration = synthesize("bir iki üç dört beş", out, engine="tone")
    assert duration == 2.0 and probe(out)["duration"] == pytest.approx(2.0, abs=0.05)
    assert rms(samples(out), 16000, 0, 2.0) > 0.05


def test_empty_text_falls_back_to_a_one_second_tone(tmp_path):
    out = str(tmp_path / "t.wav")
    assert synthesize("   ", out, engine="espeak") == 1.0 and probe(out)["duration"] == pytest.approx(1.0, abs=0.05)


# ---------- müzik ----------

@pytest.mark.parametrize("emotion", list(CHORDS))
def test_every_emotion_has_its_own_stereo_chord_of_the_requested_length(tmp_path, emotion):
    out = synthesize_music(str(tmp_path / "m.wav"), emotion, 6.0)
    info = probe(out)
    assert info["duration"] == pytest.approx(6.0, abs=0.05)
    assert int(info["audio"][0]["channels"]) == 2 and int(info["audio"][0]["sample_rate"]) == audio.MUSIC_SAMPLE_RATE


def test_each_chord_sounds_at_its_root_note(tmp_path):
    for emotion in ("anger", "joy"):
        out = synthesize_music(str(tmp_path / f"{emotion}.wav"), emotion, 6.0)
        x, root = samples(out, 8000), CHORDS[emotion][0]
        at_root = band_db(x, 8000, 1.5, 5.5, root - 4, root + 4)
        off_root = band_db(x, 8000, 1.5, 5.5, root * 1.3, root * 1.3 + 8)
        assert at_root > off_root + 10


def test_music_fades_in_and_is_low_passed(tmp_path):
    out = synthesize_music(str(tmp_path / "m.wav"), "joy", 6.0)
    x = samples(out, 16000)
    assert rms(x, 16000, 0.0, 0.2) < rms(x, 16000, 2.0, 2.2) * 0.5               # fade-in
    assert band_db(x, 16000, 1.5, 5.5, 3000, 7500) < band_db(x, 16000, 1.5, 5.5, 200, 600) - 30   # lowpass 2,4 kHz


def test_music_has_a_slow_tremolo_at_a_quarter_hertz(tmp_path):
    out = synthesize_music(str(tmp_path / "m.wav"), "neutral", 16.0)
    x = samples(out, 8000)
    window = 0.2
    env = np.array([rms(x, 8000, t, t + window) for t in np.arange(2.0, 15.8, window)])
    env = env - env.mean()
    spectrum = np.abs(np.fft.rfft(env * np.hanning(len(env))))
    freqs = np.fft.rfftfreq(len(env), window)
    assert 0.2 <= freqs[spectrum[1:].argmax() + 1] <= 0.3


def test_unknown_emotion_falls_back_to_the_neutral_chord(tmp_path):
    a = samples(synthesize_music(str(tmp_path / "a.wav"), "bilinmeyen", 3.0))
    b = samples(synthesize_music(str(tmp_path / "b.wav"), "neutral", 3.0))
    assert np.allclose(a, b, atol=1e-3)
