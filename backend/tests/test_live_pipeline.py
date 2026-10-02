"""ÜCRETLİ ses ve dudak senkronu yollarının işlem hattına bağlanışı: sahte sağlayıcılarla, gerçek veritabanı ve nesne deposuyla.

ElevenLabs ve fal çağrıları sahtedir; hiçbir test gerçek servise istek göndermez. Burada sınanan, çağrıların bütçe
rezervasyonundan geçmesi, kilidin rezervasyondan önce çalışması ve çıktıların içerik-adresli olmasıdır.
"""
import shutil
import subprocess

import pytest
from sqlalchemy import select, text

from app import storage
from app.budget import killswitch as ks
from app.budget import pricing
from app.config import settings
from app.db import session_scope
from app.engine.providers import TransientError
from app.live import PaidCallBlocked
from app.models import AudioTrack, Character
from app.post import audio, mux
from app.post.pipeline import effective_tts_engine, mux_scene, voice_and_lipsync
from app.quality.anomaly import probe_duration
from tests.media_utils import band_db, make_tone, pcm, probe
from tests.scene_utils import scene_row, track_rows

class Fakes:
    """Sahte ElevenLabs ve fal: çağrıları kaydeder, istenirse ilk N çağrıda geçici hata verir."""

    def __init__(self):
        self.tts_calls, self.lipsync_calls = [], []
        self.tts_failures = self.lipsync_failures = 0


@pytest.fixture
def fakes(refined, paid, monkeypatch, tmp_path):
    """`refined` önce: görsel üretim ücretsiz modda yapılır (ücretli modda gerçek skorlayıcı olmadığından durur); sonra kilit
    yalnızca bellekte açılır ve sağlayıcılar sahtelenir."""
    f = Fakes()

    def tts(text, voice_id, emotion, out_path):
        f.tts_calls.append((text, voice_id, emotion))
        if len(f.tts_calls) <= f.tts_failures:
            raise TransientError("429")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=1.5",
                        "-c:a", "libmp3lame", out_path], check=True)
        return 1.5

    def lipsync(video, voice, out):
        f.lipsync_calls.append((video, voice, probe(voice)["duration"], probe_duration(video)))
        if len(f.lipsync_calls) <= f.lipsync_failures:
            raise TransientError("503")
        shutil.copy(video, out)
        return out

    monkeypatch.setattr("app.live.elevenlabs.tts", tts)
    monkeypatch.setattr("app.live.fal_video.lipsync", lipsync)
    monkeypatch.setattr(settings, "tts_engine", "elevenlabs")
    return f


