"""ÜCRETLİ video üretimi: MiniMax H3, yönetmenin kendi GPU sunucusunda (ör. vast.ai). Bu modül hiçbir yerde çalıştırılmadı.

Sunucu SGLang'in OpenAI uyumlu, eşzamansız video ucudur (docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3, Ekim 2026):
  POST /v1/videos               {model, prompt, seconds, task, conditions, target, num_inference_steps, seed, ...} -> {id}
  GET  /v1/videos/{id}          status: queued | completed | failed; tamamlanınca inference_time_s (GPU süresi)
  GET  /v1/videos/{id}/content  MP4: H.264 24 fps + AAC 32 kHz stereo (yerleşik ses montajda atılır)
  GET  /health                  hazır mı (bağlantı sınaması, app/live/probe.py; üretim yapmaz)

Görevler: t2va (yalnızca metin: test ve tam tur), fl2va (verilen ilk kareden: hareket testi, düzenlenmiş kare) ve ref2va
(çekimdeki karakterlerin durağan referans görselleriyle; ayrı bir Ref2VA sunucusu ister, adresi boşsa referans gitmez).
Görseller isteğin içinde data URI olarak gider (SGLang destekler); worker'ın deposu sunucuya açılmaz.

Model CFG damıtılmıştır, negatif prompt almaz: stil kılavuzunun negatifleri gönderilmez (servis geçmişinde not edilir).
Süre 4-15 sn arasıdır: daha kısa çekim 4 sn üretilir ve indirilince çekim süresine kesilir. Kısa kenar 480p için 480, diğer
her istek için açık modelin doğal çözünürlüğü olan 768 (2K yalnızca MiniMax'in kendi API'sindedir).

Her giriş noktası önce `guard("h3_api_key")` çağırır (DRY_RUN=1 ya da boş anahtar: PaidCallBlocked). Anahtar, sunucunun
önündeki kimlik doğrulamanın belirtecidir: vast.ai Instance Portal `Authorization: Bearer <OPEN_BUTTON_TOKEN>` kabul eder.

Bütçe: vast.ai makineyi saatle faturalar. Rezervasyon = saatlik ücret × video saniyesi başına GPU süresi × süre
(pricing.yaml `self_hosted.h3`); işlenen tutar = saatlik ücret × sunucunun bildirdiği GPU süresi. Makinenin boşta geçen
süresi bütçeye yansımaz. Gönderilmiş bir işin kimliği saklanır: zaman aşımı ya da indirme hatasından sonraki deneme aynı
işi bekler, GPU'da ikinci kez üretmez.
"""
import base64
import io
import logging
import math
import os
import subprocess
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

log = logging.getLogger("hellgrind.live.h3")

ENGINE = "h3"
KEY = "h3_api_key"
SECONDS = (4, 15)                    # açık model 4-15 sn üretir (24 fps)
SHORT_EDGE = {"480p": 480}           # diğer her istek 768: açık modelin doğal kısa kenarı
NATIVE_EDGE = 768
ASPECT_RATIO = "16:9"
FLOW_SHIFT, AUDIO_FLOW_SHIFT = 12.0, 3.0      # SGLang tarifindeki varsayılanlar
MAX_REFERENCES = 9                   # Ref2VA en çok 9 görsel alır
IMAGE_EDGE = 1536                    # data URI'ye konan görselin uzun kenarı: istek gövdesi küçük kalsın
POLL_S = 5.0
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
DOWNLOAD_ATTEMPTS = 3
RETRY_BASE_S = 2.0                   # indirme yeniden denemesinde bekleme: 2, 4, 8 sn
NOTED_PROMPT_CHARS = 400
TRANSPORT: httpx.BaseTransport | None = None  # testler sahte sunucu bağlar; gerçek ağ yalnızca ücretli modda kullanılır


# ---------- hatalar ----------

