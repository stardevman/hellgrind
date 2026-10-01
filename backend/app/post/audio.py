"""Ses üretimi.

Kademe 1: ElevenLabs yerine, metin uzunluğuna göre süre hesaplayan yapay bir 'konuşma' tonu üretir.
Kademe 2: synthesize() ElevenLabs API çağrısıyla değiştirilecek (duygu etiketi -> voice_settings).
"""
import subprocess

WORDS_PER_SECOND = 2.5


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
