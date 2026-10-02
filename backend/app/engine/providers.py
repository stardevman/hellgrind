"""Video üretim sağlayıcıları.

DRY_RUN=1 (varsayılan): LocalProvider, ücretsiz prosedürel render; hiçbir dış servise gitmez.
DRY_RUN=0: app/live/fal_video.py içindeki ücretli sağlayıcı, yalnızca açık onayla.
"""
import os
import random
import uuid
from dataclasses import dataclass, field

from app.budget import pricing
from app.config import settings
from app.engine.procedural import render_clip, render_defect

RESOLUTIONS = {"480p": (854, 480), "580p": (1032, 580), "720p": (1280, 720), "1080p": (1920, 1080)}
DEFAULT_SIZE = RESOLUTIONS["720p"]
HIGH_ACTION = 0.7


class TransientError(Exception):
    """429 / 5xx / zaman aşımı: yeniden denenebilir."""


@dataclass
class GenRequest:
    prompt: str
    seed: int
    resolution: str
    duration_s: float
    negative: str = ""
    image_url: str | None = None
    extra: dict = field(default_factory=dict)    # emotion, camera, characters, action, label (yerel render için)


@dataclass
class GenResult:
    local_path: str
    request_id: str
    cost_usd: float          # DRY_RUN'da gerçek sağlayıcının alacağı tahmini (sanal) tutar
    engine: str
    meta: dict = field(default_factory=dict)     # tanılama; Quality Gate bunu ASLA okumaz (kusuru kendisi bulmalı)


class LocalProvider:
    """Ücretsiz. Bütçe muhasebesi, gerçek sağlayıcının fiyatıyla "sanal" olarak yapılır."""

    def __init__(self, engine: str):
        self.engine = engine

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(self.engine, req.resolution, req.duration_s)

    def generate(self, req: GenRequest) -> GenResult:
        # Geçici hata seed'e bağlı DEĞİL: aynı isteği yeniden denemek gerçekte olduğu gibi kurtarabilmeli.
        if random.random() < settings.dry_run_fail_rate:
            raise TransientError("yerel: yapay geçici hata (DRY_RUN_FAIL_RATE)")
        rng = random.Random(req.seed)            # kusur kararı ise seed'e bağlıdır: tekrarlanabilir
        os.makedirs(settings.work_dir, exist_ok=True)
        out = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
        width, height = RESOLUTIONS.get(req.resolution, DEFAULT_SIZE)
        defect = rng.choice(["black", "freeze"]) if rng.random() < settings.dry_run_defect_rate else None
        if defect:
            render_defect(out, defect, req.seed, width, height, req.duration_s, settings.local_render_fps)
        else:
            x = req.extra
            render_clip(out, req.seed, width, height, req.duration_s, settings.local_render_fps,
                        emotion=x.get("emotion", "neutral"), camera=x.get("camera"), characters=x.get("characters"),
                        action=float(x.get("action", 0.0)), label=x.get("label", ""))
        return GenResult(local_path=out, request_id=f"local-{uuid.uuid4()}", cost_usd=self.estimate(req),
                         engine=self.engine, meta={"planted_defect": defect})


def get_provider(engine: str):
    """DRY_RUN açıkken her zaman LocalProvider; ücretli modül yalnızca kapalıyken ve görev içinde yüklenir."""
    if settings.dry_run:
        return LocalProvider(engine)
    from app.live.fal_video import get_live_provider
    return get_live_provider(engine)


def route_engine(scene) -> str:
    """Yoğun aksiyon (>= 0.7) Seedance'e, gerisi Wan'a (ücretsiz modda yalnızca sanal fiyatı değiştirir)."""
    return "seedance" if (scene.action_complexity or 0) >= HIGH_ACTION else "fal_wan"
