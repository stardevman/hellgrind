"""ÜCRETLİ video üretimi ve dudak senkronu (fal.ai). Bu modül hiçbir yerde çalıştırılmadı.

Varsayılan video motoru MiniMax H3'tür (app/live/vast_h3.py, yönetmenin kendi GPU sunucusu). fal.ai isteğe bağlıdır:
yönetmenin Seedance seçtiği çekimler, eski Wan çekimleri, Flux storyboard ve referans kareleri, previs aktarımı ve dudak
senkronu.

Akış: Flux LoRA ile ilk kare, sonra bu kareden Seedance (ya da eski çekimlerde Wan 2.2) ile video. Önce görsel, sonra video: iterasyon
ilk kareyi `first_frame()` ile ayrı üretip puanlar; video yalnızca eşiği geçen kareden (`image_url`) üretilir.
Her giriş noktası önce `guard("fal_key")` çağırır (DRY_RUN=1 ya da boş anahtar: PaidCallBlocked); `fal_client` yalnızca
fonksiyon içinde yüklenir.

Karakter referansı: çekimdeki bir karakterin durağan referans görseli varsa (ve LoRA'sı yoksa) ilk kare ve storyboard
karesi Flux Kontext ile o görselden çizilir; karakterin yüzü, saçı ve kıyafeti korunur, sahne prompt'tan gelir.

Bölüm B bağlantıları: storyboard karesi ve mekân/nesne referansı tek bir Flux karesidir (`image()`); karakter dönüş
videosu Flux karesinden yörünge hareketli Wan videosudur (`FalProvider.generate`); previs rehberi Wan 2.2 video-to-video
ile stile aktarılır (`restyle()`); düzenlenmiş ilk kare yüklenip (`upload()`) videoya çevrilir.

Uç nokta kimlikleri ve parametreler fal.ai model sayfalarından doğrulandı (2 Ekim 2026):
  fal-ai/flux-lora                          prompt, image_size, seed, loras[{path, scale}], num_images, output_format
  fal-ai/wan/v2.2-a14b/image-to-video       prompt, image_url, num_frames 17-161, frames_per_second 4-60, resolution
                                            480p|580p|720p, negative_prompt, seed, aspect_ratio
  bytedance/seedance-2.0/fast/image-to-video  prompt, image_url, resolution 480p|720p, duration "auto" ya da 4-15,
                                            aspect_ratio, generate_audio (varsayılan true); TOHUM ALMAZ
  fal-ai/sync-lipsync/v2/pro                video_url, audio_url, sync_mode
  fal-ai/flux-pro/kontext                   prompt, image_url, seed, aspect_ratio (16:9...), num_images, output_format,
                                            guidance_scale, safety_tolerance; 0,04 USD/görsel (5 Ekim 2026)
  fal-ai/wan/v2.2-a14b/video-to-video       video_url, prompt, strength 0-1 (varsayılan 0,9), num_frames 17-161,
                                            frames_per_second 4-60, resolution 480p|580p|720p, negative_prompt, seed,
                                            aspect_ratio; 0,04/0,06/0,08 USD/sn (16 fps'de) (3 Ekim 2026)

Bütçe: iterasyon, sağlayıcının `estimate()` değeri kadar rezervasyon alır ve `generate()` sonucundaki `cost_usd`'yi işler.
Bu yüzden `estimate()` ilk kare dahil tüm üretimi kapsar. fal yanıtı fatura tutarı döndürmez: tutarlar pricing.yaml tahminidir.
"""
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

import httpx

from app.budget import history, pricing
from app.config import settings
from app.core.hashing import stable_hash
from app.engine.providers import GenRequest, GenResult, TransientError
from app.live import PaidCallBlocked, guard

log = logging.getLogger("hellgrind.live.fal")

# Varsayılan uç noktalar (asıl değerler settings.fal_*_endpoint: .env ya da panelin Ayarlar sayfası)
FLUX = "fal-ai/flux-lora"
WAN = "fal-ai/wan/v2.2-a14b/image-to-video"
SEEDANCE = "bytedance/seedance-2.0/fast/image-to-video"
LIPSYNC = "fal-ai/sync-lipsync/v2/pro"
WAN_V2V = "fal-ai/wan/v2.2-a14b/video-to-video"
KONTEXT = "fal-ai/flux-pro/kontext"

