"""ElevenLabs sağlayıcısı: SAHTE httpx.post ile, ağ yasak. Hiçbir gerçek istek yapılmaz."""
import json
import sys
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.budget import killswitch as ks
from app.budget import pricing
from app.config import settings
from app.db import session_scope
from app.engine.providers import TransientError
from app.live import elevenlabs
from app.live.elevenlabs import EMOTION_SETTINGS, MODEL_ID, tts
from app.models import AudioTrack, Scene
from app.post import pipeline


class Reply:
    def __init__(self, status=200, content=b"mp3-bytes"):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("hata", request=None, response=self)


@pytest.fixture
def eleven(monkeypatch, tmp_path, no_network):
    sent, replies = [], []
    monkeypatch.setattr(elevenlabs, "guard", lambda key: "sahte-anahtar")
    monkeypatch.setattr(elevenlabs, "probe_duration", lambda path: 2.345)
    monkeypatch.setattr(elevenlabs.time, "sleep", lambda s: None)

    def post(url, **kw):
        sent.append({"url": url, **kw})
        reply = replies.pop(0) if replies else Reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(httpx, "post", post)
    return type("E", (), {"sent": sent, "replies": replies, "out": str(tmp_path / "ses.mp3")})


def test_request_matches_the_elevenlabs_api(eleven):
    duration = tts("Merhaba dünya", "SES123", "fear", eleven.out)

    (req,) = eleven.sent
    assert req["url"] == "https://api.elevenlabs.io/v1/text-to-speech/SES123"
    assert req["params"] == {"output_format": "mp3_44100_128"} and req["headers"]["xi-api-key"] == "sahte-anahtar"
    assert req["json"]["text"] == "Merhaba dünya" and req["json"]["model_id"] == MODEL_ID == "eleven_multilingual_v2"
    assert duration == 2.35 or duration == 2.34 or duration == 2.345       # probe_duration yuvarlanır
    assert open(eleven.out, "rb").read() == b"mp3-bytes"


@pytest.mark.parametrize("emotion", sorted(EMOTION_SETTINGS))
def test_emotion_maps_to_stability_and_style(eleven, emotion):
    tts("x", "SES", emotion, eleven.out)
    stability, style = EMOTION_SETTINGS[emotion]
    assert eleven.sent[0]["json"]["voice_settings"] == {"stability": stability, "similarity_boost": 0.8, "style": style,
                                                         "use_speaker_boost": True}


def test_angry_voice_is_less_stable_and_more_stylised_than_neutral():
    (n_stab, n_style), (a_stab, a_style) = EMOTION_SETTINGS["neutral"], EMOTION_SETTINGS["anger"]
    assert a_stab < n_stab and a_style > n_style


def test_unknown_emotion_uses_neutral(eleven):
    tts("x", "SES", "bilinmeyen", eleven.out)
    assert eleven.sent[0]["json"]["voice_settings"]["stability"] == EMOTION_SETTINGS["neutral"][0]


def test_a_character_without_voice_id_is_refused_before_any_request(eleven):
    with pytest.raises(ValueError, match="voice_id"):
        tts("x", None, "joy", eleven.out)
    assert eleven.sent == []


def test_rate_limit_and_server_errors_are_retried(eleven):
    eleven.replies += [Reply(429), Reply(503), Reply(200, b"tamam")]
    tts("x", "SES", "joy", eleven.out)
    assert len(eleven.sent) == 3 and open(eleven.out, "rb").read() == b"tamam"


def test_network_failures_are_retried(eleven):
    eleven.replies += [httpx.ConnectError("yok"), Reply(200)]
    tts("x", "SES", "joy", eleven.out)
    assert len(eleven.sent) == 2


