"""Video üretim sağlayıcıları.

Kademe 1: yalnızca DryRunProvider çalışır. Gerçek sağlayıcılar Kademe 2'de doldurulacak;
şimdilik yanlışlıkla para harcanmasın diye açıkça hata verirler.
"""
import os
import random
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.budget import pricing
from app.config import settings


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
    extra: dict = field(default_factory=dict)


@dataclass
class GenResult:
    local_path: str
    request_id: str
    cost_usd: float
    engine: str


class DryRunProvider:
    """Sıfır maliyet: samples/ klasöründeki hazır videolardan birini seed'e göre seçer."""

    def __init__(self, engine: str):
        self.engine = engine
        self.samples = sorted(str(p) for p in Path(settings.samples_dir).glob("sample_*.mp4"))
        if not self.samples:
            raise RuntimeError("Örnek video yok. Önce: docker compose run --rm api python -m scripts.make_samples")

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(self.engine, req.resolution, req.duration_s)

    def generate(self, req: GenRequest) -> GenResult:
        rng = random.Random(req.seed)
        time.sleep(settings.dry_run_latency_s * (0.5 + rng.random()))
        if rng.random() < settings.dry_run_fail_rate:
            raise TransientError("dry-run: yapay geçici hata")
        src = self.samples[req.seed % len(self.samples)]
        os.makedirs(settings.work_dir, exist_ok=True)
        dst = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
        shutil.copyfile(src, dst)
        return GenResult(local_path=dst, request_id=f"dry-{uuid.uuid4()}",
                         cost_usd=self.estimate(req), engine=self.engine)


class NotEnabledProvider:
    def __init__(self, engine: str):
        self.engine = engine

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(self.engine, req.resolution, req.duration_s)

    def generate(self, req: GenRequest) -> GenResult:
        raise NotImplementedError(f"{self.engine} gerçek sağlayıcısı Kademe 2'de eklenecek. DRY_RUN=1 kullan.")


def get_provider(engine: str):
    return DryRunProvider(engine) if settings.dry_run else NotEnabledProvider(engine)


def route_engine(scene) -> str:
    """Basit yönlendirici: yoğun aksiyon Seedance'e, gerisi Wan'a."""
    if scene.engine:
        return scene.engine
    return "seedance" if (scene.action_complexity or 0) >= 0.7 else "fal_wan"