def ledger(pid):
    with session_scope() as s:
        rows = s.execute(text("SELECT kind, provider, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}).all()
    return [(k, p, float(a)) for k, p, a in rows]


def post_ledger(pid, before):
    return ledger(pid)[len(before):]


def dialogue_track(sid):
    return next(t for t in track_rows(sid) if t.kind == "dialogue")


# ---------- uçtan uca ----------

def test_paid_voice_and_lipsync_are_reserved_committed_and_content_addressed(refined, fakes, tmp_path):
    pid, sid = refined
    before = ledger(pid)
    key = voice_and_lipsync(sid)
    track = dialogue_track(sid)

    assert track.file_key == f"audio/elevenlabs/{track.content_hash}.mp3" and track.duration_s == 1.5 and storage.exists(track.file_key)
    text_, voice_id, emotion = fakes.tts_calls[0]
    assert (text_, emotion) == (track.text, track.emotion) and voice_id is None
    video_duration = fakes.lipsync_calls[0][3]
    rows = post_ledger(pid, before)
    assert rows == [("reserve", "elevenlabs", pytest.approx(pricing.estimate_tts(track.text), abs=1e-4)),
                    ("commit", "elevenlabs", pytest.approx(pricing.estimate_tts(track.text), abs=1e-4)),
                    ("reserve", "fal:lipsync", pytest.approx(pricing.estimate_lipsync(video_duration), abs=1e-4)),
                    ("commit", "fal:lipsync", pytest.approx(pricing.estimate_lipsync(video_duration), abs=1e-4))]
    assert key == scene_row(sid).lipsync_video_key and storage.exists(key)


def test_the_voice_id_comes_from_the_character(refined, fakes):
    pid, sid = refined
    with session_scope() as s:
        s.execute(text("UPDATE characters SET voice_id = 'ses-kerem' WHERE id = :c"), {"c": dialogue_track(sid).character_id})
    voice_and_lipsync(sid)
    assert fakes.tts_calls[0][1] == "ses-kerem"


def test_the_mixed_voice_track_given_to_lipsync_matches_the_video_length(refined, fakes):
    pid, sid = refined
    voice_and_lipsync(sid)
    _, voice_path, voice_duration, video_duration = fakes.lipsync_calls[0]
    assert voice_duration == pytest.approx(video_duration, abs=0.1)


def test_the_final_video_is_built_from_the_paid_mp3_voice(refined, fakes, tmp_path):
    pid, sid = refined
    voice_and_lipsync(sid)
    final = mux_scene(sid)
    path = storage.download(final, str(tmp_path / "f.mp4"))
    info = probe(path)
    assert len(info["video"]) == 1 and len(info["audio"]) == 1
    x = pcm(path, 16000)
    offset = dialogue_track(sid).start_offset_s
    assert band_db(x, 16000, offset + 0.3, offset + 1.2, 470, 530) > band_db(x, 16000, offset + 0.3, offset + 1.2, 1500, 2500) + 20


def test_after_all_paid_steps_nothing_stays_reserved_and_spending_equals_the_commits(refined, fakes):
    pid, sid = refined
    voice_and_lipsync(sid)
    rows = ledger(pid)
    with session_scope() as s:
        spent, reserved = s.execute(text("SELECT spent_usd, reserved_usd FROM projects WHERE id = :p"), {"p": pid}).one()
    assert float(reserved) == 0 and float(spent) == pytest.approx(sum(a for k, _, a in rows if k == "commit"), abs=1e-3)


# ---------- içerik-adresli: ikinci kez ödenmez ----------

def test_a_second_run_with_the_same_dialogue_pays_for_nothing(refined, fakes):
    pid, sid = refined
    voice_and_lipsync(sid)
    after_first = ledger(pid)
    voice_and_lipsync(sid)
    assert len(fakes.tts_calls) == 1 and len(fakes.lipsync_calls) == 1 and ledger(pid) == after_first


def test_switching_the_engine_voices_the_line_again_under_its_own_key(refined, fakes, monkeypatch):
    pid, sid = refined
    monkeypatch.setattr(settings, "tts_engine", "espeak")
    voice_and_lipsync(sid)
    espeak_key = dialogue_track(sid).file_key
    monkeypatch.setattr(settings, "tts_engine", "elevenlabs")
    voice_and_lipsync(sid)
    assert espeak_key.startswith("audio/espeak/") and dialogue_track(sid).file_key.startswith("audio/elevenlabs/")
    assert len(fakes.tts_calls) == 1


# ---------- bütçe ----------

def test_an_exhausted_budget_stops_the_paid_voice_before_the_provider_is_called(refined, fakes):
    pid, sid = refined
    with session_scope() as s:
        s.execute(text("UPDATE projects SET spent_usd = budget_limit_usd WHERE id = :p"), {"p": pid})
    with pytest.raises(ks.BudgetExceeded):
        voice_and_lipsync(sid)
    assert fakes.tts_calls == [] and ks.is_frozen(pid) and dialogue_track(sid).file_key is None


def test_a_transient_voice_error_is_retried_with_a_fresh_reservation_each_time(refined, fakes):
    pid, sid = refined
    fakes.tts_failures = 2
    before = ledger(pid)
    voice_and_lipsync(sid)
    kinds = [(k, p) for k, p, _ in post_ledger(pid, before) if p == "elevenlabs"]
    assert kinds == [("reserve", "elevenlabs"), ("release", "elevenlabs")] * 2 + [("reserve", "elevenlabs"), ("commit", "elevenlabs")]
    assert len(fakes.tts_calls) == 3


def test_a_voice_that_keeps_failing_gives_up_after_three_attempts_and_charges_nothing(refined, fakes):
    pid, sid = refined
    fakes.tts_failures = 99
    before = ledger(pid)
    with pytest.raises(TransientError):
        voice_and_lipsync(sid)
    rows = post_ledger(pid, before)
    assert len(fakes.tts_calls) == 3 and [k for k, _, _ in rows].count("release") == 3 and "commit" not in [k for k, _, _ in rows]
    assert scene_row(sid).lipsync_video_key is None


def test_a_transient_lipsync_error_is_retried_too(refined, fakes):
    pid, sid = refined
    fakes.lipsync_failures = 1
    before = ledger(pid)
    voice_and_lipsync(sid)
    kinds = [k for k, p, _ in post_ledger(pid, before) if p == "fal:lipsync"]
    assert kinds == ["reserve", "release", "reserve", "commit"] and len(fakes.lipsync_calls) == 2


def test_a_character_without_a_voice_id_fails_loudly_and_returns_the_reservation(refined, paid, monkeypatch):
    """Sahte yerine gerçek elevenlabs.tts: voice_id yoksa istek atılmadan ValueError, rezervasyon iade edilir."""
    pid, sid = refined
    monkeypatch.setattr(settings, "tts_engine", "elevenlabs")
    before = ledger(pid)
    with pytest.raises(ValueError, match="voice_id"):
        voice_and_lipsync(sid)
    assert [k for k, _, _ in post_ledger(pid, before)] == ["reserve", "release"]


# ---------- kilit ----------

def test_paid_voice_is_blocked_without_a_key_and_leaves_no_trace_in_the_ledger(refined, fakes, monkeypatch):
    pid, sid = refined
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    before = ledger(pid)
    with pytest.raises(PaidCallBlocked, match="ELEVENLABS_API_KEY"):
        voice_and_lipsync(sid)
    assert fakes.tts_calls == [] and ledger(pid) == before


def test_free_mode_never_reaches_a_paid_provider_even_when_elevenlabs_is_configured(refined, fakes, monkeypatch):
    pid, sid = refined
    monkeypatch.setattr(settings, "dry_run", True)
    before = ledger(pid)
    voice_and_lipsync(sid)
    assert effective_tts_engine() == "espeak" and dialogue_track(sid).file_key.startswith("audio/espeak/")
    assert fakes.tts_calls == [] and fakes.lipsync_calls == [] and ledger(pid) == before


def test_a_scene_without_dialogue_copies_the_video_and_pays_nothing(refined, fakes):
    pid, sid = refined
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET is_active = false WHERE scene_id = :s AND kind = 'dialogue'"), {"s": sid})
    before = ledger(pid)
    key = voice_and_lipsync(sid)
    assert fakes.tts_calls == [] and fakes.lipsync_calls == [] and ledger(pid) == before and storage.exists(key)


# ---------- ses karıştırıcı ----------

def test_mix_audio_places_each_voice_at_its_offset_and_pads_to_the_duration(tmp_path):
    a, b = make_tone(tmp_path / "a.wav", 600, 1.0), make_tone(tmp_path / "b.wav", 1400, 1.0)
    out = mux.mix_audio([mux.AudioInput(a, 0.5), mux.AudioInput(b, 3.0, -6.0)], str(tmp_path / "mix.wav"), 5.0)
    assert probe(out)["duration"] == pytest.approx(5.0, abs=0.05)
    x = pcm(out, 16000)
    assert band_db(x, 16000, 0.7, 1.3, 575, 625) > band_db(x, 16000, 0.7, 1.3, 1375, 1425) + 40
    assert band_db(x, 16000, 3.2, 3.8, 1375, 1425) > band_db(x, 16000, 3.2, 3.8, 575, 625) + 40
    assert band_db(x, 16000, 4.3, 4.9, 1375, 1425) < band_db(x, 16000, 3.2, 3.8, 1375, 1425) - 40       # sonrası sessiz


def test_mix_audio_with_a_single_voice_pads_to_the_duration(tmp_path):
    out = mux.mix_audio([mux.AudioInput(make_tone(tmp_path / "a.wav", 600, 1.0), 0.0)], str(tmp_path / "mix.wav"), 3.0)
    assert probe(out)["duration"] == pytest.approx(3.0, abs=0.05)