class JobFailed(TransientError):
    """Sunucu işi başarısız bildirdi (ör. bellek yetmedi): yeni bir gönderimle yeniden denenebilir."""


def _status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_transient(exc: BaseException) -> bool:
    """429, 5xx ve zaman aşımı yeniden denenebilir. Bağlanamamak ve tünelin kapalı olması (530) hesap sorunudur."""
    status = _status(exc)
    if status is not None:
        return status == 429 or (status >= 500 and status != 530)
    return isinstance(exc, (TimeoutError, httpx.TransportError)) and not isinstance(exc, httpx.ConnectError)


def account_problem(exc: BaseException) -> str | None:
    """Yeniden denemeyle düzelmeyen, yönetmenin elinde olan sorunlar için Türkçe açıklama."""
    status = _status(exc)
    if status in (401, 403):
        return ("H3 sunucusu erişim anahtarını reddetti: Ayarlar › MiniMax H3'te anahtarı (vast.ai OPEN_BUTTON_TOKEN ya da "
                "WEB_PASSWORD) ve \"Bağlantıyı sına\" sonucunu kontrol edin.")
    if status == 530 or isinstance(exc, httpx.ConnectError):
        return ("H3 sunucusuna bağlanılamadı: vast.ai'de makine çalışıyor mu ve sunucu adresi güncel mi? (Cloudflare tünel "
                "adresi makine her yeniden başladığında değişir.) Bu çekimde tamamlanmış işlerin GPU süresi harcamaya yazıldı.")
    return None


