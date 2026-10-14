"""Ücretsiz ses üretimi.

Diyalog: Okur (yerel Türkçe sinir ağı modeli, app/post/okur) ya da espeak-ng (çevrimdışı, Türkçe). Okur'un model
dosyası yoksa espeak-ng'ye, o da kurulu değilse ya da metin boşsa yapay bir konuşma tonuna düşer.
Müzik: duyguya göre akor; FFmpeg aevalsrc ile 0,25 Hz tremolo, fade-in ve lowpass.
Ücretli ElevenLabs yolu app/live altındadır ve DRY_RUN=1 iken çağrılmaz.
Bu modül ayar okumaz; motor ve parametreler çağıranın verdiği değerlerdir, böylece tek başına test edilir.
"""
import os
import shutil
import subprocess

from app.quality.anomaly import probe_duration

WORDS_PER_SECOND = 2.5
VOICE_VARIANTS = ["m1", "f2", "m3", "f4", "m7", "f1"]       # espeak-ng ses varyantları (erkek/kadın)
EMOTION_VOICE = {   # duygu -> (perde 0-99, hız kelime/dk)
    "neutral": (50, 150), "joy": (66, 170), "anger": (38, 178), "fear": (72, 186),
    "sadness": (34, 122), "surprise": (78, 172), "disgust": (40, 138), "tension": (46, 158),
}
CHORDS = {          # duygu -> üç ses (Hz)
    "neutral": (196.0, 246.9, 293.7), "joy": (261.6, 329.6, 392.0), "sadness": (220.0, 261.6, 329.6),
    "fear": (116.5, 138.6, 164.8), "anger": (82.4, 123.5, 164.8), "tension": (146.8, 155.6, 220.0),
    "surprise": (261.6, 329.6, 415.3), "disgust": (138.6, 146.8, 207.7),
}
# Okur tek sesle konuşur (erkek, ~115 Hz). Karakterler perde kaydırmasıyla ayrılır: sıra espeak varyantlarıyla aynıdır
# (erkek/kadın), değerler yarım ton. Duygu hızı ve perdeyi hafifçe değiştirir (EMOTION_VOICE'tan, yumuşatılarak).
OKUR_SHIFTS = [0.0, 4.0, -2.5, 6.0, -4.5, 2.0]
OKUR_SPEED_GAIN, OKUR_PITCH_PER_STEP = 0.5, 0.06
TREMOLO_HZ = 0.25
MUSIC_SAMPLE_RATE = 44100


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def speech_duration(text: str) -> float:
    return max(1.0, round(len((text or "").split()) / WORDS_PER_SECOND, 2))


def voice_for(emotion: str, voice_seed: int) -> tuple[str, int, int]:
    """(ses varyantı, perde, hız). Varyant ve perde sapması karakter tohumundan gelir: karakterin sesi hep aynı kalır;
    duygu perde ve hızı belirler."""
    pitch, speed = EMOTION_VOICE.get(emotion or "neutral", EMOTION_VOICE["neutral"])
    pitch = min(99, max(0, pitch + (voice_seed // len(VOICE_VARIANTS)) % 25 - 12))
    return VOICE_VARIANTS[voice_seed % len(VOICE_VARIANTS)], pitch, speed


def okur_voice(emotion: str, voice_seed: int) -> tuple[float, float]:
    """(perde kaydırma yarım ton, hız). Kaydırma karakter tohumundan (karakterin sesi hep aynı kalır), hız ve küçük bir
    perde payı duygudan gelir."""
    pitch, speed = EMOTION_VOICE.get(emotion or "neutral", EMOTION_VOICE["neutral"])
    base_pitch, base_speed = EMOTION_VOICE["neutral"]
    shift = OKUR_SHIFTS[voice_seed % len(OKUR_SHIFTS)] + (pitch - base_pitch) * OKUR_PITCH_PER_STEP
    return round(shift, 2), round(1 + (speed / base_speed - 1) * OKUR_SPEED_GAIN, 3)


def synthesize_okur(text: str, out_path: str, model_dir: str, emotion: str = "neutral", voice_seed: int = 0) -> float:
    """Okur ile diyalog sesi (48 kHz mono WAV). Perde, süre değişmeden kaydırılır (asetrate + atempo). Okunacak bir şey
    yoksa 0 döner (çağıran yedeğe düşer)."""
    from app.post import okur
    shift, speed = okur_voice(emotion, voice_seed)
    audio = okur.voice(model_dir).say(text, speed=speed, seed=voice_seed)
    if not audio.size:
        return 0.0
    if not shift:
        okur.write_wav(out_path, audio)
    else:
        raw = out_path + ".raw.wav"
        okur.write_wav(raw, audio)
        ratio = 2 ** (shift / 12)
        _ffmpeg("-i", raw, "-af", f"asetrate={okur.SAMPLE_RATE * ratio:.0f},aresample={okur.SAMPLE_RATE},"
                f"atempo={1 / ratio:.5f}", "-ac", "1", out_path)
        os.remove(raw)
    return round(probe_duration(out_path), 2)


def synthesize_tone(text: str, out_path: str) -> float:
    """Yapay konuşma tonu: espeak-ng yokken ya da metin boşken. Süre kelime sayısından hesaplanır."""
    duration = speech_duration(text)
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc=0.3*sin(2*PI*(220+40*sin(2*PI*4*t))*t):s=44100:d={duration}",
            "-ac", "1", out_path)
    return duration


def synthesize(text: str, out_path: str, emotion: str = "neutral", voice_seed: int = 0,
               engine: str = "espeak", lang: str = "tr", model_dir: str | None = None) -> float:
    """Diyalog sesi yazar, süresini (sn) döndürür. engine="okur" model_dir ({kök}/tts/okur) ister."""
    if engine == "okur" and model_dir and (text or "").strip():
        duration = synthesize_okur(text, out_path, model_dir, emotion, voice_seed)
        if duration > 0:
            return duration
        engine = "espeak"
    if engine == "espeak" and shutil.which("espeak-ng") and (text or "").strip():
        variant, pitch, speed = voice_for(emotion, voice_seed)
        # "--": "- Nereye gidiyorsun?" gibi tireyle başlayan satırlar seçenek sanılıp sessizce ses üretmezdi
        subprocess.run(["espeak-ng", "-v", f"{lang}+{variant}", "-p", str(pitch), "-s", str(speed),
                        "-w", out_path, "--", text], check=True, capture_output=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
            return round(probe_duration(out_path), 2)
    return synthesize_tone(text, out_path)


def synthesize_music(out_path: str, emotion: str = "neutral", duration: float = 30.0) -> str:
    f1, f2, f3 = CHORDS.get(emotion or "neutral", CHORDS["neutral"])
    chord = f"0.20*sin(2*PI*{f1}*t)+0.14*sin(2*PI*{f2}*t)+0.10*sin(2*PI*{f3}*t)+0.06*sin(2*PI*{f1 / 2}*t)"
    tremolo = f"0.65+0.35*sin(2*PI*{TREMOLO_HZ}*t)"
    _ffmpeg("-f", "lavfi", "-i", f"aevalsrc=({chord})*({tremolo}):s={MUSIC_SAMPLE_RATE}:d={duration}",
            "-af", "afade=t=in:d=1,lowpass=f=2400", "-ac", "2", out_path)
    return out_path
