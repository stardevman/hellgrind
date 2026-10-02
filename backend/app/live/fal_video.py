"""ÜCRETLİ video üretimi ve dudak senkronu (fal.ai). Bu modül hiçbir yerde çalıştırılmadı.

Akış: Flux LoRA ile ilk kare, sonra bu kareden Wan 2.2 ya da Seedance ile video. Her giriş noktası önce `guard("fal_key")`
çağırır (DRY_RUN=1 ya da boş anahtar: PaidCallBlocked); `fal_client` yalnızca fonksiyon içinde yüklenir.

Uç nokta kimlikleri ve parametreler fal.ai model sayfalarından doğrulandı (2 Ekim 2026):
  fal-ai/flux-lora                          prompt, image_size, seed, loras[{path, scale}], num_images, output_format
  fal-ai/wan/v2.2-a14b/image-to-video       prompt, image_url, num_frames 17-161, frames_per_second 4-60, resolution
                                            480p|580p|720p, negative_prompt, seed, aspect_ratio
  bytedance/seedance-2.0/fast/image-to-video  prompt, image_url, resolution 480p|720p, duration "auto" ya da 4-15,
                                            aspect_ratio, generate_audio (varsayılan true); TOHUM ALMAZ
  fal-ai/sync-lipsync/v2/pro                video_url, audio_url, sync_mode

Bütçe: iterasyon, sağlayıcının `estimate()` değeri kadar rezervasyon alır ve `generate()` sonucundaki `cost_usd`'yi işler.
Bu yüzden `estimate()` ilk kare dahil tüm üretimi kapsar. fal yanıtı fatura tutarı döndürmez: tutarlar pricing.yaml tahminidir.
"""
import logging
import math
import os
import threading
import time
import uuid

import httpx

from app.budget import pricing
from app.config import settings
from app.core.hashing import stable_hash
from app.engine.providers import GenRequest, GenResult, TransientError
from app.live import guard

log = logging.getLogger("hellgrind.live.fal")

FLUX = "fal-ai/flux-lora"
WAN = "fal-ai/wan/v2.2-a14b/image-to-video"
SEEDANCE = "bytedance/seedance-2.0/fast/image-to-video"
LIPSYNC = "fal-ai/sync-lipsync/v2/pro"

WAN_FPS = 16
WAN_FRAMES = (17, 161)
WAN_RESOLUTIONS = ("480p", "580p", "720p")
SEEDANCE_SECONDS = (4, 15)                 # Seedance yalnızca "auto" ya da 4-15 sn kabul eder; 3 sn'lik çekim geçersiz olurdu
SEEDANCE_RESOLUTIONS = ("480p", "720p")
FIRST_FRAME_MEGAPIXELS = 1.0               # landscape_16_9 ~0,59 MP; fiyat MP başına olduğundan tutucu: 1 MP
QUEUE_START_TIMEOUT_S = 900
DOWNLOAD_ATTEMPTS = 3


def _client(key: str):
    import fal_client                       # ücretli paket: yalnızca fonksiyon içinde
    return fal_client.SyncClient(key=key)


def is_transient(exc: BaseException) -> bool:
    """429, 5xx ve zaman aşımı yeniden denenebilir. fal-client'ın FalClientHTTPError'u `status_code` taşır;
    FalClientTimeoutError bir TimeoutError'dur."""
    status = getattr(exc, "status_code", None)
    if status is None:                                  # httpx.HTTPStatusError durumu response içinde taşır
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return isinstance(exc, (TimeoutError, httpx.TransportError))


def _classify(exc: BaseException) -> BaseException:
    if isinstance(exc, TransientError) or not is_transient(exc):
        return exc
    return TransientError(f"{type(exc).__name__}: {exc}")


def _reraise(exc: BaseException):
    """Geçici hatayı TransientError'a çevirerek, diğerlerini olduğu gibi yükseltir."""
    classified = _classify(exc)
    if classified is exc:
        raise exc
    raise classified from exc


def _run(client, application: str, arguments: dict) -> tuple[dict, str]:
    handle = client.submit(application, arguments=arguments, start_timeout=QUEUE_START_TIMEOUT_S)
    return handle.get(), handle.request_id


def _download(url: str) -> str:
    """Üretilmiş videoyu indirir. İndirme de yeniden denenir: tek bir ağ kesintisi ödenmiş üretimi kaybettirmemeli."""
    os.makedirs(settings.work_dir, exist_ok=True)
    path = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
    last: BaseException | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
                response.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
            return path
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            if os.path.exists(path):
                os.remove(path)
            if isinstance(e, httpx.HTTPStatusError) and not is_transient(e):
                raise                                        # süresi dolmuş bağlantı gibi: yeniden denemek işe yaramaz
            log.warning("indirme hatası (deneme %d/%d): %s", attempt, DOWNLOAD_ATTEMPTS, e)
            time.sleep(min(2 ** attempt, 8))
    raise TransientError(f"video indirilemedi: {last}")