def _classify(exc: BaseException) -> BaseException:
    if (reason := account_problem(exc)) is not None:
        return PaidCallBlocked(reason)
    if isinstance(exc, TransientError):
        return exc
    if is_transient(exc):
        return TransientError(f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        detail = (exc.response.text or "").strip()[:300]
        return RuntimeError(f"H3 sunucusu isteği reddetti (HTTP {exc.response.status_code}): {detail}")
    return exc


def _reraise(exc: BaseException):
    classified = _classify(exc)
    if classified is exc:
        raise exc
    raise classified from exc


# ---------- istek ----------

def seconds_for(duration_s: float) -> int:
    """Üretilen ve GPU'da harcanan süre: 4-15 sn, tam saniye."""
    return int(min(SECONDS[1], max(SECONDS[0], math.ceil(duration_s - 1e-6))))


def short_edge(resolution: str) -> int:
    return SHORT_EDGE.get(resolution, NATIVE_EDGE)


def priced_resolution(resolution: str) -> str:
    """pricing.yaml'daki satır: 480p ya da 720p (768p üretilir, filmin 720p kademesine karşılık gelir)."""
    return "480p" if short_edge(resolution) == 480 else "720p"


def data_uri(path: str) -> str:
    """Görseli JPEG'e çevirip (uzun kenar en çok IMAGE_EDGE) data URI yapar."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


_reference_uris: dict[str, str] = {}
_reference_lock = threading.Lock()


def reference_uri(key: str) -> str:
    """Depodaki referans görseli bir kez indirip data URI'ye çevirir. Anahtar içerik-adreslidir: önbellek güvenlidir."""
    import tempfile
    from app import storage
    with _reference_lock:                        # dalga iş parçacıklıdır: aynı görsel bir kez çevrilsin
        if key not in _reference_uris:
            os.makedirs(settings.work_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                path = storage.download(key, os.path.join(tmp, "reference" + os.path.splitext(key)[1]))
                _reference_uris[key] = data_uri(path)
        return _reference_uris[key]


@dataclass
class Job:
    url: str            # sunucu adresi (FL2VA ya da Ref2VA)
    task: str           # t2va | fl2va | ref2va
    body: dict
    references: list[str]


def references_of(req: GenRequest) -> list[dict]:
    """Çekimdeki karakterlerin durağan referans görselleri ({"name", "key"}); Ref2VA sunucusu yoksa gönderilmez."""
    if not settings.h3_ref_endpoint_url:
        return []
    return [r for r in req.extra.get("stills") or [] if r.get("key")][:MAX_REFERENCES]


def reference_prompt(prompt: str, names: list[str]) -> str:
    """Ref2VA, görselleri istemdeki <Picture n> etiketiyle eşler (modalite içinde bir tabanlı sıra)."""
    tags = ", ".join(f"<Picture {i}> is {name}" for i, name in enumerate(names, start=1))
    return f"Character references: {tags}. Keep each character's face, hair, proportions and outfit as in their picture. {prompt}"


def build_job(req: GenRequest) -> Job:
    refs = references_of(req)
    keyframe = [{"type": "image", "uri": req.image_url, "role": "keyframe", "frame_index": 0}] if req.image_url else []
    if refs:
        conditions = [{"type": "image", "uri": reference_uri(r["key"]), "role": "reference"} for r in refs] + keyframe
        url, task, prompt = settings.h3_ref_endpoint_url, "ref2va", reference_prompt(req.prompt, [r["name"] for r in refs])
    elif keyframe:
        url, task, prompt, conditions = settings.h3_endpoint_url, "fl2va", req.prompt, keyframe
    else:
        url, task, prompt, conditions = settings.h3_endpoint_url, "t2va", req.prompt, []
    seconds = seconds_for(req.duration_s)
    body = {"model": settings.h3_model, "prompt": prompt, "seconds": seconds, "task": task, "conditions": conditions,
            "target": {"short_edge": short_edge(req.resolution), "aspect_ratio": ASPECT_RATIO,
                       "duration_seconds": float(seconds)},
            "num_outputs_per_prompt": 1, "num_inference_steps": int(settings.h3_steps),
            "flow_shift": FLOW_SHIFT, "audio_flow_shift": AUDIO_FLOW_SHIFT, "seed": req.seed}
    return Job(url=url.rstrip("/"), task=task, body=body, references=[r["name"] for r in refs])


def _client(url: str, token: str) -> httpx.Client:
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
                        transport=TRANSPORT, follow_redirects=True)


def _submit(client: httpx.Client, job: Job, negative: str) -> str:
    r = client.post("/v1/videos", json=job.body)
    r.raise_for_status()
    job_id = r.json()["id"]
    # Kuyruğa giren iş kayda hemen geçer: sonucu alınamasa da GPU'da çalışmış olabilir.
    history.step(settings.h3_model, job_id, task=job.task, resolution=f"{job.body['target']['short_edge']}p",
                 duration=job.body["seconds"], seed=job.body["seed"], steps=job.body["num_inference_steps"],
                 references=len(job.references) or None, negative_dropped=bool(negative) or None,
                 prompt=job.body["prompt"][:NOTED_PROMPT_CHARS])
    return job_id


def _wait(client: httpx.Client, job_id: str) -> dict:
    deadline = time.monotonic() + float(settings.h3_timeout_s)
    while True:
        r = client.get(f"/v1/videos/{job_id}")
        r.raise_for_status()
        job = r.json()
        status = job.get("status")
        if status == "completed":
            return job
        if status in ("failed", "deleted"):
            message = (job.get("error") or {}).get("message") or status
            raise JobFailed(f"H3 işi başarısız ({job_id}): {str(message)[:300]}")
        if time.monotonic() >= deadline:
            raise TransientError(f"H3 işi {settings.h3_timeout_s} sn içinde bitmedi ({job_id}, durum: {status})")
        time.sleep(POLL_S)


def _download(client: httpx.Client, job_id: str) -> str:
    """Videoyu indirir; ağ kesintisinde yeniden dener (iş sunucuda durur, yeniden üretilmez)."""
    os.makedirs(settings.work_dir, exist_ok=True)
    path = os.path.join(settings.work_dir, f"{uuid.uuid4()}.mp4")
    last: BaseException | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with client.stream("GET", f"/v1/videos/{job_id}/content", timeout=DOWNLOAD_TIMEOUT) as response:
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
                raise
            log.warning("H3 indirme hatası (deneme %d/%d): %s", attempt, DOWNLOAD_ATTEMPTS, e)
            time.sleep(min(RETRY_BASE_S * 2 ** (attempt - 1), 8))
    raise TransientError(f"H3 videosu indirilemedi: {last}")


def trim(path: str, duration_s: float) -> str:
    """H3 en az 4 sn üretir: daha kısa çekimin klibi çekim süresine kesilir (fazlasının GPU süresi yine harcanmıştır)."""
    out = path[:-4] + "_cut.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-t", f"{duration_s:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", out], check=True, capture_output=True)
    os.replace(out, path)
    return path


