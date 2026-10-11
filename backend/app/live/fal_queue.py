"""ÜCRETLİ: fal.ai kuyruğu (submit -> status -> result), yükleme önbelleği ve çıktının hemen indirilmesi.

Bu modül testlerde yalnızca sahte istemciyle çalışır. Her giriş noktası önce `guard("fal_key")` çağırır; `fal_client`
yalnızca fonksiyon içinde yüklenir (app/live/fal_video._client).

- **Sürdürme:** gönderilen işin kimliği (request_id) Redis'e (iş anahtarı: uç nokta + gövdenin özeti) ve çekime
  (`scenes.fal_request`) yazılır. Görev yeniden teslim edilirse (worker yeniden başladı) ya da panel kapanıp açılırsa aynı
  gövde yeniden gönderilmez: bekleyen iş request_id'den sürdürülür (ikinci kez ücretlenmez).
- **Hatalar:** 4xx (ör. 422 doğrulama) yeniden denenmez; fal'in mesajı olduğu gibi `FalRejected` ile döner ve denetçide
  gösterilir. Yalnızca 5xx'te iş en çok bir kez yeniden gönderilir. Zaman aşımında aynı iş bir süre daha beklenir (yeniden
  gönderilmez: fal'de süren iş yine ücretlenir), yine bitmezse iptal edilir.
- **Yükleme önbelleği:** yerel dosya fal deposuna bir kez yüklenir; SHA-256 -> adres Redis'te tutulur (süreçler arası).
- **Çıktı:** fal CDN adresine güvenilmez: sonuç gelir gelmez indirilir, boyutu doğrulanır (çağıran nesne deposuna yazar).
"""
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass

from app.budget import history, killswitch
from app.config import settings
from app.core.hashing import stable_hash
from app.engine.providers import ProviderError, TransientError
from app.live import PaidCallBlocked, guard

log = logging.getLogger("hellgrind.live.fal_queue")

POLL_S = 5.0
UPLOAD_TTL_S = 24 * 3600            # fal deposundaki dosyanın adresi bu süre yeniden kullanılır
JOB_TTL_S = 7 * 24 * 3600
JOB = "hg:fal:job:{}"               # iş anahtarı -> {"request_id", "endpoint", "submitted_at"}
UPLOAD = "hg:fal:upload:{}"         # sha256 -> fal deposu adresi
SUBMIT_RETRIES = 1                  # yalnızca 5xx: en çok bir yeniden gönderim
NOTED = ("resolution", "duration", "seed", "aspect_ratio", "generate_audio", "prompt_expansion_mode", "task", "draft",
         "num_inference_steps", "guidance_scale", "acceleration", "image_size")


class FalError(ProviderError):
    """fal.ai işi tamamlanamadı (yeniden denenmez; iterasyonun genel yeniden deneme döngüsüne girmez)."""


class FalRejected(FalError):
    """4xx: istek reddedildi (ör. 422 doğrulama). Mesaj fal'in döndürdüğü gibidir."""

    def __init__(self, status: int, message: str, endpoint: str):
        super().__init__(f"fal.ai isteği reddetti (HTTP {status}, {endpoint}): {message}")
        self.status, self.detail, self.endpoint = status, message, endpoint


class FalTimeout(FalError):
    """İş, zaman aşımı ve bir bekleme penceresi daha geçtiği hâlde bitmedi; iptal edildi."""


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _message_of(exc: BaseException) -> str:
    """fal'in hata gövdesi (422: {"detail": [...]}) olduğu gibi; yoksa istisnanın metni."""
    response = getattr(exc, "response", None)
    try:
        if response is not None:
            body = response.json()
            return json.dumps(body.get("detail", body), ensure_ascii=False) if isinstance(body, dict) else str(body)
    except Exception:                                   # noqa: BLE001 — gövde JSON değil
        pass
    return str(exc).strip() or type(exc).__name__


def classify(exc: BaseException, endpoint: str) -> BaseException:
    """Hesap sorunu (bakiye, anahtar) -> PaidCallBlocked; diğer 4xx -> FalRejected (yeniden denenmez); 5xx ve ağ ->
    TransientError (bir kez yeniden gönderilir)."""
    from app.live.fal_video import account_problem, is_transient
    if isinstance(exc, (FalError, PaidCallBlocked, TransientError, killswitch.ProjectFrozen)):
        return exc
    if (reason := account_problem(exc)) is not None:
        return PaidCallBlocked(reason)
    status = _status_of(exc)
    if status is not None and 400 <= status < 500:
        return FalRejected(status, _message_of(exc), endpoint)
    if is_transient(exc):
        return TransientError(f"{type(exc).__name__}: {exc}")
    return exc


