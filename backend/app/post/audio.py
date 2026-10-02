"""Ücretsiz ses üretimi.

Diyalog: espeak-ng (çevrimdışı, Türkçe destekli). Kurulu değilse yapay bir 'konuşma' tonuna düşer.
Müzik: duyguya göre akor seçen prosedürel fon müziği (FFmpeg).
Ücretli ElevenLabs yolu app/live/elevenlabs.py içindedir ve DRY_RUN=1 iken çağrılmaz.
Bu modül config okumaz; böylece testlerde bağımsız çalışır.
"""
import os
import shutil
import subprocess

from app.quality.anomaly import probe_duration

WORDS_PER_SECOND = 2.5
VOICE_VARIANTS = ["m1", "f2", "m3", "f4", "m7", "f1"]
EMOTION_VOICE = {   # (perde 0-99, hız kelime/dk)
    "neutral": (50, 150), "joy": (66, 170), "anger": (38, 178), "fear": (72, 186),
    "sadness": (34, 122), "surprise": (78, 172), "disgust": (40, 138), "tension": (46, 158),
}
CHORDS = {          # Hz
    "neutral": (196.0, 246.9, 293.7), "joy": (261.6, 329.6, 392.0), "sadness": (220.0, 261.6, 329.6),
    "fear": (116.5, 138.6, 164.8), "anger": (82.4, 123.5, 164.8), "tension": (146.8, 155.6, 220.0),
    "surprise": (261.6, 329.6, 415.3), "disgust": (138.6, 146.8, 207.7),
}


def speech_duration(text: str) -> float:
    return max(1.0, round(len((text or "").split()) / WORDS_PER_SECOND, 2))


def synthesize_dry(text: str, out_path: str) -> float:
    dur = speech_duration(text)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"aevalsrc=0.3*sin(2*PI*(220+40*sin(2*PI*4*t))*t):s=44100:d={dur}",
         "-ac", "1", out_path],
        check=True,
    )
    return dur


def synthesize(text: str, out_path: str, emotion: str = "neutral", voice_seed: int = 0,
               engine: str = "espeak", lang: str = "tr") -> float:
    if engine == "espeak" and shutil.which("espeak-ng") and (text or "").strip():
        pitch, speed = EMOTION_VOICE.get(emotion or "neutral", EMOTION_VOICE["neutral"])
        pitch = min(99, max(0, pitch + voice_seed % 25 - 12))
        variant = VOICE_VARIANTS[voice_seed % len(VOICE_VARIANTS)]
        # "--": "- Nereye gidiyorsun?" gibi tireyle başlayan satırlar seçenek sanılıp sessizce ses üretmiyordu
        subprocess.run(["espeak-ng", "-v", f"{lang}+{variant}", "-p", str(pitch), "-s", str(speed),
                        "-w", out_path, "--", text], check=True, capture_output=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            return round(probe_duration(out_path), 2)
    return synthesize_dry(text, out_path)      # espeak-ng yok, metin boş ya da ses üretilemedi: yapay ton


def synthesize_music(out_path: str, emotion: str = "neutral", duration: float = 30.0) -> str:
    f1, f2, f3 = CHORDS.get(emotion or "neutral", CHORDS["neutral"])
    expr = (f"(0.20*sin(2*PI*{f1}*t)+0.14*sin(2*PI*{f2}*t)+0.10*sin(2*PI*{f3}*t)+0.06*sin(2*PI*{f1 / 2}*t))"
            f"*(0.65+0.35*sin(2*PI*0.25*t))")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"aevalsrc={expr}:s=44100:d={duration}",
                    "-af", "afade=t=in:d=1,lowpass=f=2400", "-ac", "2", out_path], check=True)
    return out_path
