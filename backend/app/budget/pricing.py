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