WAN_FPS = 16
WAN_FRAMES = (17, 161)
WAN_RESOLUTIONS = ("480p", "580p", "720p")
SEEDANCE_SECONDS = (4, 15)                 # Seedance yalnızca "auto" ya da 4-15 sn kabul eder; 3 sn'lik çekim geçersiz olurdu
SEEDANCE_RESOLUTIONS = ("480p", "720p")
FIRST_FRAME_MEGAPIXELS = 1.0               # landscape_16_9 ~0,59 MP; fiyat MP başına olduğundan tutucu: 1 MP
V2V_STRENGTH = 0.65                        # previs: hareket ve kompozisyon rehberden, görünüm stilden (0,9 prompt'a fazla yaslanır)
QUEUE_START_TIMEOUT_S = 900
DOWNLOAD_ATTEMPTS = 3


def _client(key: str):
    from app.live import require_package
    require_package("fal_client")
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


CHARGE_NOTE = ("Yalnızca reddedilen bu çağrı ücretlendirilmedi; bu çekimde daha önce tamamlanan çağrılar ücretlendi ve "
               "harcamaya yazıldı (kesin tutar fal.ai faturasındadır).")


def account_problem(exc: BaseException) -> str | None:
    """Hesap kaynaklı, yeniden denemeyle düzelmeyen hatalar için yönetmene Türkçe açıklama (bakiye, anahtar)."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    if "balance" in text or "billing" in text or status == 402:
        return ("fal.ai bakiyesi bitti: fal.ai/dashboard/billing adresinden bakiye yükleyin, sonra çekimi yeniden üretin. "
                f"{CHARGE_NOTE}")
    if status in (401, 403):
        return ("fal.ai anahtarı reddedildi (yetki yok ya da hesap kilitli): Ayarlar › fal.ai'den anahtarı ve "
                f"\"Bağlantıyı sına\" sonucunu kontrol edin. {CHARGE_NOTE}")
    return None


def _classify(exc: BaseException) -> BaseException:
    if (reason := account_problem(exc)) is not None:
        return PaidCallBlocked(reason)
    if isinstance(exc, TransientError) or not is_transient(exc):
        return exc
    return TransientError(f"{type(exc).__name__}: {exc}")


def _reraise(exc: BaseException):
    """Geçici hatayı TransientError'a çevirerek, diğerlerini olduğu gibi yükseltir."""
    classified = _classify(exc)
    if classified is exc:
        raise exc
    raise classified from exc


# Servis geçmişine yazılan istek parametreleri: neyin üretildiğini anlatanlar. Adresler (image_url, video_url...) yazılmaz.
NOTED_ARGUMENTS = ("resolution", "num_frames", "frames_per_second", "duration", "image_size", "num_images", "seed",
                   "strength", "sync_mode", "aspect_ratio", "generate_audio")
NOTED_PROMPT_CHARS = 400


def noted(arguments: dict) -> dict:
    info = {k: arguments[k] for k in NOTED_ARGUMENTS if k in arguments}
    if arguments.get("loras"):
        info["loras"] = [{"path": l["path"], "scale": l.get("scale")} for l in arguments["loras"]]
    if arguments.get("prompt"):
        info["prompt"] = arguments["prompt"][:NOTED_PROMPT_CHARS]
    return info


def _run(client, application: str, arguments: dict) -> tuple[dict, str]:
    handle = client.submit(application, arguments=arguments, start_timeout=QUEUE_START_TIMEOUT_S)
    # Kuyruğa giren istek kayda hemen geçer: sonucu alınamasa da fal tarafında ücretlenmiş olabilir.
    history.step(application, handle.request_id, **noted(arguments))
    return handle.get(), handle.request_id


def _download(url: str, suffix: str = ".mp4") -> str:
    """Üretilmiş dosyayı indirir. İndirme de yeniden denenir: tek bir ağ kesintisi ödenmiş üretimi kaybettirmemeli."""
    os.makedirs(settings.work_dir, exist_ok=True)
    path = os.path.join(settings.work_dir, f"{uuid.uuid4()}{suffix}")
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
    raise TransientError(f"dosya indirilemedi: {last}")


@dataclass
class Frame:
    """Üretilmiş tek kare: fal'daki adresi (videoya `image_url` olarak verilir), indirilmiş yerel kopyası ve bedeli."""
    url: str
    local_path: str
    request_id: str
    cost_usd: float


def flux_arguments(prompt: str, seed: int, loras=None, triggers=None, image_size: str = "landscape_16_9") -> dict:
    """Flux LoRA isteği: karakter LoRA'ları ve tetik sözcükleri kareye ulaşır."""
    args = {"prompt": ", ".join([*[t for t in triggers or [] if t], prompt]), "image_size": image_size, "num_images": 1,
            "output_format": "jpeg", "enable_safety_checker": True, "seed": seed}
    loras = [{"path": l["path"], "scale": l.get("scale", 1.0)} for l in loras or []]
    if loras:
        args["loras"] = loras
    return args


