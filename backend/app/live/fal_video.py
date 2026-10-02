"""ÜCRETLİ: fal.ai üzerinden Flux LoRA ilk kare -> Wan 2.2 / Seedance video, ve dudak senkronu.

Uç nokta kimlikleri ve parametreler fal.ai model sayfalarından doğrulandı (2 Ekim 2026):
  fal-ai/flux-lora                          prompt, image_size, seed, loras[{path, scale}], num_images -> images[].url
  fal-ai/wan/v2.2-a14b/image-to-video       prompt, image_url, resolution 480p|580p|720p, num_frames 17-161,
                                            frames_per_second 4-60, negative_prompt, seed, aspect_ratio -> video.url
  bytedance/seedance-2.0/fast/image-to-video prompt, image_url, resolution 480p|720p, duration "auto"|4-15,
                                            aspect_ratio, generate_audio (seed parametresi YOK) -> video.url
  fal-ai/sync-lipsync/v2/pro                video_url, audio_url, sync_mode -> video.url
Fiyatlar değişir (pricing.yaml). Bu modül DRY_RUN=1 iken hiçbir şey yapmaz: her giriş noktası guard()'dan geçer.
"""
import os
import time
import uuid

import httpx

from app.budget import pricing
from app.config import settings
from app.engine.providers import GenRequest, GenResult, TransientError
from app.live import guard

ENDPOINTS = {
    "flux": "fal-ai/flux-lora",
    "fal_wan": "fal-ai/wan/v2.2-a14b/image-to-video",
    "seedance": "bytedance/seedance-2.0/fast/image-to-video",
    "lipsync": "fal-ai/sync-lipsync/v2/pro",
}
FLUX_IMAGE_SIZE, FLUX_MEGAPIXELS = "landscape_16_9", 1024 * 576 / 1e6
WAN_FPS, WAN_FRAMES = 16, (17, 161)
SEEDANCE_SECONDS = (4, 15)
RESOLUTIONS = {"fal_wan": ("480p", "580p", "720p"), "seedance": ("480p", "720p")}
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
DOWNLOAD_ATTEMPTS = 5


def _fal():
    os.environ["FAL_KEY"] = guard("fal_key")
    import fal_client                      # ücretli bağımlılık: yalnızca burada, işlev içinde
    return fal_client


def is_transient(e: Exception) -> bool:
    """fal-client HTTP hatalarını FalClientHTTPError (.status_code), zaman aşımını TimeoutError olarak fırlatır."""
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    return status in TRANSIENT_STATUS or isinstance(e, (TimeoutError, httpx.TransportError))


def _call(fal, app: str, args: dict) -> tuple[dict, str | None]:
    """(sonuç, fal istek kimliği). 429 ve 5xx TransientError olur; geri kalan hatalar olduğu gibi yükselir."""
    request_id: list[str] = []
    try:
        return fal.subscribe(app, arguments=args, on_enqueue=request_id.append), (request_id[0] if request_id else None)
    except Exception as e:
        if is_transient(e):
            raise TransientError(str(e)) from e
        raise


def _download(url: str, ext: str = ".mp4") -> str:
    """Üretim ücretlendi; indirme geçici hatada birkaç kez denenir (üretimi baştan ödememek için)."""
    os.makedirs(settings.work_dir, exist_ok=True)
    out = os.path.join(settings.work_dir, f"{uuid.uuid4()}{ext}")
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with open(out, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            return out
        except httpx.HTTPError:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(2 ** (attempt - 1))
    raise AssertionError("ulaşılamaz")


def resolution_for(engine: str, requested: str) -> str:
    return requested if requested in RESOLUTIONS[engine] else "720p"


def wan_frames(seconds: float) -> int:
    return min(WAN_FRAMES[1], max(WAN_FRAMES[0], round(seconds * WAN_FPS)))


def seedance_seconds(seconds: float) -> int:
    return min(SEEDANCE_SECONDS[1], max(SEEDANCE_SECONDS[0], round(seconds)))


def billed_seconds(engine: str, seconds: float) -> float:
    """Gerçekte üretilen (ve faturalanan) süre: istek 3 sn olsa da Seedance en az 4 sn üretir."""
    return float(seedance_seconds(seconds)) if engine == "seedance" else wan_frames(seconds) / WAN_FPS


class FalVideoProvider:
    def __init__(self, engine: str):
        self.fal = _fal()
        if engine not in ("fal_wan", "seedance"):
            raise ValueError(f"{engine} için ücretli sağlayıcı yok (yalnızca fal_wan ve seedance)")
        self.engine = engine

    def estimate(self, req: GenRequest) -> float:
        video = pricing.estimate_video(self.engine, resolution_for(self.engine, req.resolution),
                                       billed_seconds(self.engine, req.duration_s))
        return round(video + pricing.estimate_image(FLUX_MEGAPIXELS), 4)

    def _first_frame(self, req: GenRequest) -> str:
        if req.image_url:                  # yeniden denemede ilk kare tekrar üretilmez
            return req.image_url
        triggers = req.extra.get("lora_triggers", [])
        img, _ = _call(self.fal, ENDPOINTS["flux"], {
            "prompt": ", ".join([*triggers, req.prompt]), "seed": req.seed, "image_size": FLUX_IMAGE_SIZE,
            "num_images": 1, "loras": req.extra.get("loras", []),
        })
        req.image_url = img["images"][0]["url"]
        return req.image_url

    def video_args(self, req: GenRequest, image_url: str) -> dict:
        resolution = resolution_for(self.engine, req.resolution)
        if self.engine == "seedance":
            return {"prompt": req.prompt, "image_url": image_url, "resolution": resolution, "aspect_ratio": "16:9",
                    "duration": str(seedance_seconds(req.duration_s)), "generate_audio": False}   # sesi kendimiz ekleriz
        args = {"prompt": req.prompt, "image_url": image_url, "seed": req.seed, "resolution": resolution,
                "aspect_ratio": "16:9", "num_frames": wan_frames(req.duration_s), "frames_per_second": WAN_FPS}
        if req.negative:
            args["negative_prompt"] = req.negative
        return args

    def generate(self, req: GenRequest) -> GenResult:
        image_url = self._first_frame(req)
        vid, request_id = _call(self.fal, ENDPOINTS[self.engine], self.video_args(req, image_url))
        return GenResult(local_path=_download(vid["video"]["url"]), request_id=f"fal-{request_id or uuid.uuid4()}",
                         cost_usd=self.estimate(req), engine=self.engine)


def get_live_provider(engine: str):
    return FalVideoProvider(engine)


def lipsync(video_path: str, audio_source_path: str, out_path: str) -> str:
    """audio_source_path: içinde konuşma sesi olan dosya (mp4/wav). Süre uyuşmazsa video sese göre kesilir (cut_off)."""
    fal = _fal()
    res, _ = _call(fal, ENDPOINTS["lipsync"], {
        "video_url": fal.upload_file(video_path),
        "audio_url": fal.upload_file(audio_source_path),
        "sync_mode": "cut_off",
    })
    tmp = _download(res["video"]["url"])
    os.replace(tmp, out_path)
    return out_path