class FalProvider:
    """Yerel sağlayıcıyla aynı arayüz: `engine`, `estimate(req)`, `generate(req)`."""

    def __init__(self, engine: str):
        if engine not in ("fal_wan", "seedance"):
            raise NotImplementedError(f"{engine} için ücretli sağlayıcı henüz bağlı değil")
        self.engine = engine
        self._lock = threading.Lock()
        self._frames: dict[str, str] = {}                  # istek -> ilk kare URL'si
        self._videos: dict[str, tuple[str, str]] = {}      # istek -> (video URL'si, fal istek kimliği)

    # ---------- süre, çözünürlük, maliyet ----------

    def frames(self, duration_s: float) -> int:
        return min(WAN_FRAMES[1], max(WAN_FRAMES[0], round(duration_s * WAN_FPS)))

    def billed_seconds(self, duration_s: float) -> float:
        """Gerçekten üretilen ve faturalanan süre. Çekim süresinden farklı olabilir: Seedance en az 4 sn üretir."""
        if self.engine == "fal_wan":
            return self.frames(duration_s) / WAN_FPS
        return float(min(SEEDANCE_SECONDS[1], max(SEEDANCE_SECONDS[0], math.ceil(duration_s))))

    def resolution(self, requested: str) -> str:
        allowed = WAN_RESOLUTIONS if self.engine == "fal_wan" else SEEDANCE_RESOLUTIONS
        return requested if requested in allowed else "720p"

    def estimate(self, req: GenRequest) -> float:
        video = pricing.estimate_video(self.engine, self.resolution(req.resolution), self.billed_seconds(req.duration_s))
        image = 0.0 if req.image_url else pricing.estimate_image(FIRST_FRAME_MEGAPIXELS)
        return round(video + image, 4)

    # ---------- üretim ----------

    def _key(self, req: GenRequest) -> str:
        x = req.extra
        return stable_hash([self.engine, req.prompt, req.seed, req.resolution, req.duration_s, x.get("loras"), x.get("lora_triggers")])

    def _first_frame(self, client, req: GenRequest) -> str:
        if req.image_url:
            return req.image_url
        key = self._key(req)
        with self._lock:
            if key in self._frames:                          # yeniden deneme: ikinci kez ilk kare ödenmez
                return self._frames[key]
        triggers = [t for t in req.extra.get("lora_triggers") or [] if t]
        args = {"prompt": ", ".join([*triggers, req.prompt]), "image_size": "landscape_16_9", "num_images": 1,
                "output_format": "jpeg", "enable_safety_checker": True, "seed": req.seed}
        loras = [{"path": l["path"], "scale": l.get("scale", 1.0)} for l in req.extra.get("loras") or []]
        if loras:
            args["loras"] = loras                            # karakter LoRA'ları ilk kareye ulaşır
        result, _ = _run(client, FLUX, args)
        url = result["images"][0]["url"]
        with self._lock:
            self._frames[key] = url
        return url

    def video_arguments(self, req: GenRequest, image_url: str) -> tuple[str, dict]:
        resolution = self.resolution(req.resolution)
        if self.engine == "fal_wan":
            return WAN, {"prompt": req.prompt, "image_url": image_url, "num_frames": self.frames(req.duration_s),
                         "frames_per_second": WAN_FPS, "resolution": resolution, "negative_prompt": req.negative,
                         "seed": req.seed, "aspect_ratio": "16:9", "enable_safety_checker": True}
        # Seedance tohum almaz; kendi sesimizi ekleyeceğimizden yerleşik ses kapatılır (varsayılan açık).
        return SEEDANCE, {"prompt": req.prompt, "image_url": image_url, "resolution": resolution,
                          "duration": str(int(self.billed_seconds(req.duration_s))), "aspect_ratio": "16:9",
                          "generate_audio": False}

    def generate(self, req: GenRequest) -> GenResult:
        key = self._key(req)
        client = _client(guard("fal_key"))
        try:
            with self._lock:
                made = self._videos.get(key)
            if made is None:                                 # daha önce üretilmiş ama indirilememiş video yeniden üretilmez
                image_url = self._first_frame(client, req)
                application, args = self.video_arguments(req, image_url)
                result, request_id = _run(client, application, args)
                made = (result["video"]["url"], request_id)
                with self._lock:
                    self._videos[key] = made
            path = _download(made[0])
        except Exception as e:
            _reraise(e)
        with self._lock:
            self._frames.pop(key, None)
            self._videos.pop(key, None)
        return GenResult(local_path=path, request_id=made[1], cost_usd=self.estimate(req), engine=self.engine,
                         meta={"billed_seconds": self.billed_seconds(req.duration_s), "seed_sent": self.engine == "fal_wan"})


def get_live_provider(engine: str) -> FalProvider:
    guard("fal_key")                        # sağlayıcı kurulurken: engelliyse hiçbir varyasyon satırı ya da rezervasyon oluşmaz
    return FalProvider(engine)


def lipsync(video_path: str, audio_path: str, out_path: str) -> str:
    """Videoyu sesle eşler (fal-ai/sync-lipsync/v2/pro). Çağıran, `pricing.estimate_lipsync` kadar rezervasyon almış olmalı."""
    client = _client(guard("fal_key"))
    try:
        video_url, audio_url = client.upload_file(video_path), client.upload_file(audio_path)
        result, _ = _run(client, LIPSYNC, {"video_url": video_url, "audio_url": audio_url, "sync_mode": "cut_off"})
        downloaded = _download(result["video"]["url"])
    except Exception as e:
        _reraise(e)
    os.replace(downloaded, out_path)
    return out_path