def test_gives_up_with_a_transient_error_after_three_attempts(eleven):
    eleven.replies += [Reply(500), Reply(500), Reply(500)]
    with pytest.raises(TransientError, match="3 denemede"):
        tts("x", "SES", "joy", eleven.out)
    assert len(eleven.sent) == 3


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_client_errors_are_not_retried(eleven, status):
    eleven.replies += [Reply(status)]
    with pytest.raises(httpx.HTTPStatusError):
        tts("x", "SES", "joy", eleven.out)
    assert len(eleven.sent) == 1


# ---------- ses aşaması: bütçe rezervasyonu (veritabanı) ----------

@pytest.fixture
def voiced(pg, mk_project, mk_scene, monkeypatch, tmp_path):
    """ElevenLabs kaynaklı bir replik. `tts` taklit edilir; bellekte DRY_RUN bayrağı yalnızca ücretli dala girmek için kapatılır,
    gerçek hiçbir istek yapılmaz (no_network + sahte tts)."""
    calls = []

    def fake_tts(content, voice_id, emotion, out):
        calls.append((content, voice_id, emotion))
        open(out, "wb").write(b"x")
        return 1.5

    monkeypatch.setattr("app.live.elevenlabs.tts", fake_tts)
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))

    def make(limit=100.0, line="Burada kimse yok mu, lütfen cevap verin."):
        pid = mk_project(limit=limit)
        sid = mk_scene(pid, "refined")
        cid = str(uuid.uuid4())
        with session_scope() as s:
            s.execute(text("""INSERT INTO characters (id, project_id, name, pinecone_namespace, voice_id)
                              VALUES (:c, :p, 'Kerem', :ns, 'SES-KEREM')"""), {"c": cid, "p": pid, "ns": pid})
            tid = s.execute(text("""INSERT INTO audio_tracks (scene_id, character_id, kind, source, text, emotion, content_hash)
                                    VALUES (:s, :c, 'dialogue', 'elevenlabs', :t, 'fear', 'h') RETURNING id"""),
                            {"s": sid, "c": cid, "t": line}).scalar()
        with session_scope() as s:
            return pid, s.get(AudioTrack, tid), s.get(Scene, sid)

    return make, calls


def ledger(pid):
    with session_scope() as s:
        return s.execute(text("SELECT kind::text, provider, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"),
                         {"p": pid}).all()


def test_paid_voice_reserves_by_character_count_and_commits(voiced, tmp_path):
    make, calls = voiced
    pid, track, scene = make()

    duration = pipeline._voice(track, scene, str(tmp_path / "o.mp3"))

    estimate = pricing.estimate_tts(track.text)
    assert estimate == round(len(track.text) / 1000 * 0.30, 4) and duration == 1.5
    assert calls == [(track.text, "SES-KEREM", "fear")]
    assert [(k, p, float(a)) for k, p, a in ledger(pid)] == [("reserve", "elevenlabs", estimate), ("commit", "elevenlabs", estimate)]


def test_a_longer_line_reserves_more(voiced, tmp_path):
    make, _ = voiced
    pid, track, scene = make(line="a" * 2000)
    pipeline._voice(track, scene, str(tmp_path / "o.mp3"))
    assert float(ledger(pid)[0][2]) == 0.6


def test_a_budget_that_cannot_cover_the_line_stops_before_any_paid_call(voiced, tmp_path):
    make, calls = voiced
    pid, track, scene = make(limit=0.01, line="a" * 2000)
    with pytest.raises(ks.BudgetExceeded):
        pipeline._voice(track, scene, str(tmp_path / "o.mp3"))
    assert calls == []                                                     # sağlayıcıya hiç gidilmedi
    assert ks.is_frozen(pid)


def test_a_failed_paid_call_releases_the_reservation(voiced, monkeypatch, tmp_path):
    make, _ = voiced
    pid, track, scene = make()
    monkeypatch.setattr("app.live.elevenlabs.tts", lambda *a: (_ for _ in ()).throw(TransientError("hata")))
    with pytest.raises(TransientError):
        pipeline._voice(track, scene, str(tmp_path / "o.mp3"))
    assert [k for k, _, _ in ledger(pid)] == ["reserve", "release"]
