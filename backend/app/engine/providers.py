"""Video üretim sağlayıcıları.

DRY_RUN=1 (varsayılan): LocalProvider, ücretsiz prosedürel render; hiçbir dış servise gitmez.
DRY_RUN=0, yalnızca açık onayla: taslak MiniMax H3 Max, final Seedance 2.5, ikisi de fal.ai kuyruğunda
(app/live/fal_engines.py); Seedance 2.0 fast ve eski Wan çekimleri fal.ai'de (app/live/fal_video.py). Kendi
barındırdığımız MiniMax H3 (RunPod / vast.ai) yalnızca H3_PLATFORM açıkken; varsayılan kapalı.
"""
import math
import os
import random
import uuid
from dataclasses import dataclass, field

from app.budget import pricing
from app.config import settings
from app.engine.procedural import render_clip, render_defect

RESOLUTIONS = {"480p": (854, 480), "580p": (1032, 580), "720p": (1280, 720), "1080p": (1920, 1080)}
DEFAULT_SIZE = RESOLUTIONS["720p"]
DEFAULT_ENGINE = "h3max"         # taslak motoru (Ayarlar › VIDEO_DRAFT_ENGINE, proje ayarı draft_engine önce gelir)
CHOSEN_ENGINES = ("h3max", "seedance25", "seedance", "h3")   # seçilmiş motor yönlendirmede korunur
MIN_SECONDS = {"h3": 4, "seedance": 4, "seedance25": 4}   # bu motorlar daha kısa klip üretmez: fazlası da faturada


def default_engine(cfg: dict | None = None) -> str:
    """Yeni çekimlerin motoru: projenin taslak motoru, yoksa Ayarlar'daki VIDEO_DRAFT_ENGINE (varsayılan h3max)."""
    from app.engine.fal_plan import draft_engine
    return draft_engine(cfg)


def billed_seconds(engine: str, duration_s: float) -> float:
    """Motorun gerçekten ürettiği (ve ücretlendirilen) süre: en kısa süresi olan motorlarda tam saniyeye yuvarlanır."""
    if engine == "h3max":
        from app.engine.fal_plan import billed_seconds as fal_seconds
        return fal_seconds(engine, duration_s)
    if engine not in MIN_SECONDS:
        return duration_s
    return float(max(MIN_SECONDS[engine], math.ceil(duration_s - 1e-6)))


class TransientError(Exception):
    """429 / 5xx / zaman aşımı: yeniden denenebilir."""


class ProviderError(RuntimeError):
    """Sağlayıcı isteği tamamlanamadı ve yeniden denenmez (ör. fal.ai 422 doğrulama hatası): mesaj olduğu gibi denetçide
    gösterilir. Ücretli modüllerin hata sınıfları (app/live/fal_queue.FalError) bundan türer."""


@dataclass
class GenRequest:
    prompt: str
    seed: int
    resolution: str
    duration_s: float
    negative: str = ""
    image_url: str | None = None
    extra: dict = field(default_factory=dict)    # emotion, camera, characters, action, label, lighting (yerel render için)


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
        if self.engine in ("h3max", "seedance25"):                 # fal: aynı isteğin tahmini (referans görselleri dahil)
            from app.engine.fal_plan import cost
            return cost(self.engine, req.resolution, req.duration_s, int(req.extra.get("fal_images") or 0))["reserve_usd"]
        return pricing.estimate_video(self.engine, req.resolution, billed_seconds(self.engine, req.duration_s),
                                      mode=req.extra.get("h3_mode"))

    def generate(self, req: GenRequest) -> GenResult:
        # Geçici hata seed'e bağlı DEĞİL: aynı isteği yeniden denemek gerçekte olduğu gibi kurtarabilmeli.
        if random.random() < settings.dry_run_fail_rate:
            raise TransientError("yerel: yapay geçici hata (DRY_RUN_FAIL_RATE)")
        rng = random.Random(req.seed)            # kusur kararı ise seed'e bağlıdır: tekrarlanabilir
        os.makedirs(settings.work_dir, exist_ok=True)
        out = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
        size = {"480P": "480p", "768P": "720p", "1080P": "1080p"}.get(req.resolution, req.resolution)   # fal H3 Max
        width, height = RESOLUTIONS.get(size, DEFAULT_SIZE)
        defect = rng.choice(["black", "freeze"]) if rng.random() < settings.dry_run_defect_rate else None
        if defect:
            render_defect(out, defect, req.seed, width, height, req.duration_s, settings.local_render_fps)
        else:
            x = req.extra
            render_clip(out, req.seed, width, height, req.duration_s, settings.local_render_fps,
                        emotion=x.get("emotion", "neutral"), camera=x.get("camera"), characters=x.get("characters"),
                        action=float(x.get("action", 0.0)), label=x.get("label", ""), lighting=x.get("lighting", ""))
        return GenResult(local_path=out, request_id=f"local-{uuid.uuid4()}", cost_usd=self.estimate(req),
                         engine=self.engine, meta={"planted_defect": defect})


def get_provider(engine: str):
    """DRY_RUN açıkken her zaman LocalProvider; ücretli modül yalnızca kapalıyken ve görev içinde yüklenir."""
    if settings.dry_run:
        return LocalProvider(engine)
    if engine in ("h3max", "seedance25"):                       # fal.ai kuyruğu (taslak / final)
        from app.live.fal_engines import get_live_provider as fal_provider
        return fal_provider(engine)
    if engine == "h3":
        if settings.h3_platform == "vast":                      # deneysel, varsayılan kapalı
            from app.live.vast_h3 import get_live_provider as h3_provider
        elif settings.h3_platform == "runpod":                  # RunPod Serverless
            from app.live.runpod_h3 import get_live_provider as h3_provider
        else:
            raise H3Disabled()
        return h3_provider()
    from app.live.fal_video import get_live_provider
    return get_live_provider(engine)


H3_DISABLED = "Bu motor kapalı (RunPod uç noktası silindi; H3_PLATFORM=kapali). Çekimi H3 Max'e geçirin."


class H3Disabled(RuntimeError):
    """Kendi barındırdığımız MiniMax H3 kapalı: hiçbir RunPod / vast.ai çağrısı yapılmaz."""

    def __init__(self):
        super().__init__(H3_DISABLED)


def h3_disabled(engine: str | None) -> bool:
    """Ücretli modda RunPod/vast.ai H3'ü kapalıyken "h3" motorlu çekim üretilemez (ücretsiz modda yerel motor çizer)."""
    return (engine or "h3") == "h3" and settings.h3_platform not in ("runpod", "vast")


def route_engine(scene, cfg: dict | None = None) -> str:
    """Seçilmiş motor (H3 Max, Seedance 2.5, Seedance 2.0 fast, H3) korunur; motorsuz, eski Wan ya da Higgsfield çekimleri
    taslak motoruna gider (projenin draft_engine'i, yoksa Ayarlar'daki VIDEO_DRAFT_ENGINE)."""
    engine = getattr(scene, "engine", None)
    return engine if engine in CHOSEN_ENGINES else default_engine(cfg)
