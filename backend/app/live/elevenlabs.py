"""ÜCRETLİ konuşma sentezi (ElevenLabs). Bu modül hiçbir yerde çalıştırılmadı.

`eleven_multilingual_v2`; duygu `stability` ve `style` değerlerine eşlenir. Her çağrı önce `guard("elevenlabs_api_key")`
çağırır. İstek biçimi ElevenLabs belgelerinden doğrulandı (2 Ekim 2026): POST /v1/text-to-speech/{voice_id}, başlık
`xi-api-key`, gövde {text, model_id, voice_settings{stability, similarity_boost, style, use_speaker_boost}}, yanıt ses dosyası.
Çağıran, karakter sayısına göre (`pricing.estimate_tts`) rezervasyon almış olmalıdır.
"""
import os

import httpx

from app.engine.providers import TransientError
from app.live import guard
from app.quality.anomaly import probe_duration

URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
EXTENSION = ".mp3"
SIMILARITY_BOOST = 0.75

EMOTION_VOICE = {   # duygu -> (stability, style): düşük kararlılık ve yüksek stil, daha duygusal okuma
    "neutral": (0.55, 0.0), "joy": (0.35, 0.45), "anger": (0.25, 0.70), "fear": (0.30, 0.55),
    "sadness": (0.60, 0.35), "surprise": (0.30, 0.60), "disgust": (0.40, 0.45), "tension": (0.40, 0.50),
}


def voice_settings(emotion: str | None) -> dict:
    stability, style = EMOTION_VOICE.get(emotion or "neutral", EMOTION_VOICE["neutral"])
    return {"stability": stability, "similarity_boost": SIMILARITY_BOOST, "style": style, "use_speaker_boost": True}


def tts(text: str, voice_id: str | None, emotion: str | None, out_path: str) -> float:
    """Metni seslendirip `out_path`'e yazar, süresini (sn) döndürür. 429 ve 5xx TransientError olur."""
    key = guard("elevenlabs_api_key")
    if not voice_id:
        raise ValueError("Bu karakter için voice_id tanımlı değil: ElevenLabs ses kimliğini karaktere ekleyin.")
    try:
        response = httpx.post(URL.format(voice_id=voice_id), params={"output_format": OUTPUT_FORMAT},
                              headers={"xi-api-key": key, "Accept": "audio/mpeg"}, timeout=120,
                              json={"text": text, "model_id": MODEL_ID, "voice_settings": voice_settings(emotion)})
    except (httpx.TransportError, TimeoutError) as e:
        raise TransientError(f"ElevenLabs: {type(e).__name__}: {e}") from e
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"ElevenLabs HTTP {response.status_code}")
    if response.status_code >= 400:                           # istek gövdesini ya da başlıkları (anahtar!) günlüğe sızdırma
        raise RuntimeError(f"ElevenLabs isteği reddedildi: HTTP {response.status_code} {response.text[:200]}")
    with open(out_path, "wb") as f:
        f.write(response.content)
    if os.path.getsize(out_path) == 0:
        raise TransientError("ElevenLabs boş ses döndürdü")
    return round(probe_duration(out_path), 2)