# ---------- yükleme önbelleği ----------

_local_uploads: dict[str, str] = {}
_upload_lock = threading.Lock()


def _r():
    return killswitch.r()


def upload_cached(client, path: str) -> tuple[str, str, bool]:
    """(adres, sha256, önbellekten mi). Aynı içerik bir kez yüklenir (yükleme ücretsizdir ama yavaştır)."""
    from app.engine.fal_plan import sha256_of
    sha = sha256_of(path)
    with _upload_lock:
        url = _local_uploads.get(sha)
        if url is None:
            raw = _r().get(UPLOAD.format(sha))
            url = raw.decode() if isinstance(raw, bytes) else raw
        if url:
            _local_uploads[sha] = url
            return url, sha, True
        url = client.upload_file(path)
        _local_uploads[sha] = url
        _r().set(UPLOAD.format(sha), url, ex=UPLOAD_TTL_S)
        return url, sha, False


def upload_key(client, key: str) -> str:
    """Nesne deposundaki dosyayı (anahtar) fal deposuna yükler (SHA önbellekli)."""
    from app import storage
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = storage.download(key, os.path.join(tmp, "upload" + (os.path.splitext(key)[1] or ".bin")))
        url, _, _ = upload_cached(client, path)
    return url


# ---------- iş kaydı (sürdürme) ----------

def job_key(endpoint: str, args: dict) -> str:
    return stable_hash([endpoint, args])


def pending(job: str, scene_id: str | None = None) -> dict | None:
    raw = _r().get(JOB.format(job))
    if raw:
        return json.loads(raw)
    if scene_id:                                        # Redis boşaltıldıysa çekimin kaydı
        from app.db import session_scope
        from app.models import Scene
        with session_scope() as s:
            sc = s.get(Scene, scene_id)
            req = (sc.fal_request or {}) if sc is not None else {}
        if req.get("job") == job and req.get("status") == "submitted" and req.get("request_id"):
            return req
    return None


def _scene_request(scene_id: str | None, **fields) -> None:
    if not scene_id:
        return
    from app.db import session_scope
    from app.models import Scene
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        if sc is not None:
            sc.fal_request = {**(sc.fal_request or {}), **fields}


def remember(job: str, request_id: str, endpoint: str, scene_id: str | None, **info) -> None:
    entry = {"request_id": request_id, "endpoint": endpoint, "submitted_at": time.time(), "job": job}
    _r().set(JOB.format(job), json.dumps(entry), ex=JOB_TTL_S)
    _scene_request(scene_id, **entry, status="submitted", error=None, **info)


def forget(job: str) -> None:
    _r().delete(JOB.format(job))


# ---------- kuyruk ----------

@dataclass
class Outcome:
    result: dict
    request_id: str
    endpoint: str
    resumed: bool
    elapsed_s: float


def _noted(args: dict) -> dict:
    out = {k: args[k] for k in NOTED if k in args}
    if args.get("prompt"):
        out["prompt"] = args["prompt"][:400]
    return out


def _submit(client, endpoint: str, args: dict) -> str:
    handle = client.submit(endpoint, arguments=args)
    return handle.request_id


def _state(status) -> str:
    """fal_client Queued / InProgress / Completed (sahte istemcide aynı adlı sınıflar)."""
    return type(status).__name__