def kontext_prompt(prompt: str, name: str) -> str:
    """Kontext bir düzenleme modelidir: karakteri koruyup sahneyi değiştirmesi açıkça söylenir."""
    return (f"The character in the reference image is {name}. Keep {name}'s face, hair, eyes, proportions and outfit exactly "
            f"as in the reference image; change only the scene, pose, framing and lighting as described. {prompt}")


def kontext_arguments(prompt: str, image_url: str, seed: int, name: str) -> dict:
    return {"prompt": kontext_prompt(prompt, name), "image_url": image_url, "seed": seed, "aspect_ratio": "16:9",
            "num_images": 1, "output_format": "jpeg"}


_reference_urls: dict[str, str] = {}
_reference_lock = threading.Lock()


def reference_url(client, key: str) -> str:
    """Karakterin referans görseli fal deposuna bir kez yüklenir (yükleme ücretsizdir). Anahtar içerik-adreslidir: süreç
    içi önbellek güvenlidir."""
    from app import storage
    with _reference_lock:                    # dalga iş parçacıklıdır: aynı görsel yalnızca bir kez yüklensin
        if key not in _reference_urls:
            os.makedirs(settings.work_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                path = storage.download(key, os.path.join(tmp, "reference" + os.path.splitext(key)[1]))
                _reference_urls[key] = client.upload_file(path)
        return _reference_urls[key]


def _frame(client, prompt: str, seed: int, loras=None, triggers=None, reference: dict | None = None,
           image_size: str = "landscape_16_9") -> tuple[dict, str]:
    """Tek kare isteği: karakter referansı varsa Kontext (görselden), yoksa Flux LoRA (metinden)."""
    if reference:
        image_url = reference_url(client, reference["key"])
        return _run(client, settings.fal_kontext_endpoint, kontext_arguments(prompt, image_url, seed, reference["name"]))
    return _run(client, settings.fal_flux_endpoint, flux_arguments(prompt, seed, loras, triggers, image_size))


def image_estimate(megapixels: float = FIRST_FRAME_MEGAPIXELS, reference: bool = False) -> float:
    return pricing.estimate_frame(reference, megapixels)


def image(prompt: str, seed: int, loras=None, triggers=None, image_size: str = "landscape_16_9",
          reference: dict | None = None) -> Frame:
    """Tek kare (storyboard, mekân ve nesne referansı). Çağıran, `image_estimate(reference=...)` kadar rezervasyon almış
    olmalı. reference: {"name", "key"}: karakterin durağan referans görseli (Kontext)."""
    client = _client(guard("fal_key"))
    try:
        result, request_id = _frame(client, prompt, seed, loras, triggers, reference, image_size)
        url = result["images"][0]["url"]
        path = _download(url, ".jpg")
    except Exception as e:
        _reraise(e)
    return Frame(url=url, local_path=path, request_id=request_id, cost_usd=image_estimate(reference=bool(reference)))


def upload(path: str) -> str:
    """Yerel dosyayı fal deposuna yükler (ücretsiz; üretim isteğine adres olarak verilir)."""
    client = _client(guard("fal_key"))
    try:
        return client.upload_file(path)
    except Exception as e:
        _reraise(e)


def v2v_frames(duration_s: float) -> int:
    return min(WAN_FRAMES[1], max(WAN_FRAMES[0], round(duration_s * WAN_FPS)))


def restyle_arguments(video_url: str, prompt: str, negative: str, seed: int, resolution: str, duration_s: float) -> dict:
    return {"video_url": video_url, "prompt": prompt, "negative_prompt": negative, "seed": seed,
            "strength": V2V_STRENGTH, "num_frames": v2v_frames(duration_s), "frames_per_second": WAN_FPS,
            "resolution": resolution if resolution in WAN_RESOLUTIONS else "720p", "aspect_ratio": "16:9",
            "enable_safety_checker": True}


def restyle(video_path: str, prompt: str, negative: str, seed: int, resolution: str, duration_s: float) -> tuple[str, str]:
    """Previs rehberini stile aktarır (Wan 2.2 video-to-video). (yerel video, fal istek kimliği). Çağıran,
    `methods.restyle_estimate()` kadar rezervasyon almış olmalı."""
    client = _client(guard("fal_key"))
    try:
        video_url = client.upload_file(video_path)
        result, request_id = _run(client, settings.fal_wan_v2v_endpoint, restyle_arguments(video_url, prompt, negative, seed, resolution, duration_s))
        path = _download(result["video"]["url"])
    except Exception as e:
        _reraise(e)
    return path, request_id


class FalProvider:
    """Yerel sağlayıcıyla aynı arayüz: `engine`, `estimate(req)`, `generate(req)`. Ek olarak `image_first`: iterasyon ilk
    kareyi `first_frame(req)` ile ayrı üretip puanlar, videoyu yalnızca geçen kareden ister."""

    image_first = True
    key_name = "fal_key"

    def __init__(self, engine: str):
        if engine not in ("fal_wan", "seedance"):
            raise NotImplementedError(f"{engine} için ücretli sağlayıcı henüz bağlı değil")
        self.engine = engine
        self._lock = threading.Lock()
        self._frames: dict[str, tuple[str, str]] = {}      # istek -> (ilk kare URL'si, fal istek kimliği)
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
        image = 0.0 if req.image_url else self.estimate_frame(req)
        return round(video + image, 4)

    def estimate_frame(self, req: GenRequest | None = None) -> float:
        return image_estimate(reference=bool(req and req.extra.get("reference")))

    # ---------- üretim ----------

    def _key(self, req: GenRequest) -> str:
        x = req.extra
        return stable_hash([self.engine, req.prompt, req.seed, req.resolution, req.duration_s, x.get("loras"), x.get("lora_triggers"),
                            x.get("reference")])

    def _first_frame(self, client, req: GenRequest) -> tuple[str, str | None]:
        if req.image_url:
            return req.image_url, None
        key = self._key(req)
        with self._lock:
            if key in self._frames:                          # yeniden deneme: ikinci kez ilk kare ödenmez
                url, request_id = self._frames[key]
                model = settings.fal_kontext_endpoint if req.extra.get("reference") else settings.fal_flux_endpoint
                history.step(model, request_id, reused=True)
                return url, request_id
        result, request_id = _frame(client, req.prompt, req.seed, req.extra.get("loras"), req.extra.get("lora_triggers"),
                                    req.extra.get("reference"))
        made = (result["images"][0]["url"], request_id)
        with self._lock:
            self._frames[key] = made
        return made

    def first_frame(self, req: GenRequest) -> Frame:
        """Önce görsel: yalnızca ilk kare (Flux LoRA), puanlanmak üzere indirilir. Çağıran `estimate_frame()` kadar
        rezervasyon almış olmalı. İndirme başarısız olursa yeniden deneme kareyi yeniden ödemez."""
        client = _client(guard("fal_key"))
        key = self._key(req)
        try:
            url, request_id = self._first_frame(client, req)
            path = _download(url, ".jpg")
        except Exception as e:
            _reraise(e)
        with self._lock:
            self._frames.pop(key, None)
        return Frame(url=url, local_path=path, request_id=request_id or "", cost_usd=self.estimate_frame(req))

    def upload(self, path: str) -> str:
        return upload(path)

    def video_arguments(self, req: GenRequest, image_url: str) -> tuple[str, dict]:
        resolution = self.resolution(req.resolution)
        if self.engine == "fal_wan":
            return settings.fal_wan_endpoint, {"prompt": req.prompt, "image_url": image_url, "num_frames": self.frames(req.duration_s),
                         "frames_per_second": WAN_FPS, "resolution": resolution, "negative_prompt": req.negative,
                         "seed": req.seed, "aspect_ratio": "16:9", "enable_safety_checker": True}
        # Seedance tohum almaz; kendi sesimizi ekleyeceğimizden yerleşik ses kapatılır (varsayılan açık).
        return settings.fal_seedance_endpoint, {"prompt": req.prompt, "image_url": image_url, "resolution": resolution,
                          "duration": str(int(self.billed_seconds(req.duration_s))), "aspect_ratio": "16:9",
                          "generate_audio": False}

    def generate(self, req: GenRequest) -> GenResult:
        key = self._key(req)
        client = _client(guard("fal_key"))
        try:
            with self._lock:
                made = self._videos.get(key)
            if made is None:                                 # daha önce üretilmiş ama indirilememiş video yeniden üretilmez
                image_url, _ = self._first_frame(client, req)
                application, args = self.video_arguments(req, image_url)
                result, request_id = _run(client, application, args)
                made = (result["video"]["url"], request_id, application)
                with self._lock:
                    self._videos[key] = made
            else:
                history.step(made[2], made[1], reused=True)
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
        result, _ = _run(client, settings.fal_lipsync_endpoint, {"video_url": video_url, "audio_url": audio_url, "sync_mode": "cut_off"})
        downloaded = _download(result["video"]["url"])
    except Exception as e:
        _reraise(e)
    os.replace(downloaded, out_path)
    return out_path
