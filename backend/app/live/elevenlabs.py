"""ÜCRETLİ konuşma sentezi (ElevenLabs). Bu modül hiçbir yerde çalıştırılmadı.

Model `settings.elevenlabs_model` (varsayılan `eleven_multilingual_v2`; en yenisi `eleven_v4`); duygu `stability` ve
`style` değerlerine eşlenir. Her çağrı önce `guard("elevenlabs_api_key")` çağırır. İstek biçimi ElevenLabs belgelerinden
doğrulandı (2 Ekim 2026, model ve dil alanları 5 Kasım 2026): POST /v1/text-to-speech/{voice_id}, başlık `xi-api-key`, gövde
{text, model_id, voice_settings{stability, similarity_boost, style, use_speaker_boost}, language_code}, yanıt ses dosyası.
`language_code` dili sabitler (ör. "Pamuk Nine" İngilizce "nine" okunmaz); Multilingual v2 bu alanı desteklemez, ona
gönderilmez. v3 ve v4'te stil ve konuşmacı güçlendirme ayarı yoktur, onlara yalnızca kararlılık ve benzerlik gider.
Çağıran, karakter sayısına göre (`pricing.estimate_tts`) rezervasyon almış olmalıdır.
"""
import os

import httpx

from app.budget import history
from app.config import settings
from app.engine.providers import TransientError
from app.live import guard
from app.quality.anomaly import probe_duration

URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL_ID = "eleven_multilingual_v2"                # varsayılan; asıl değer settings.elevenlabs_model
OUTPUT_FORMAT = "mp3_44100_128"
EXTENSION = ".mp3"
SIMILARITY_BOOST = 0.75

EMOTION_VOICE = {   # duygu -> (stability, style): düşük kararlılık ve yüksek stil, daha duygusal okuma
    "neutral": (0.55, 0.0), "joy": (0.35, 0.45), "anger": (0.25, 0.70), "fear": (0.30, 0.55),
    "sadness": (0.60, 0.35), "surprise": (0.30, 0.60), "disgust": (0.40, 0.45), "tension": (0.40, 0.50),
}


NO_LANGUAGE_CODE = ("eleven_multilingual",)     # language_code'u desteklemeyen modeller (API: "not supported for multilingual_v2")
NO_STYLE = ("eleven_v3", "eleven_v4")           # stil ve konuşmacı güçlendirme ayarı olmayan modeller


def language_code(model: str | None = None) -> str | None:
    """Gönderilecek dil kodu: ayar ("tr"); "auto", boş ya da dil kodu almayan model için yok (model tahmin eder)."""
    model = model or settings.elevenlabs_model
    lang = (settings.elevenlabs_language or "").strip()
    return None if not lang or lang == "auto" or model.startswith(NO_LANGUAGE_CODE) else lang


def voice_settings(emotion: str | None, model: str | None = None) -> dict:
    model = model or settings.elevenlabs_model
    stability, style = EMOTION_VOICE.get(emotion or "neutral", EMOTION_VOICE["neutral"])
    if model.startswith(NO_STYLE):
        return {"stability": stability, "similarity_boost": SIMILARITY_BOOST}
    return {"stability": stability, "similarity_boost": SIMILARITY_BOOST, "style": style, "use_speaker_boost": True}


def request_body(text: str, emotion: str | None) -> dict:
    model = settings.elevenlabs_model
    body = {"text": text, "model_id": model, "voice_settings": voice_settings(emotion, model)}
    if lang := language_code(model):
        body["language_code"] = lang
    return body


def tts(text: str, voice_id: str | None, emotion: str | None, out_path: str) -> float:
    """Metni seslendirip `out_path`'e yazar, süresini (sn) döndürür. 429 ve 5xx TransientError olur."""
    key = guard("elevenlabs_api_key")
    if not voice_id:
        raise ValueError("Bu karakter için voice_id tanımlı değil: ElevenLabs ses kimliğini karaktere ekleyin.")
    body = request_body(text, emotion)
    try:
        response = httpx.post(URL.format(voice_id=voice_id), params={"output_format": OUTPUT_FORMAT},
                              headers={"xi-api-key": key, "Accept": "audio/mpeg"}, timeout=120, json=body)
    except (httpx.TransportError, TimeoutError) as e:
        raise TransientError(f"ElevenLabs: {type(e).__name__}: {e}") from e
    history.step(body["model_id"], (getattr(response, "headers", None) or {}).get("request-id"),
                 characters=len(text), voice_id=voice_id, emotion=emotion or "neutral", voice=body["voice_settings"],
                 language=body.get("language_code") or "auto", http_status=response.status_code)
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"ElevenLabs HTTP {response.status_code}")
    if response.status_code >= 400:                           # istek gövdesini ya da başlıkları (anahtar!) günlüğe sızdırma
        raise RuntimeError(f"ElevenLabs isteği reddedildi: HTTP {response.status_code} {response.text[:200]}")
    with open(out_path, "wb") as f:
        f.write(response.content)
    if os.path.getsize(out_path) == 0:
        raise TransientError("ElevenLabs boş ses döndürdü")
    return round(probe_duration(out_path), 2)