# ---------- sağlayıcı ----------

class H3Provider:
    """Yerel sağlayıcıyla aynı arayüz: `engine`, `estimate(req)`, `generate(req)`, `upload(path)`. Önce görsel yoktur:
    video doğrudan üretilir ve kalite kapısı videoyu puanlar."""

    engine = ENGINE
    key_name = KEY
    image_first = False

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, str] = {}          # istek özeti -> iş kimliği (yeniden deneme aynı işi bekler)

    def billed_seconds(self, duration_s: float) -> float:
        return float(seconds_for(duration_s))

    def resolution(self, requested: str) -> str:
        return priced_resolution(requested)

    def estimate(self, req: GenRequest) -> float:
        return pricing.estimate_video(ENGINE, self.resolution(req.resolution), self.billed_seconds(req.duration_s))

    def upload(self, path: str) -> str:
        """İlk kare (hareket testi, düzenlenmiş kare): istekte data URI olarak gider; ayrı bir yükleme yoktur."""
        return data_uri(path)

    def generate(self, req: GenRequest) -> GenResult:
        token = guard(KEY)
        job = build_job(req)
        key = stable_hash([job.url, job.body])
        try:
            with _client(job.url, token) as client:
                with self._lock:
                    job_id = self._jobs.get(key)
                if job_id is None:
                    job_id = _submit(client, job, req.negative)
                    with self._lock:
                        self._jobs[key] = job_id
                else:
                    history.step(settings.h3_model, job_id, task=job.task, reused=True)
                try:
                    done = _wait(client, job_id)
                except JobFailed:
                    with self._lock:
                        self._jobs.pop(key, None)            # başarısız iş yeniden gönderilir
                    raise
                path = _download(client, job_id)
        except Exception as e:
            _reraise(e)
        with self._lock:
            self._jobs.pop(key, None)
        if self.billed_seconds(req.duration_s) - req.duration_s > 0.05:
            trim(path, req.duration_s)
        gpu = done.get("inference_time_s")
        measured = isinstance(gpu, (int, float)) and gpu > 0
        if measured:
            history.step(settings.h3_model, job_id, gpu_seconds=round(float(gpu), 1))
        cost = pricing.estimate_gpu(ENGINE, float(gpu)) if measured else self.estimate(req)
        return GenResult(local_path=path, request_id=job_id, cost_usd=cost, engine=ENGINE,
                         meta={"task": job.task, "billed_seconds": self.billed_seconds(req.duration_s),
                               "short_edge": job.body["target"]["short_edge"], "gpu_seconds": gpu if measured else None,
                               "references": job.references, "negative_dropped": bool(req.negative), "seed_sent": True})


def get_live_provider() -> H3Provider:
    """Sağlayıcı kurulurken kilit ve adres denetlenir: engelliyse hiçbir varyasyon satırı ya da rezervasyon oluşmaz."""
    guard(KEY)
    if not settings.h3_endpoint_url:
        raise PaidCallBlocked("H3 sunucu adresi tanımlı değil: Ayarlar › MiniMax H3'ten vast.ai sunucusunun adresini girin.")
    return H3Provider()
