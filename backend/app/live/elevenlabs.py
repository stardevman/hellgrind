"""ÜCRETLİ: ElevenLabs metinden sese. Duygu etiketi voice_settings'e (stability, style) eşlenir."""
import time

import httpx

from app.engine.providers import TransientError
from app.live import guard
from app.quality.anomaly import probe_duration

MODEL_ID = "eleven_multilingual_v2"
EMOTION_SETTINGS = {   # (stability, style)
    "neutral": (0.55, 0.15), "joy": (0.40, 0.45), "anger": (0.30, 0.70), "fear": (0.30, 0.60),
    "sadness": (0.60, 0.40), "surprise": (0.35, 0.55), "disgust": (0.45, 0.50), "tension": (0.45, 0.45),
}
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
ATTEMPTS = 3


def voice_settings(emotion: str | None) -> dict:
    stability, style = EMOTION_SETTINGS.get(emotion or "neutral", EMOTION_SETTINGS["neutral"])
    return {"stability": stability, "similarity_boost": 0.8, "style": style, "use_speaker_boost": True}


def tts(text: str, voice_id: str | None, emotion: str, out_path: str) -> float:
    """Konuşmayı out_path'e (mp3) yazar, süresini döndürür. 429/5xx ve ağ hataları birkaç kez denenir
    (başarısız istek ücretlendirilmez); sonunda olmazsa TransientError."""
    key = guard("elevenlabs_api_key")
    if not voice_id:
        raise ValueError("Karakterin voice_id alanı boş")
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json={"text": text, "model_id": MODEL_ID, "voice_settings": voice_settings(emotion)},
                timeout=120,
            )
            if r.status_code in TRANSIENT_STATUS:
                last = f"HTTP {r.status_code}"
            else:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return round(probe_duration(out_path), 2)
        except httpx.TransportError as e:
            last = str(e)
        if attempt < ATTEMPTS:
            time.sleep(2 ** (attempt - 1))
    raise TransientError(f"ElevenLabs {ATTEMPTS} denemede yanıt vermedi: {last}")
