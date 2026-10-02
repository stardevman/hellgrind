"""Video üretim sağlayıcıları.

DRY_RUN=1 (varsayılan): LocalProvider — ücretsiz prosedürel render, hiçbir dış servise gitmez.
DRY_RUN=0 + FAL_KEY: app/live/fal_video.py içindeki ücretli sağlayıcı (test edilmez, Kademe 2).
"""
import os
import random
import uuid
from dataclasses import dataclass, field

from app.budget import pricing
from app.config import settings
from app.engine.procedural import RESOLUTIONS, render_clip, render_defect


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
    extra: dict = field(default_factory=dict)   # emotion, camera, characters, action, label


@dataclass
class GenResult:
    local_path: str
    request_id: str
    cost_usd: float          # DRY_RUN'da gerçek sağlayıcının tutacağı tahmini (sanal) tutar
    engine: str


class LocalProvider:
    """Ücretsiz. Bütçe muhasebesi, gerçek sağlayıcının fiyatıyla 'sanal' olarak yapılır."""

    def __init__(self, engine: str):
        self.engine = engine

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(self.engine, req.resolution, req.duration_s)

    def generate(self, req: GenRequest) -> GenResult:
        # Geçici hata seed'e bağlı DEĞİL: aynı isteği yeniden denemek gerçekte olduğu gibi kurtarabilmeli.
        if random.random() < settings.dry_run_fail_rate:
            raise TransientError("yerel: yapay geçici hata (DRY_RUN_FAIL_RATE)")
        rng = random.Random(req.seed)       # bozuk klip kararı ise seed'e bağlıdır (tekrarlanabilir)
        os.makedirs(settings.work_dir, exist_ok=True)
        out = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
        w, h = RESOLUTIONS.get(req.resolution, (1280, 720))
        if rng.random() < settings.dry_run_defect_rate:          # Quality Gate'i sınamak için
            render_defect(out, kind=rng.choice(["black", "freeze"]), seed=req.seed, width=w, height=h,
                          duration=req.duration_s, fps=settings.local_render_fps)
        else:
            x = req.extra
            render_clip(out, seed=req.seed, width=w, height=h, duration=req.duration_s,
                        fps=settings.local_render_fps, emotion=x.get("emotion", "neutral"),
                        camera=x.get("camera"), characters=x.get("characters"),
                        action=float(x.get("action", 0.0)), label=x.get("label", ""))
        return GenResult(local_path=out, request_id=f"local-{uuid.uuid4()}",
                         cost_usd=self.estimate(req), engine=self.engine)


def get_provider(engine: str):
    if settings.dry_run:
        return LocalProvider(engine)
    from app.live.fal_video import get_live_provider    # ücretli yol: yalnızca açıkça istenirse
    return get_live_provider(engine)


def route_engine(scene) -> str:
    """Yoğun aksiyon (>= 0.7) Seedance'e, gerisi Wan'a (DRY_RUN'da yalnızca sanal fiyatı değiştirir).
    Yalnızca action_complexity'ye bakar; sahnede saklı eski motor kararı yönlendirmeyi kilitlemez."""
    return "seedance" if (scene.action_complexity or 0) >= 0.7 else "fal_wan"
