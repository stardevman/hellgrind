"""ÜCRETLİ ElevenLabs modülü: sahte httpx.post ile. Hiçbir test gerçek servise istek göndermez."""
import subprocess

import httpx
import pytest

from app.config import settings
from app.engine.providers import TransientError
from app.live import PaidCallBlocked, elevenlabs
from app.live.elevenlabs import EMOTION_VOICE, tts, voice_settings

pytestmark = pytest.mark.usefixtures("paid")
KEY = "test-elevenlabs_api_key"


@pytest.fixture(scope="module")
def mp3(tmp_path_factory):
    """Gerçek bir mp3 (2 sn): sahte yanıtın gövdesi."""
    path = tmp_path_factory.mktemp("mp3") / "voice.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:a", "libmp3lame", str(path)], check=True)
    return path.read_bytes()


class Reply:
    def __init__(self, status=200, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


@pytest.fixture
def http(monkeypatch):
    sent = []
    state = {"reply": Reply(200, b"")}

    def post(url, **kw):
        sent.append((url, kw))
        reply = state["reply"]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(elevenlabs.httpx, "post", post)
    return sent, state


# ---------- istek ----------

def test_request_follows_the_documented_shape(http, mp3, tmp_path):
    sent, state = http
    state["reply"] = Reply(200, mp3)
    duration = tts("Merhaba dünya", "voice-1", "anger", str(tmp_path / "o.mp3"))
    (url, kw), = sent
    assert url == "https://api.elevenlabs.io/v1/text-to-speech/voice-1"
    assert kw["params"] == {"output_format": "mp3_44100_128"} and kw["headers"]["xi-api-key"] == KEY
    assert kw["json"] == {"text": "Merhaba dünya", "model_id": "eleven_multilingual_v2",
                          "voice_settings": {"stability": 0.25, "similarity_boost": 0.75, "style": 0.70, "use_speaker_boost": True}}
    assert duration == pytest.approx(2.0, abs=0.1) and (tmp_path / "o.mp3").read_bytes() == mp3


def test_the_model_is_eleven_multilingual_v2():
    assert elevenlabs.MODEL_ID == "eleven_multilingual_v2"


@pytest.mark.parametrize("emotion", ["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"])
def test_every_emotion_maps_to_valid_stability_and_style(emotion):
    s = voice_settings(emotion)
    assert 0 <= s["stability"] <= 1 and 0 <= s["style"] <= 1 and s["similarity_boost"] == 0.75


def test_emotion_moves_stability_and_style_in_the_expected_direction():
    assert voice_settings("anger")["stability"] < voice_settings("neutral")["stability"] < voice_settings("sadness")["stability"]
    assert voice_settings("anger")["style"] > voice_settings("neutral")["style"] and voice_settings("neutral")["style"] == 0.0
    assert voice_settings(None) == voice_settings("neutral") and voice_settings("bilinmeyen") == voice_settings("neutral")
    assert set(EMOTION_VOICE) == {"neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"}


# ---------- hatalar ----------

@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_transient(http, tmp_path, status):
    http[1]["reply"] = Reply(status)
    with pytest.raises(TransientError):
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))


@pytest.mark.parametrize("error", [httpx.ConnectError("x"), httpx.ReadTimeout("x"), TimeoutError("x")])
def test_network_errors_are_transient(http, tmp_path, error):
    http[1]["reply"] = error
    with pytest.raises(TransientError):
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_other_client_errors_fail_loudly_without_leaking_the_key(http, tmp_path, status):
    http[1]["reply"] = Reply(status, text='{"detail": "voice not found"}')
    with pytest.raises(RuntimeError) as e:
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))
    assert str(status) in str(e.value) and KEY not in str(e.value) and "voice not found" in str(e.value)


def test_an_empty_audio_body_is_transient_and_the_empty_file_is_not_trusted(http, tmp_path):
    http[1]["reply"] = Reply(200, b"")
    with pytest.raises(TransientError):
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))


def test_a_character_without_a_voice_id_fails_before_any_request(http, tmp_path):
    with pytest.raises(ValueError, match="voice_id"):
        tts("x", None, "joy", str(tmp_path / "o.mp3"))
    assert http[0] == []


# ---------- kilit ----------

def test_free_mode_blocks_before_any_request(http, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "dry_run", True)
    with pytest.raises(PaidCallBlocked):
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))
    assert http[0] == []


def test_a_missing_key_blocks_before_any_request(http, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    with pytest.raises(PaidCallBlocked, match="ELEVENLABS_API_KEY"):
        tts("x", "v", "joy", str(tmp_path / "o.mp3"))
    assert http[0] == []


def test_the_module_never_touches_the_real_network_in_tests(http, tmp_path):
    """no_network fixture'ı (paid içinde) sahte httpx.post atlanırsa bile genel internete çıkışı yasaklar."""
    import socket
    with pytest.raises(AssertionError, match="genel internete"):
        socket.create_connection(("93.184.216.34", 443), timeout=1)      # IP: DNS gerektirmez, ağsız ortamda da aynı davranır
