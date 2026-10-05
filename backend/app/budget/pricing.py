import math
from functools import lru_cache

import yaml

from app.config import settings


@lru_cache(maxsize=1)
def table(path: str | None = None) -> dict:
    with open(path or settings.pricing_file) as f:
        return yaml.safe_load(f)


def estimate_video(engine: str, resolution: str, duration_s: float, prices: dict | None = None) -> float:
    per_engine = (prices or table())["video_per_second"][engine]
    per_second = per_engine.get(resolution, max(per_engine.values()))  # bilinmeyen çözünürlük: en pahalısı
    return round(per_second * duration_s, 4)


def estimate_lipsync(duration_s: float, prices: dict | None = None) -> float:
    return round((prices or table())["lipsync_per_minute"] * duration_s / 60.0, 4)


def estimate_image(megapixels: float, prices: dict | None = None) -> float:
    """Flux LoRA: megapiksel başına, yukarı yuvarlanır (en az 1 MP) — tahmin bilerek tutucudur."""
    return round(math.ceil(megapixels) * (prices or table())["image_per_megapixel"]["flux_lora"], 4)


def estimate_reference_image(prices: dict | None = None) -> float:
    """Flux Kontext: referans görselden bir kare, görsel başına."""
    return round((prices or table())["image_per_image"]["flux_kontext"], 4)


def estimate_frame(reference: bool, megapixels: float = 1.0, prices: dict | None = None) -> float:
    """İlk kare ya da storyboard karesi: karakter referansı varsa Kontext, yoksa Flux LoRA."""
    return estimate_reference_image(prices) if reference else estimate_image(megapixels, prices)


def estimate_tts(text: str, prices: dict | None = None) -> float:
    return round(len(text or "") / 1000.0 * (prices or table())["tts_per_1k_chars"], 4)


def estimate_llm(input_tokens: int, output_tokens: int, prices: dict | None = None) -> float:
    rate = (prices or table())["llm_per_million_tokens"]
    return round((input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000, 6)


# ---------- panelden düzenleme ----------

REQUIRED_ENGINES = ("fal_wan", "seedance", "wan_v2v")      # kodun fiyat sorduğu video motorları


class PricingError(ValueError):
    """Fiyat tablosu geçersiz (kullanıcıya Türkçe gösterilir)."""


def _positive(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise PricingError(f"{where}: sıfırdan büyük bir sayı olmalı (gelen: {value!r})")
    return float(value)


def validate_table(text: str) -> dict:
    """YAML'ı okur ve kodun kullandığı her fiyatın var ve pozitif olduğunu denetler. Yorumlar dosyada korunur."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PricingError(f"YAML okunamadı: {e}") from e
    if not isinstance(data, dict):
        raise PricingError("Fiyat tablosu bir sözlük olmalı")
    video = data.get("video_per_second")
    if not isinstance(video, dict):
        raise PricingError("video_per_second eksik")
    for engine in REQUIRED_ENGINES:
        if not isinstance(video.get(engine), dict) or not video[engine]:
            raise PricingError(f"video_per_second.{engine} eksik (çözünürlük: fiyat)")
    for engine, rows in video.items():
        if not isinstance(rows, dict):
            raise PricingError(f"video_per_second.{engine}: çözünürlük -> fiyat sözlüğü olmalı")
        for res, price in rows.items():
            _positive(price, f"video_per_second.{engine}.{res}")
    _positive((data.get("image_per_megapixel") or {}).get("flux_lora"), "image_per_megapixel.flux_lora")
    _positive((data.get("image_per_image") or {}).get("flux_kontext"), "image_per_image.flux_kontext")
    _positive(data.get("lipsync_per_minute"), "lipsync_per_minute")
    _positive(data.get("tts_per_1k_chars"), "tts_per_1k_chars")
    llm = data.get("llm_per_million_tokens") or {}
    _positive(llm.get("input"), "llm_per_million_tokens.input")
    _positive(llm.get("output"), "llm_per_million_tokens.output")
    return data


def save_table(text: str) -> dict:
    """Doğrular ve dosyayı yerinde yazar (bağlı tek dosya yeniden adlandırılamaz); önbelleği boşaltır."""
    data = validate_table(text)
    try:
        with open(settings.pricing_file, "r+", encoding="utf-8") as f:
            f.seek(0)
            f.write(text if text.endswith("\n") else text + "\n")
            f.truncate()
    except OSError as e:
        raise PricingError(f"Fiyat dosyasına yazılamadı ({e.strerror}): {settings.pricing_file}") from e
    table.cache_clear()
    return data
