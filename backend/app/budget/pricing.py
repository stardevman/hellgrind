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


def estimate_tts(text: str, prices: dict | None = None) -> float:
    return round(len(text or "") / 1000.0 * (prices or table())["tts_per_1k_chars"], 4)


def estimate_llm(input_tokens: int, output_tokens: int, prices: dict | None = None) -> float:
    rate = (prices or table())["llm_per_million_tokens"]
    return round((input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000, 6)
