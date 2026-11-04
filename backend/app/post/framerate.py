"""Kare hızı kilidi (proje ayarı `fps_lock`): filmin tek kare hızı.

Video modelleri kendi sabit hızlarında üretir (H3, Seedance, Veo 24; Wan 16; dışarıdan yüklenen çekim 25 ya da 30 olabilir).
Kilit açıkken her çekim renk aşamasında (GRADE) bu hıza çevrilir (FFmpeg `fps`: süre korunur, kare atılır ya da
tekrarlanır), kare tutma (on twos) bu hız üzerinde yapılır ve film, kaba kurgu, teslim şartnamesi ve EDL/OTIO aynı hızı
kullanır. Kilit kapalıyken (None) eski davranış: çekimler kaynak hızında kalır, film 24 fps ya da şartnamenin hızıyla
birleşir.
"""
import json
import subprocess

# kare hızı -> (FFmpeg yazımı, sayı, EDL zaman tabanı)
RATES = {"23.976": ("24000/1001", 24000 / 1001, 24), "24": ("24", 24.0, 24), "25": ("25", 25.0, 25)}
LABELS = {"23.976": "23,976 fps (NTSC film)", "24": "24 fps (sinema, çizgi film)", "25": "25 fps (TV Türkiye / Avrupa)"}


def lock_of(project_settings: dict | None) -> str | None:
    """Projenin kilitli kare hızı ("23.976" | "24" | "25") ya da None (kilit yok)."""
    value = (project_settings or {}).get("fps_lock")
    return value if value in RATES else None


def ffmpeg_rate(lock: str) -> str:
    return RATES[lock][0]


def rate_value(text: str) -> float:
    """"24000/1001" ya da "25" -> sayı; okunamazsa 0."""
    try:
        if "/" in text:
            a, b = text.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(text)
    except ValueError:
        return 0.0


def matches(source_rate: str, lock: str) -> bool:
    """Kaynak hızı kilitle aynı mı (0,01 fps toleransla)."""
    return abs(rate_value(source_rate) - RATES[lock][1]) < 0.01


def source_rates(paths: list[str]) -> list[str]:
    """Videoların kaynak kare hızları (ffprobe, kesirli yazım); okunamayan "?"."""
    out = []
    for path in paths:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "json", path], capture_output=True, text=True)
        try:
            out.append(json.loads(r.stdout)["streams"][0]["r_frame_rate"])
        except (ValueError, KeyError, IndexError):
            out.append("?")
    return out