def _wait(client, endpoint: str, request_id: str, timeout_s: float, poll_s: float, project_id: str | None) -> dict:
    """Sonuç (dict). Durum sorgusundaki ağ ya da 5xx hatası işi yeniden göndermez: aynı iş sorgulanmaya devam eder.
    Sonuç çağrısının hatası (işin kendisi başarısız: 4xx/5xx) yükseltilir."""
    from app.live.fal_video import is_transient
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            state = _state(client.status(endpoint, request_id, with_logs=False))
        except Exception as e:                           # noqa: BLE001
            if not is_transient(e):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{endpoint} {request_id}: durum okunamadı ({e})") from e
            log.warning("fal durum sorgusu hatası (iş yeniden gönderilmez): %s", e)
            time.sleep(poll_s)
            continue
        if state == "Completed":
            return client.result(endpoint, request_id)
        if project_id and killswitch.is_frozen(project_id):
            try:
                client.cancel(endpoint, request_id)
            except Exception as e:                       # noqa: BLE001 — iş zaten bitmiş olabilir
                log.warning("fal işi iptal edilemedi (%s): %s", request_id, e)
            raise killswitch.ProjectFrozen(project_id)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{endpoint} {request_id}: {timeout_s:.0f} sn içinde bitmedi")
        time.sleep(poll_s)


def run(client, endpoint: str, args: dict, *, timeout_s: float, scene_id: str | None = None,
        project_id: str | None = None, poll_s: float | None = None, info: dict | None = None) -> Outcome:
    """İşi gönderir (ya da bekleyen aynı işi sürdürür), bitmesini bekler ve sonucu döndürür."""
    poll = POLL_S if poll_s is None else poll_s
    job = job_key(endpoint, args)
    started = time.monotonic()
    entry = pending(job, scene_id)
    resumed = entry is not None
    submits = 1 if resumed else 0                       # sürdürülen iş ilk deneme sayılır: en çok bir yeniden gönderim
    request_id = entry["request_id"] if entry else None
    if resumed:
        history.step(endpoint, request_id, resumed=True, **_noted(args))
        log.info("fal işi sürdürülüyor: %s %s", endpoint, request_id)
    while True:
        try:
            if request_id is None:
                submits += 1
                request_id = _submit(client, endpoint, args)
                remember(job, request_id, endpoint, scene_id, **(info or {}))
                history.step(endpoint, request_id, **_noted(args))       # gönderilen iş kayda hemen geçer
            try:
                result = _wait(client, endpoint, request_id, timeout_s, poll, project_id)
            except TimeoutError:                         # zaman aşımı: aynı iş bir pencere daha beklenir
                log.warning("fal işi zaman aşımı, bir kez daha bekleniyor: %s %s", endpoint, request_id)
                try:
                    result = _wait(client, endpoint, request_id, timeout_s, poll, project_id)
                except TimeoutError as e:
                    try:
                        client.cancel(endpoint, request_id)
                    except Exception:                    # noqa: BLE001
                        pass
                    forget(job)
                    _scene_request(scene_id, status="failed", error=str(e))
                    raise FalTimeout(f"fal.ai işi zaman aşımına uğradı ve iptal edildi ({request_id}): {e}") from e
        except killswitch.ProjectFrozen:
            forget(job)                                  # iş iptal edildi: sürdürülecek bir şey kalmadı
            _scene_request(scene_id, status="failed", error="Kill-Switch: fal.ai işi iptal edildi")
            raise
        except (FalError, PaidCallBlocked):
            raise
        except Exception as e:                           # noqa: BLE001 — sınıflandırılır
            err = classify(e, endpoint)
            if isinstance(err, TransientError) and submits <= SUBMIT_RETRIES:
                log.warning("fal 5xx/ağ hatası, iş bir kez yeniden gönderiliyor: %s", e)
                forget(job)
                request_id = None
                continue
            forget(job)
            _scene_request(scene_id, status="failed", error=str(err))
            if isinstance(err, TransientError):
                raise FalError(f"fal.ai işi tamamlanamadı (yeniden deneme de başarısız): {err}") from e
            raise err from e
        forget(job)
        _scene_request(scene_id, status="completed", completed_at=time.time())
        return Outcome(result=result, request_id=request_id, endpoint=endpoint, resumed=resumed,
                       elapsed_s=round(time.monotonic() - started, 1))


def download(url: str, suffix: str, expected_size: int | None = None) -> str:
    """Çıktıyı hemen indirir (fal CDN adresine güvenilmez); boyut bildirildiyse doğrular."""
    from app.live.fal_video import _download
    path = _download(url, suffix)
    if expected_size and os.path.getsize(path) != int(expected_size):
        size = os.path.getsize(path)
        os.remove(path)
        raise TransientError(f"indirilen dosya eksik: {size} bayt, beklenen {expected_size}")
    return path


def client():
    from app.live.fal_video import _client
    return _client(guard("fal_key"))
