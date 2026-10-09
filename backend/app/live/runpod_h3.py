"""ÜCRETLİ video üretimi: MiniMax H3, RunPod Serverless'ta (varsayılan platform). Bu modül gerçek RunPod'da hiç çalıştırılmadı.

Kurulum (backend/runpod/README.md): modeller bir veri merkezindeki Network Volume'da (varsayılan CA-MTL-1, 200 GB;
backend/runpod/setup_volume.sh ile bir kez indirilir); worker, RunPod'un resmî ComfyUI worker'ı tabanlı imaj
(backend/runpod/worker: güncel ComfyUI + H3 düğümleri, videoyu da döndüren handler). Uç nokta: RTX 5090 ve RTX PRO 6000,
yalnızca volume'un veri merkezi, en az 0 worker (flex), ayarlanabilir boşta bekleme (varsayılan 60 sn).

İş akışı (docs.runpod.io › Serverless › Operation reference, Ekim 2026):
- POST https://api.runpod.ai/v2/{uç nokta}/run  {"input": {workflow, images, hellgrind}, "policy": {...}} -> {"id", "status"}
- GET  .../status/{id} -> {"status": IN_QUEUE | IN_PROGRESS | COMPLETED | FAILED | CANCELLED | TIMED_OUT, "output",
  "delayTime", "executionTime" (ms)}; sonuç tamamlandıktan sonra 30 dk tutulur.
- GET  .../health -> {"jobs": {...}, "workers": {"idle", "running"}}
İş akışı JSON'u (Ayarlar'daki ComfyUI iş akışları, {{...}} yer tutucuları), giriş görselleri (ilk kare, referanslar; base64)
ve parametreler istekte gider. Çıktı yanıtta base64 (en çok ~9 MB; RunPod /run sonuçlarını ~10 MB'ta keser) ya da worker'a
S3 tanımlıysa geçici bir indirme adresi olarak gelir; ardından çıktı güvencesinden geçer (Content-Length, worker'ın bildirdiği
ve yerelde hesaplanan SHA-256, MinIO'daki kopyanın yeniden doğrulanması; app/live/h3_outputs.py).

Bedel: RunPod'un bildirdiği çalışma süresi (executionTime) + worker'ın bildirdiği soğuk başlatma (worker'ın ilk işi: açılış
ve model yükleme) × işi çalıştıran GPU tipinin saniye ücreti (pricing.yaml › serverless.runpod). Worker'ların boşta bekleme
süresi (idle timeout) dakikalık görevle (`hg.runpod_tick`) son etkin projeye yazılır. Rezervasyon en pahalı GPU tipiyle.

Korumalar (her yeni işten önce): uç nokta yalnızca izinli veri merkezlerinde (app/core/regions.py; uç noktanın ayarı RunPod
API'sinden okunur, okunamazsa üretim başlamaz); RunPod bakiyesi Ayarlar'daki eşiğin üstünde (GraphQL `myself.clientBalance`;
RunPod GraphQL'i 2027 başında kaldıracağını duyurdu). Kilit: `guard("runpod_api_key")`.
"""
import base64
import copy
import hashlib
import json
import logging
import os
import time
import uuid

import httpx

from app.budget import history, killswitch, pricing
from app.config import settings
from app.core import regions
from app.engine.providers import GenRequest, GenResult, TransientError
from app.live import PaidCallBlocked, guard, h3_outputs, vast_box, vast_h3

log = logging.getLogger("hellgrind.live.runpod")

ENGINE = "h3"
KEY = "runpod_api_key"
BACKEND = "runpod"
JOBS_API = "https://api.runpod.ai/v2"                      # iş API'si (uç nokta başına)
REST_API = "https://api.runpod.io/v2"                      # yönetim (uç nokta ayarları); REST v1 2026-11-15'te kalkıyor
GRAPHQL = "https://api.runpod.io/graphql"                  # bakiye (REST'te karşılığı yok)
POOLS = {"RTX 5090": "ADA_32_PRO", "RTX PRO 6000": "BLACKWELL_96"}   # RunPod GPU havuzları
FAILED = ("FAILED", "CANCELLED", "TIMED_OUT")
POLL_S = 5.0
QUEUE_WAIT_S = 1800                                        # soğuk başlatma ve kuyruk için işin süresine eklenen pay
INLINE_LIMIT = 9_000_000                                   # yanıtta taşınan base64 çıktının üst sınırı (bayt)
BALANCE_CACHE_S = 60
CONFIG_CACHE_S = 600
MAX_TICK_S = 300
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
TRANSPORT: httpx.BaseTransport | None = None               # testler sahte RunPod bağlar


class JobFailed(TransientError):
    """RunPod işi başarısız, iptal ya da zaman aşımı bildirdi: yeni bir gönderimle yeniden denenebilir."""


class JobLost(RuntimeError):
    """Gönderilmiş işin durumu UNKNOWN_LIMIT sorguda da okunamadı (404: RunPod işi tanımıyor — kuyruk boşaltıldı ya da
    sonucu silindi —, zaman aşımı ya da 5xx). Yeniden GÖNDERİLMEZ (aynı iş ikinci kez ücretlenebilirdi): çekim hata
    notuyla durur; yönetmen yeniden üretir."""


UNKNOWN_LIMIT = 12            # durum art arda bu kadar sorguda okunamazsa iş kayıp sayılır (POLL_S ile ~1 dk)


# ---------- gönderilmiş işler (süreçler arası, Redis) ----------
# Celery worker süreci öldürülürse (ör. bellek: SIGKILL) görev yeniden teslim edilir ve baştan çalışır. Aynı iş anahtarı
# (seed, istem, kare, boyut...) için RunPod'da bekleyen ya da çalışan iş varsa yenisi gönderilmez: yeni teslim eski işi
# beklemeyi sürdürür. Bir projede aynı anda en çok `runpod_max_pending_jobs` iş bekler; fazlası yer açılana dek
# gönderilmez (olay akışına yazılır). Kill-Switch projenin bekleyen işlerini RunPod'da iptal eder (`cancel_project`).

JOB_TTL_S = 7 * 3600          # Celery visibility_timeout (6 sa) + pay


MODEL_ORDER = ("keyframe", "edit_keyframe", "fl2va", "ref2va")   # kuyruk sırası: Qwen-Image, Qwen-Image-Edit, H3 DiT'leri
STALE_S = 120                 # bu kadar süredir sorgulanmayan bekleyen iş (süreci ölmüş) sıraya ve sınıra sayılmaz
GATHER_S = 1.0                # proje boştayken aynı anda gelen işlerin toplanma süresi (en öncelikli model ilk gitsin)


def _job_key(jkey: str) -> str:
    return vast_box._k(f"rp_job:{jkey}")


def _pending_key(project_id: str | None) -> str:
    return vast_box._k(f"rp_pending:{project_id or '-'}")


def _waiting_key(project_id: str | None) -> str:
    return vast_box._k(f"rp_waiting:{project_id or '-'}")


def _s(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


def submitted(jkey: str) -> str | None:
    """Bu iş anahtarı için RunPod'a gönderilmiş ve henüz bitmemiş işin kimliği."""
    raw = killswitch.r().get(_job_key(jkey))
    if not raw:
        return None
    return json.loads(raw).get("id")


def remember(jkey: str, job_id: str, project_id: str | None, model: str = "fl2va") -> None:
    r = killswitch.r()
    r.set(_job_key(jkey), json.dumps({"id": job_id, "project": project_id, "at": time.time()}), ex=JOB_TTL_S)
    r.hset(_pending_key(project_id), job_id, json.dumps({"jkey": jkey, "model": model, "seen": time.time()}))
    r.expire(_pending_key(project_id), JOB_TTL_S)


def touch(job_id: str, project_id: str | None) -> None:
    """Bekleyen iş hâlâ sorgulanıyor (sahibi yaşıyor)."""
    r = killswitch.r()
    raw = r.hget(_pending_key(project_id), job_id)
    if raw:
        entry = json.loads(raw)
        entry["seen"] = time.time()
        r.hset(_pending_key(project_id), job_id, json.dumps(entry))


def settle(job_id: str, project_id: str | None) -> None:
    """İş RunPod'da bitti (artık kuyrukta yer tutmuyor); çıktısı alınana dek iş anahtarı kayıtlı kalır."""
    killswitch.r().hdel(_pending_key(project_id), job_id)


def forget(jkey: str, job_id: str, project_id: str | None) -> None:
    r = killswitch.r()
    r.delete(_job_key(jkey))
    r.hdel(_pending_key(project_id), job_id)


def _pending_entries(project_id: str | None) -> dict[str, dict]:
    out = {}
    for k, v in killswitch.r().hgetall(_pending_key(project_id)).items():
        raw = _s(v)
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            entry = {"jkey": raw, "model": "fl2va", "seen": time.time()}    # eski biçim: yalnızca iş anahtarı
        out[_s(k)] = entry
    return out


def pending(project_id: str | None) -> dict[str, str]:
    """Projenin RunPod'da bekleyen ya da çalışan işleri: {iş kimliği: iş anahtarı}."""
    return {job_id: e["jkey"] for job_id, e in _pending_entries(project_id).items()}


def _live_pending(project_id: str | None, now: float) -> dict[str, dict]:
    """Sahibi hâlâ sorgulayan bekleyen işler (STALE_S içinde görülmüş)."""
    return {j: e for j, e in _pending_entries(project_id).items() if now - float(e.get("seen") or 0) <= STALE_S}


def _waiting(project_id: str | None, now: float) -> dict[str, dict]:
    r, out = killswitch.r(), {}
    for k, v in r.hgetall(_waiting_key(project_id)).items():
        entry = json.loads(_s(v))
        if now - float(entry.get("seen") or 0) > STALE_S:
            r.hdel(_waiting_key(project_id), k)            # süreci ölmüş bekleyen
            continue
        out[_s(k)] = entry
    return out


def _rank(model: str) -> int:
    return MODEL_ORDER.index(model) if model in MODEL_ORDER else len(MODEL_ORDER)


def may_submit(model: str, token: str, since: float, project_id: str | None, cap: int, now: float) -> str | None:
    """Gönderilebilir mi; değilse neden (olay için). Kurallar (proje başına, RunPod worker'ında model değişimi az olsun):
    - projede başka modelin işi bekliyorsa ya da çalışıyorsa, onlar bitene dek beklenir (model grubu sırası);
    - aynı model çalışırken daha öncelikli bir modelin işi bekliyorsa yeni iş eklenmez (grup boşalır, sıra ona geçer);
    - proje boştaysa bekleyenlerin en öncelikli modeli gider (aynı anda gelenler GATHER_S içinde toplanır);
    - proje sınırı (runpod_max_pending_jobs)."""
    active = _live_pending(project_id, now)
    waiting = _waiting(project_id, now)
    better = [e for k, e in waiting.items() if k != token and _rank(e["model"]) < _rank(model)]
    models = {e.get("model", "fl2va") for e in active.values()}
    if models and model not in models:
        return "model"
    if better:
        return "model"
    if not models and now - since < GATHER_S:
        return "gather"
    if len(active) >= cap:
        return "full"
    return None


def _frozen(project_id: str | None) -> bool:
    return bool(project_id) and killswitch.is_frozen(project_id)


def submit_once(c: httpx.Client, job: vast_h3.Job, kind: str, project_id: str | None) -> tuple[str, bool]:
    """(iş kimliği, yeniden kullanıldı mı). Aynı iş anahtarı için bekleyen iş varsa onu döndürür; yoksa sırası gelince
    (model grubu ve proje sınırı, `may_submit`) gönderir. Gönderim proje başına kilitle sıralanır."""
    jkey = job.key()
    if (job_id := submitted(jkey)) is not None:
        return job_id, True
    cap = max(1, int(settings.runpod_max_pending_jobs))
    model = model_kind(job)
    token, since = uuid.uuid4().hex, time.time()
    deadline = time.monotonic() + float(settings.h3_timeout_s) + QUEUE_WAIT_S
    noted: set[str] = set()
    r = killswitch.r()
    try:
        while True:
            if _frozen(project_id):
                raise killswitch.ProjectFrozen(project_id)
            r.hset(_waiting_key(project_id), token, json.dumps({"model": model, "since": since, "seen": time.time()}))
            r.expire(_waiting_key(project_id), JOB_TTL_S)
            with r.lock(vast_box._k(f"rp_submit:{project_id or '-'}"), timeout=60, blocking_timeout=60):
                if (job_id := submitted(jkey)) is not None:
                    return job_id, True
                why = may_submit(model, token, since, project_id, cap, time.time())
                if why is None:
                    job_id = submit(c, job, kind)
                    remember(jkey, job_id, project_id, model)
                    return job_id, False
            if project_id and why not in noted and why != "gather":
                if why == "full":
                    killswitch.event(project_id, "runpod_queue_full", pending=len(pending(project_id)), limit=cap)
                else:
                    killswitch.event(project_id, "runpod_model_wait", model=MODEL_TR.get(model, model))
                noted.add(why)
            if time.monotonic() >= deadline:
                raise TransientError(f"RunPod'da sıra gelmedi ({why}): projenin işleri bitmedi")
            time.sleep(min(POLL_S, GATHER_S) if why == "gather" else POLL_S)
    finally:
        r.hdel(_waiting_key(project_id), token)


def cancel_project(project_id: str) -> int:
    """Kill-Switch: projenin RunPod'da bekleyen ve çalışan işlerini iptal eder (POST /cancel/{id}). Dönen değer iptal
    istenen iş sayısı. Bekleyen işleri sorgulayan görevler CANCELLED görüp durur; rezervasyonları iade edilir."""
    jobs = pending(project_id)
    if not jobs:
        return 0
    key = guard(KEY)
    cancelled = 0
    with _client(key) as c:
        for job_id, jkey in jobs.items():
            try:
                c.post(f"/{endpoint_id()}/cancel/{job_id}").raise_for_status()
                cancelled += 1
            except Exception as e:                    # noqa: BLE001 — iş zaten bitmiş ya da silinmiş olabilir
                log.warning("RunPod işi iptal edilemedi %s: %s", job_id, e)
            forget(jkey, job_id, project_id)
            try:
                h3_outputs.failed(server(), job_id, "Kill-Switch: RunPod'da iptal edildi")
            except Exception:                         # noqa: BLE001
                log.warning("çıktı kaydı kapatılamadı: %s", job_id)
    killswitch.event(project_id, "runpod_cancelled", jobs=len(jobs), cancelled=cancelled)
    return cancelled


# ---------- bağlantı ----------

def endpoint_id() -> str:
    eid = (settings.runpod_endpoint_id or "").strip()
    if not eid:
        raise PaidCallBlocked("RunPod uç nokta kimliği tanımlı değil: Ayarlar › MiniMax H3 · RunPod'dan girin "
                              "(kurulum: backend/runpod/README.md).")
    return eid


def server() -> str:
    """Çıktı kaydındaki sunucu adı."""
    return f"runpod://{endpoint_id()}"


def instance() -> str:
    """Çıktı kaydındaki "makine": RunPod'da uç nokta."""
    return f"runpod:{endpoint_id()}"


def _client(key: str, base: str = JOBS_API) -> httpx.Client:
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                        timeout=TIMEOUT, transport=TRANSPORT, follow_redirects=True)


def _status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _classify(exc: BaseException) -> BaseException:
    if isinstance(exc, (PaidCallBlocked, TransientError, JobLost, killswitch.ProjectFrozen)):
        return exc
    status = _status(exc)
    if status in (401, 403):
        return PaidCallBlocked("RunPod API anahtarı reddedildi: Ayarlar › MiniMax H3 · RunPod'dan anahtarı kontrol edin.")
    if status == 404:
        return PaidCallBlocked(f"RunPod uç noktası bulunamadı ({settings.runpod_endpoint_id}): uç nokta kimliğini kontrol "
                               "edin.")
    if status == 429 or (status is not None and status >= 500):
        return TransientError(f"RunPod geçici hata (HTTP {status})")
    if isinstance(exc, (TimeoutError, httpx.TransportError)):
        return TransientError(f"RunPod'a ulaşılamadı ({type(exc).__name__})")
    if isinstance(exc, httpx.HTTPStatusError):
        return RuntimeError(f"RunPod isteği reddetti (HTTP {status}): {(exc.response.text or '').strip()[:300]}")
    return exc


def _reraise(exc: BaseException):
    classified = _classify(exc)
    if classified is exc:
        raise exc
    raise classified from exc


# ---------- durum önbelleği (panel ve korumalar) ----------

def _cache_get(name: str, max_age: float) -> dict | None:
    raw = killswitch.r().get(vast_box._k(f"rp_{name}"))
    if not raw:
        return None
    data = json.loads(raw)
    return data if time.time() - data.get("at", 0) <= max_age else None


def _cache_set(name: str, data: dict) -> dict:
    data = {**data, "at": time.time()}
    killswitch.r().set(vast_box._k(f"rp_{name}"), json.dumps(data, ensure_ascii=False), ex=7 * 24 * 3600)
    return data


def cached_state() -> dict:
    """Panel: son okunan bakiye, uç nokta ayarı ve sağlık (yalnızca kayıt; ağ yok)."""
    return {name: _cache_get(name, float("inf")) for name in ("balance", "config", "health", "tick")}


# ---------- bakiye ----------

def balance(key: str, refresh: bool = False) -> dict:
    """RunPod hesap bakiyesi (GraphQL `myself`; salt okunur, ücretsiz)."""
    if not refresh and (hit := _cache_get("balance", BALANCE_CACHE_S)) is not None:
        return hit
    with _client(key, "") as c:                                     # tam adres: base_url sona "/" ekler
        r = c.post(GRAPHQL, json={"query": "query { myself { clientBalance currentSpendPerHr spendLimit } }"})
    r.raise_for_status()
    body = r.json()
    me = (body.get("data") or {}).get("myself") or {}
    if "clientBalance" not in me:
        raise RuntimeError(f"RunPod bakiyesi okunamadı: {json.dumps(body.get('errors') or body)[:200]}")
    return _cache_set("balance", {"usd": float(me["clientBalance"]), "spend_per_hr": me.get("currentSpendPerHr"),
                                  "spend_limit": me.get("spendLimit")})


def balance_problem(key: str) -> str | None:
    """Bakiye eşiğin altındaysa yeni iş başlamaz. Okunamazsa uyarı kaydedilir ama üretim durmaz (GraphQL kalkabilir)."""
    try:
        b = balance(key)
    except Exception as e:                                           # noqa: BLE001
        _cache_set("balance_error", {"error": f"{type(e).__name__}: {str(e)[:200]}"})
        log.warning("RunPod bakiyesi okunamadı: %s", e)
        return None
    floor = float(settings.runpod_min_balance_usd)
    if b["usd"] < floor:
        vast_box._event("runpod_balance_low", balance=b["usd"], floor=floor)
        return (f"RunPod bakiyesi düşük ({b['usd']:.2f} $ < eşik {floor:.2f} $): yeni iş başlatılmadı. RunPod hesabına "
                "bakiye ekleyin ya da Ayarlar'dan eşiği değiştirin.")
    return None


# ---------- uç nokta ayarı ve bölge ----------

def _ids(items) -> list[str]:
    out = []
    for item in items or []:
        value = (item.get("id") or item.get("dataCenterId")) if isinstance(item, dict) else item
        if value:
            out.append(str(value))
    return out


def summarize_config(raw: dict) -> dict:
    """RunPod uç nokta yanıtını (REST v2; v1 alan adları da okunur) panelin ve bölge denetiminin kullandığı biçime çevirir."""
    raw = raw.get("serverless") or raw.get("endpoint") or raw
    workers = raw.get("workers") or {}
    gpu = raw.get("gpu") or {}
    volumes = raw.get("networkVolumes") or raw.get("networkVolumeIds") or (
        [raw["networkVolumeId"]] if raw.get("networkVolumeId") else [])
    return {"name": raw.get("name"),
            "datacenters": _ids(raw.get("dataCenterIds") or raw.get("dataCenters")),
            "volumes": _ids(volumes),
            "pools": [str(p) for p in (gpu.get("pools") or raw.get("gpuTypeIds") or [])],
            "workers_min": workers.get("min", raw.get("workersMin")),
            "workers_max": workers.get("max", raw.get("workersMax")),
            "idle_timeout": workers.get("idleTimeout", raw.get("idleTimeout")),
            "flashboot": raw.get("flashboot")}


def endpoint_config(key: str, refresh: bool = False) -> dict:
    if not refresh and (hit := _cache_get("config", CONFIG_CACHE_S)) is not None:
        return hit
    with _client(key, REST_API) as c:
        r = c.get(f"/serverless/{endpoint_id()}")
    r.raise_for_status()
    return _cache_set("config", summarize_config(r.json()))


def region_problem(cfg: dict | None) -> str | None:
    """Uç nokta yalnızca izinli veri merkezlerinde çalışmalı: kısıt yoksa (her yerde çalışabilir) ya da izinli listenin
    dışında bir merkez varsa üretim başlamaz."""
    allowed = ", ".join(regions.runpod_datacenters())
    if not regions.datacenter_allowed(settings.runpod_datacenter):
        return (f"Ayarlardaki veri merkezi ({settings.runpod_datacenter}) izinli listede değil ({allowed}); Kuzey merkezleri "
                "için Ayarlar › Kuzey veri merkezleri'ni açın.")
    if cfg is None:
        return "RunPod uç noktasının veri merkezleri okunamadı: bölge doğrulanmadan üretim başlamaz."
    dcs = cfg.get("datacenters") or []
    if not dcs:
        return ("RunPod uç noktasında veri merkezi kısıtı yok (her bölgede çalışabilir): Ayarlar › MiniMax H3 · RunPod › "
                f"Ayarları uç noktaya uygula ile yalnızca {settings.runpod_datacenter} seçin.")
    bad = [dc for dc in dcs if not regions.datacenter_allowed(dc)]
    if bad:
        return (f"RunPod uç noktası izinli listede olmayan veri merkezlerinde de çalışabilir ({', '.join(bad)}; izinli: "
                f"{allowed}): Ayarlar'dan uç nokta ayarlarını uygulayın.")
    return None


def desired_config() -> dict:
    """Ayarlar'daki uç nokta ayarı (REST v2 PATCH gövdesi)."""
    names = [g.strip() for g in (settings.runpod_gpu_types or "").split(",") if g.strip()]
    unknown = [g for g in names if g not in POOLS]
    if unknown or not names:
        raise PaidCallBlocked(f"Bilinmeyen GPU tipi: {', '.join(unknown) or 'boş'} (geçerli: {', '.join(POOLS)}).")
    body = {"gpu": {"pools": [POOLS[g] for g in names]},
            "workers": {"min": 0, "idleTimeout": int(settings.runpod_idle_timeout_s)},
            "dataCenterIds": [settings.runpod_datacenter], "flashboot": "FLASHBOOT"}
    if str(settings.runpod_workers_max) in ("1", "2", "3"):        # keep: uç noktadaki en çok worker değişmez
        workers = int(settings.runpod_workers_max)
        if int(settings.runpod_max_pending_jobs) < workers:
            raise PaidCallBlocked(f"En çok worker ({workers}) projenin bekleyen iş sınırından "
                                  f"({settings.runpod_max_pending_jobs}) büyük: fazladan worker hiç iş alamaz. "
                                  "RUNPOD_MAX_PENDING_JOBS'u artırın ya da worker sayısını düşürün.")
        body["workers"]["max"] = workers
    if settings.runpod_network_volume_id:
        body["networkVolumes"] = [settings.runpod_network_volume_id]
    return body


def apply_config() -> dict:
    """Ayarlar'daki GPU tiplerini, veri merkezini, volume'u, en az 0 worker'ı (flex) ve boşta bekleme süresini uç noktaya
    yazar (REST v2 PATCH; alan adları OpenAPI'den, gerçek hesapta denenmedi). İzinli olmayan veri merkezine yazılmaz."""
    key = guard(KEY)
    if not regions.datacenter_allowed(settings.runpod_datacenter):
        raise PaidCallBlocked(region_problem(None) or "veri merkezi izinli değil")
    body = desired_config()
    with _client(key, REST_API) as c:
        r = c.patch(f"/serverless/{endpoint_id()}", json=body)
        r.raise_for_status()
    cfg = endpoint_config(key, refresh=True)
    vast_box._event("runpod_config_applied", datacenters=cfg.get("datacenters"), pools=cfg.get("pools"))
    return cfg


def health(key: str) -> dict:
    with _client(key) as c:
        r = c.get(f"/{endpoint_id()}/health")
    r.raise_for_status()
    body = r.json()
    return _cache_set("health", {"jobs": body.get("jobs") or {}, "workers": body.get("workers") or {}})


def preflight(key: str) -> None:
    """Yeni işten önce: uç nokta, bölge, bakiye. Engel varsa Türkçe PaidCallBlocked (hiçbir rezervasyon yapılmaz)."""
    endpoint_id()
    try:
        cfg = endpoint_config(key)
    except PaidCallBlocked:
        raise
    except Exception as e:                                           # noqa: BLE001 — bölge doğrulanamazsa başlamaz
        classified = _classify(e)
        if isinstance(classified, PaidCallBlocked):
            raise classified from e
        log.warning("RunPod uç nokta ayarı okunamadı: %s", e)
        cfg = None
    if (problem := region_problem(cfg)) is not None:
        raise PaidCallBlocked(problem)
    if (problem := balance_problem(key)) is not None:
        raise PaidCallBlocked(problem)


# ---------- soğuk başlatma ve model türü ----------

def cold_start_due() -> bool:
    """Bu iş soğuk bir worker'a mı düşecek (rezervasyona açılış + model yükleme eklensin mi). Az önce biten bir iş ya da
    /health'te açık bir worker varsa hayır. Aynı anda gönderilen işlerden yalnızca biri soğuk başlatmayı üstlenir (dalganın
    bütün işleri birlikte gönderilir; worker bir kez açılır)."""
    warm = _cache_get("warm", float("inf"))
    if warm and warm.get("until", 0) > time.time():
        return False
    h = _cache_get("health", 90)
    if h and int((h.get("workers") or {}).get("idle") or 0) + int((h.get("workers") or {}).get("running") or 0) > 0:
        return False
    ttl = int(settings.runpod_idle_timeout_s) + int(pricing.serverless().get("cold_start_seconds", 140)) + 60
    return bool(killswitch.r().set(vast_box._k("rp_cold_claim"), time.time(), nx=True, ex=ttl))


EDIT_STEPS = {"full": 40, "medium": 20, "lightning": 4}   # lightning: adım ve cfg şablondaki anahtarla (4, cfg 1)


def estimate_keyframe(model: str = "qwen", edit_mode: str = "full") -> float:
    """model: qwen (Qwen-Image, metinden) ya da qwen_edit (Qwen-Image-Edit-2511, karakter referansından)."""
    return pricing.estimate_keyframe(ENGINE, cold=cold_start_due(), model=model, edit_mode=edit_mode)


def model_kind(job: vast_h3.Job) -> str:
    """Worker'da hangi model yüklü kalır: keyframe (Qwen-Image), edit_keyframe (Qwen-Image-Edit-2511), fl2va (FL2VA DiT;
    t2va da), ref2va."""
    return {"keyframe": "keyframe", "edit_keyframe": "edit_keyframe", "ref2va": "ref2va"}.get(job.task, "fl2va")


IMAGE_MODEL = {"keyframe": "Qwen/Qwen-Image-2512", "edit_keyframe": "Qwen/Qwen-Image-Edit-2511"}
MODEL_TR = {"keyframe": "Qwen-Image (ilk kare)", "edit_keyframe": "Qwen-Image-Edit-2511 (karakterli ilk kare)",
            "fl2va": "H3 FL2VA", "ref2va": "H3 Ref2VA"}


def note_worker(job: vast_h3.Job, body: dict) -> None:
    """İş bitti: worker artık sıcak (boşta bekleme süresi boyunca). Soğuk başlatma ya da model değişimi olay akışına
    süresiyle yazılır (modeller 30-40 GB; değişim her seferinde dakikalar sürer)."""
    out = body.get("output") or {}
    timing = out.get("timing") or {}
    _cache_set("warm", {"until": time.time() + int(settings.runpod_idle_timeout_s)})
    killswitch.r().set(vast_box._k("rp_job_end"), time.time(), ex=24 * 3600)   # boşta süre bu andan başlar
    kind = model_kind(job)
    cold = timing.get("cold_start_s") or 0
    previous = killswitch.r().get(vast_box._k("rp_last_model"))
    previous = previous.decode() if isinstance(previous, bytes) else previous
    killswitch.r().set(vast_box._k("rp_last_model"), kind, ex=24 * 3600)
    seconds = round(float(body.get("executionTime") or 0) / 1000.0 + float(cold), 1)
    if cold:
        vast_box._event("runpod_cold_start", model=MODEL_TR[kind], seconds=round(float(cold), 1), job_seconds=seconds)
    elif previous and previous != kind:
        vast_box._event("runpod_model_swap", model=MODEL_TR[kind], previous=MODEL_TR.get(previous, previous),
                        seconds=seconds)
    if out.get("gpu"):
        killswitch.r().set(vast_box._k("rp_last_gpu"), str(out["gpu"]), ex=24 * 3600)


# ---------- iş ----------

def _b64(uri: str) -> str:
    return uri.split(",", 1)[1] if uri.startswith("data:") else uri


def payload(job: vast_h3.Job, kind: str) -> dict:
    """RunPod isteği: yer tutucuları doldurulmuş iş akışı, giriş görselleri (base64, adla) ve iş parametreleri."""
    images, names = [], {}

    def add(uri: str) -> str:
        data = vast_h3.uri_bytes(uri)
        name = f"hg_{hashlib.sha256(data).hexdigest()[:16]}.jpg"
        if name not in names:
            names[name] = True
            images.append({"name": name, "image": _b64(uri)})
        return name

    values = {"prompt": job.prompt, "seed": job.seed, "width": job.width, "height": job.height, "fps": vast_h3.FPS,
              "duration": job.seconds, "frames": vast_h3.frames_for(job.seconds), "steps": job.steps,
              "filename_prefix": vast_h3.prefix_for(job, "keyframe" if kind in vast_h3.IMAGE_KINDS else kind),
              "unet_variant": settings.h3_unet_variant}
    if kind == "edit_keyframe":
        values.update(negative=job.negative, lightning=job.edit_mode == "lightning",
                      edit_steps=EDIT_STEPS.get(job.edit_mode, EDIT_STEPS["full"]))
    if job.keyframe:
        values["first_frame"] = add(job.keyframe)
    if job.last_frame:
        values["last_frame"] = add(job.last_frame)
    refs = [add(uri) for _, uri in job.references]
    for i in range(1, vast_h3.MAX_REFERENCES + 1):
        if refs:
            values[f"reference_{i}"] = refs[min(i, len(refs)) - 1]
    workflow = vast_h3.prune_references(copy.deepcopy(vast_h3.load_workflow(kind)), len(refs))
    workflow = vast_h3.prune_unfilled(workflow, values)
    workflow = vast_h3.fill(workflow if job.audio else vast_h3.strip_audio(workflow), values)
    timeout_ms = int(float(settings.h3_timeout_s) * 1000)
    return {"input": {"workflow": workflow, "images": images,
                      "hellgrind": {"kind": "keyframe" if kind in vast_h3.IMAGE_KINDS else "video", "job": job.key(),
                                    "inline_limit": INLINE_LIMIT}},
            "policy": {"executionTimeout": timeout_ms, "ttl": timeout_ms + QUEUE_WAIT_S * 2000}}


def submit(c: httpx.Client, job: vast_h3.Job, kind: str) -> str:
    r = c.post(f"/{endpoint_id()}/run", json=payload(job, kind))
    r.raise_for_status()
    body = r.json()
    if not body.get("id"):
        raise RuntimeError(f"RunPod iş kimliği dönmedi: {json.dumps(body)[:200]}")
    return str(body["id"])


def status(c: httpx.Client, job_id: str) -> dict:
    r = c.get(f"/{endpoint_id()}/status/{job_id}")
    r.raise_for_status()
    return r.json()


def _failure(body: dict) -> str:
    out = body.get("output") if isinstance(body.get("output"), dict) else {}
    detail = body.get("error") or out.get("error") or out.get("details") or body.get("status")
    return str(detail)[:300]


def wait(c: httpx.Client, job_id: str, job_key: str, project_id: str | None = None) -> dict:
    """İş bitene dek /status sorgular (kuyruk ve soğuk başlatma dahil). Dönen değer tamamlanmış durum yanıtı.
    Durum okunamazsa (404: RunPod işi tanımıyor; zaman aşımı, 429, 5xx) iş "durum bilinmiyor" olarak beklenir ve yeniden
    gönderilmez; UNKNOWN_LIMIT sorgudan sonra JobLost. Proje dondurulursa iş iptal edilir ve ProjectFrozen fırlar."""
    deadline = time.monotonic() + float(settings.h3_timeout_s) + QUEUE_WAIT_S
    unknown = 0
    while True:
        if _frozen(project_id):
            if job_id in pending(project_id):        # Kill-Switch henüz iptal etmediyse (cancel_project kaydı siler)
                try:
                    c.post(f"/{endpoint_id()}/cancel/{job_id}")
                except Exception:                    # noqa: BLE001 — iptal edilemese de beklemeyi bırak
                    log.warning("RunPod işi iptal edilemedi: %s", job_id)
            raise killswitch.ProjectFrozen(project_id)
        try:
            body = status(c, job_id)
        except Exception as e:                       # noqa: BLE001
            problem = _classify(e)
            if isinstance(problem, PaidCallBlocked) and _status(e) != 404:
                raise problem from e                 # anahtar reddedildi: beklemenin anlamı yok
            unknown += 1
            log.warning("RunPod iş durumu okunamadı (%d/%d) %s: %s", unknown, UNKNOWN_LIMIT, job_id, e)
            if unknown >= UNKNOWN_LIMIT:
                why = "RunPod işi tanımıyor (404: kuyruk boşaltılmış ya da sonuç silinmiş)" if _status(e) == 404 \
                    else f"durum okunamadı ({type(e).__name__})"
                raise JobLost(f"RunPod işinin durumu {UNKNOWN_LIMIT} sorguda okunamadı ({job_id}): {why}. İş yeniden "
                              "gönderilmedi; çekimi yeniden üretin.") from e
            vast_box.heartbeat(job_key)
            touch(job_id, project_id)
            time.sleep(POLL_S)
            continue
        unknown = 0
        touch(job_id, project_id)
        state = body.get("status")
        if state == "COMPLETED":
            out = body.get("output")
            if not isinstance(out, dict) or out.get("error"):
                raise JobFailed(f"RunPod işi çıktı vermedi ({job_id}): {_failure(body)}")
            return body
        if state in FAILED:
            raise JobFailed(f"RunPod işi {state} ({job_id}): {_failure(body)}")
        vast_box.heartbeat(job_key)
        if time.monotonic() >= deadline:
            raise TransientError(f"RunPod işi {settings.h3_timeout_s + QUEUE_WAIT_S} sn içinde bitmedi ({job_id})")
        time.sleep(POLL_S)


def remote_meta(body: dict) -> dict:
    """Kayda ve meta veriye yazılan sunucu yanıtı (çıktı verisi hariç)."""
    out = body.get("output") or {}
    files = [{k: v for k, v in f.items() if k != "data"} for f in (out.get("files") or out.get("images") or [])
             if isinstance(f, dict)]
    return {"status": body.get("status"), "delay_ms": body.get("delayTime"), "execution_ms": body.get("executionTime"),
            "worker_id": body.get("workerId"), "gpu": out.get("gpu"), "torch": out.get("torch"),
            "timing": out.get("timing"), "files": files,
            "comfy": out.get("comfy"), "worker_version": out.get("version")}


def billed_seconds(body: dict) -> float | None:
    """İşin faturalanan süresi: RunPod'un bildirdiği çalışma süresi + worker'ın ilk işiyse soğuk başlatması."""
    out = body.get("output") or {}
    timing = out.get("timing") or {}
    execution = body.get("executionTime")
    seconds = float(execution) / 1000.0 if isinstance(execution, (int, float)) else timing.get("execution_s")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    cold = timing.get("cold_start_s")
    return float(seconds) + (float(cold) if isinstance(cold, (int, float)) and cold > 0 else 0.0)


VIDEO = (".mp4", ".webm", ".mov", ".mkv")
IMAGE = (".png", ".jpg", ".jpeg", ".webp")


def pick(out: dict, image: bool) -> dict:
    """Worker çıktısından istenen dosya (bizim handler: "files"; resmî worker-comfyui: "images")."""
    wanted = IMAGE if image else VIDEO
    files = [f for f in (out.get("files") or out.get("images") or []) if isinstance(f, dict)
             and str(f.get("filename", "")).lower().endswith(wanted)]
    if not files:
        raise RuntimeError(f"RunPod işi {'görsel' if image else 'video'} çıktısı vermedi: iş akışında bir kaydetme düğümü "
                           f"olmalı ({', '.join(str(f.get('filename')) for f in (out.get('files') or out.get('images') or []) if isinstance(f, dict)) or 'çıktı yok'}).")
    return files[0]


def download(f: dict) -> vast_h3.Download:
    """Çıktıyı yerel dosyaya alır; boyutu ve SHA-256'yı worker'ın bildirdiğiyle (varsa) karşılaştırır."""
    os.makedirs(settings.work_dir, exist_ok=True)
    name = str(f.get("filename") or "output.mp4")
    suffix = os.path.splitext(name)[1].lower() or ".mp4"
    kind = f.get("type")
    if kind == "base64":
        data = base64.b64decode(f["data"])
        out = os.path.join(settings.work_dir, f"{uuid.uuid4()}{suffix}")
        with open(out, "wb") as fh:
            fh.write(data)
        got = vast_h3.Download(path=out, size=len(data), sha256=hashlib.sha256(data).hexdigest(), name=name)
    elif kind in ("url", "s3_url"):
        with httpx.Client(timeout=vast_h3.DOWNLOAD_TIMEOUT, transport=TRANSPORT, follow_redirects=True) as c:
            got = vast_h3._stream(c, str(f["data"]), None, suffix, name)
    else:
        raise RuntimeError(f"RunPod çıktısının türü bilinmiyor: {kind}")
    if f.get("size") is not None and int(f["size"]) != got.size:
        os.remove(got.path)
        raise vast_h3.ShortDownload(f"RunPod çıktısı eksik: {got.size} / {f['size']} bayt ({name})")
    if f.get("sha256") and f["sha256"] != got.sha256:
        os.remove(got.path)
        raise vast_h3.ShortDownload(f"RunPod çıktısının SHA-256'sı worker'ın bildirdiğiyle uyuşmuyor ({name})")
    return got


def collect(body: dict, job_id: str, image: bool = False, extra: dict | None = None) -> str:
    """Çıktıyı indirir, doğrular, yerel depoya yazar (çıktı güvencesi); kullanıma hazır yerel yol (video: MP4)."""
    got = download(pick(body.get("output") or {}, image))
    h3_outputs.archive(server(), job_id, got, extra)
    if not image and not got.path.endswith(".mp4"):
        return vast_h3._to_mp4(got.path)
    return got.path


def provenance(job: vast_h3.Job, body: dict) -> dict:
    out = body.get("output") or {}
    return {"model": settings.h3_model, "model_revision": settings.h3_model_revision or None, "backend": BACKEND,
            "platform": "runpod", "unet_variant": settings.h3_unet_variant, "torch": out.get("torch"), "task": job.task,
            "steps": job.steps, "endpoint": settings.runpod_endpoint_id,
            "server_location": settings.runpod_datacenter, "gpu": out.get("gpu"), "worker_id": body.get("workerId"),
            "license": vast_h3.LICENSE, "training_allowed": False}


def _register(job: vast_h3.Job, job_id: str, kind: str, project_id: str | None) -> None:
    h3_outputs.submitted(server(), job_id, kind, job.task, project_id,
                         {"seed": job.seed, "resolution": f"{job.width}x{job.height}", "seconds": job.seconds,
                          "prefix": vast_h3.prefix_for(job, vast_h3.comfy_kind(job) if kind == "video" else "keyframe")},
                         backend=BACKEND, instance_id=instance())


# ---------- sağlayıcı ----------

class RunpodProvider:
    """Yerel sağlayıcıyla aynı arayüz. Dalganın bütün işleri birlikte gönderilir (`whole_wave`): soğuk başlatma işler
    arasında paylaşılır; eşzamanlılığı uç noktanın worker sayısı belirler."""

    engine = ENGINE
    key_name = KEY
    image_first = False
    whole_wave = True

    def billed_seconds(self, duration_s: float) -> float:
        return float(vast_h3.seconds_for(duration_s))

    def resolution(self, requested: str) -> str:
        return vast_h3.priced_resolution(requested)

    def estimate(self, req: GenRequest) -> float:
        """Rezervasyon: koşula göre GPU süresi; worker soğuksa açılış + model yükleme de (yalnızca bir iş üstlenir)."""
        return pricing.estimate_video(ENGINE, self.resolution(req.resolution), self.billed_seconds(req.duration_s),
                                      mode=req.extra.get("h3_mode"), cold=cold_start_due())

    def upload(self, path: str) -> str:
        return vast_h3.data_uri(path)

    def generate(self, req: GenRequest) -> GenResult:
        key = guard(KEY)
        preflight(key)
        job = vast_h3.build_job(req, server=server())
        kind = vast_h3.comfy_kind(job)
        jkey = job.key()
        project_id = req.extra.get("project_id")
        job_id = None
        try:
            with _client(key) as c:
                job_id, reused = submit_once(c, job, kind, project_id)
                if not reused:
                    vast_h3._note(job, job_id, req.negative)
                    _register(job, job_id, "video", project_id)
                else:                                # yeniden teslim edilen görev: aynı işi beklemeyi sürdürür
                    history.step(settings.h3_model, job_id, task=job.task, reused=True)
                vast_box.track(jkey, project_id)
                try:
                    body = wait(c, job_id, jkey, project_id)
                except (JobFailed, JobLost) as e:
                    forget(jkey, job_id, project_id)
                    h3_outputs.failed(server(), job_id, str(e))
                    raise
                except killswitch.ProjectFrozen:
                    forget(jkey, job_id, project_id)
                    h3_outputs.failed(server(), job_id, "Kill-Switch: RunPod'da iptal edildi")
                    raise
            settle(job_id, project_id)
            note_worker(job, body)
            h3_outputs.completed(server(), job_id, remote_meta(body))
            prov = provenance(job, body)
            path = collect(body, job_id, extra={"provenance": prov})
            forget(jkey, job_id, project_id)          # çıktı alındı; indirme bozuksa yeniden deneme aynı işi kullanır
        except Exception as e:
            _reraise(e)
        finally:
            vast_box.untrack(jkey)
        cut = req.duration_s if self.billed_seconds(req.duration_s) - req.duration_s > 0.05 else None
        vast_h3.finish(path, cut, prov)
        seconds = billed_seconds(body)
        gpu = (body.get("output") or {}).get("gpu")
        if seconds:
            history.step(settings.h3_model, job_id, gpu_seconds=round(seconds, 1), gpu=gpu)
        cost = pricing.estimate_runpod(seconds, gpu) if seconds else pricing.estimate_video(
            ENGINE, self.resolution(req.resolution), self.billed_seconds(req.duration_s), mode=req.extra.get("h3_mode"))
        return GenResult(local_path=path, request_id=job_id, cost_usd=cost, engine=ENGINE,
                         meta={"task": job.task, "billed_seconds": self.billed_seconds(req.duration_s),
                               "size": f"{job.width}x{job.height}", "gpu_seconds": seconds, "gpu": gpu,
                               "references": job.names, "negative_dropped": bool(req.negative),
                               "negatives_unmapped": list(job.unmapped), "seed_sent": True, "provenance": prov})


def keyframe_image(prompt: str, seed: int, project_id: str | None = None, width: int | None = None,
                   height: int | None = None) -> GenResult:
    """Storyboard ilk karesi ya da varlık referansı, RunPod worker'ındaki ComfyUI görsel iş akışıyla (keyframe.json;
    Qwen-Image-2512). Varsayılan boy H3 tuvali (1280x704); referans 1328x1328 / 1664x928. Bu kare H3 çıktısı değildir.
    Çağıran `estimate_keyframe()` kadar rezervasyon almış olmalı."""
    if not (width and height):
        width, height = vast_h3.SIZES["720p"]
    job = vast_h3.Job(task="keyframe", url=server(), prompt=prompt, seed=seed, seconds=vast_h3.SECONDS[0], width=width,
                      height=height, steps=int(settings.h3_steps))
    return _image(job, project_id)


def edit_keyframe_image(prompt: str, seed: int, references: list[dict], negative: str = "",
                        project_id: str | None = None, mode: str | None = None) -> GenResult:
    """Karakterli çekimin ilk karesi, Qwen-Image-Edit-2511 ile (edit_keyframe.json): image 1 ana karakterin referansı,
    image 2 / image 3 ikinci karakter ya da mekân; istem onlara "image 1", "image 2" diye başvurur. Kare H3 tuvalinde
    (1280x704) çizilir; FL2VA test ve tam turu aynı kareden üretir. mode: full (40 adım, cfg 4) | medium (20 adım,
    cfg 4) | lightning (Lightning LoRA, 4 adım, cfg 1); verilmezse Ayarlar'daki QWEN_EDIT_MODE.
    Çağıran `estimate_keyframe("qwen_edit", mode)` kadar rezervasyon almış olmalı."""
    guard(KEY)
    if not references:
        raise PaidCallBlocked("Qwen-Image-Edit karesi için karakterin onaylı referans görseli yok.")
    width, height = vast_h3.SIZES["720p"]
    job = vast_h3.Job(task="edit_keyframe", url=server(), prompt=prompt, seed=seed, seconds=vast_h3.SECONDS[0],
                      width=width, height=height, steps=int(settings.h3_steps),
                      references=[(r["name"], vast_h3.reference_uri(r["key"])) for r in references[:3]],
                      negative=negative or "", edit_mode=mode if mode in EDIT_STEPS else settings.qwen_edit_mode)
    return _image(job, project_id)


def _image(job: vast_h3.Job, project_id: str | None) -> GenResult:
    key = guard(KEY)
    preflight(key)
    kind, jkey = job.task, job.key()
    try:
        with _client(key) as c:
            job_id, reused = submit_once(c, job, kind, project_id)
            history.step(f"{IMAGE_MODEL[kind]} (ComfyUI {kind}.json, RunPod)", job_id, seed=job.seed, reused=reused,
                         resolution=f"{job.width}x{job.height}",
                         prompt=job.prompt[:vast_h3.NOTED_PROMPT_CHARS],
                         **({"references": job.names, "edit_mode": job.edit_mode} if kind == "edit_keyframe" else {}))
            if not reused:
                _register(job, job_id, "keyframe", project_id)
            vast_box.track(jkey, project_id)
            try:
                body = wait(c, job_id, jkey, project_id)
            except (JobFailed, JobLost) as e:
                forget(jkey, job_id, project_id)
                h3_outputs.failed(server(), job_id, str(e))
                raise
            except killswitch.ProjectFrozen:
                forget(jkey, job_id, project_id)
                h3_outputs.failed(server(), job_id, "Kill-Switch: RunPod'da iptal edildi")
                raise
        settle(job_id, project_id)
        note_worker(job, body)
        h3_outputs.completed(server(), job_id, remote_meta(body))
        path = collect(body, job_id, image=True)
        forget(jkey, job_id, project_id)
    except Exception as e:
        _reraise(e)
    finally:
        vast_box.untrack(jkey)
    seconds = billed_seconds(body)
    gpu = (body.get("output") or {}).get("gpu")
    model = "qwen_edit" if kind == "edit_keyframe" else "qwen"
    cost = pricing.estimate_runpod(seconds, gpu) if seconds else pricing.estimate_keyframe(ENGINE, model=model,
                                                                                         edit_mode=job.edit_mode)
    return GenResult(local_path=path, request_id=job_id, cost_usd=cost, engine=ENGINE,
                     meta={"kind": kind, "gpu": gpu, "gpu_seconds": seconds, "references": job.names})


def keyframes_ready(kind: str = "keyframe") -> bool:
    """Storyboard ilk karesi RunPod'da çizilebilir mi (keyframe.json; karakterli kare için edit_keyframe.json var)."""
    return os.path.exists(vast_h3.workflow_path(kind))


def get_live_provider() -> RunpodProvider:
    """Sağlayıcı kurulurken kilit, uç nokta, bölge ve bakiye denetlenir: engelliyse hiçbir varyasyon satırı ya da
    rezervasyon oluşmaz."""
    preflight(guard(KEY))
    return RunpodProvider()


# ---------- çıktı güvencesi: yeniden indirme ----------

def retrieve_row(row: dict) -> str:
    """Kaydı RunPod'dan yeniden alır (sonuç tamamlandıktan sonra 30 dk tutulur). stored | failed | running | missing."""
    key = guard(KEY)
    image = row["kind"] == "keyframe"
    with _client(key) as c:
        try:
            body = status(c, row["job_id"])
        except httpx.HTTPStatusError as e:
            if _status(e) != 404:
                raise
            h3_outputs.lost(row["server"], row["job_id"], "RunPod işi tanımıyor (404): kuyruk boşaltılmış, iş iptal "
                            "edilmiş ya da sonucu silinmiş (tamamlandıktan 30 dk sonra)")
            return "missing"
    state = body.get("status")
    if state in ("IN_QUEUE", "IN_PROGRESS"):
        return "running"
    if state in FAILED or (state == "COMPLETED" and (not isinstance(body.get("output"), dict) or body["output"].get("error"))):
        h3_outputs.failed(row["server"], row["job_id"], f"RunPod işi {state}: {_failure(body)}")
        return "failed"
    h3_outputs.completed(row["server"], row["job_id"], remote_meta(body))
    path = collect(body, row["job_id"], image=image, extra={"retrieved": True})
    if os.path.exists(path):
        os.remove(path)
    return "stored"


# ---------- dakikalık görev: boşta bekleme bedeli, bakiye, ayar, yarım indirmeler ----------

def gpu_billed(key: str) -> bool:
    """RunPod şu an bir GPU worker'ı faturalıyor mu: hesabın harcama hızı (currentSpendPerHr) en ucuz GPU tipinin saat
    ücretinin yarısından küçükse hayır (yalnızca Network Volume diski, ör. 0,019 $/sa; FlashBoot ile uyuyan worker
    /health'te "idle" görünür ama ücret işlemez). Okunamazsa evet (iş bitişi penceresi yine sınırlar)."""
    try:
        rate = balance(key).get("spend_per_hr")
    except Exception:                                                # noqa: BLE001
        return True
    if rate is None:
        return True
    cheapest = min(float(v) for v in (pricing.serverless() or {}).get("per_second", {"x": 0.0}).values()) * 3600
    return float(rate) >= cheapest / 2


def idle_overlap(last_tick: float, now: float, job_end: float | None) -> float:
    """Faturalanan boşta süre: bu tik aralığının [son işin bitişi, bitiş + boşta bekleme] penceresiyle kesişimi. Worker son
    işten sonra en çok RUNPOD_IDLE_TIMEOUT_S açık kalır; o pencere dışındaki süre (işin kendisi — işin bedelinde — ya da
    kapanmakta olan worker'ın /health'te bir tik daha "idle" görünmesi) yazılmaz. Böylece art arda iki tik aynı
    60 sn'yi iki kez saymaz."""
    if not last_tick or job_end is None:
        return 0.0
    start = max(last_tick, job_end)
    stop = min(now, job_end + float(settings.runpod_idle_timeout_s))
    return max(0.0, min(stop - start, MAX_TICK_S))


def tick() -> str:
    """Beat görevi (`hg.runpod_tick`, dakikada bir). Boşta bekleyen worker'ların süresi son etkin projeye yazılır (en pahalı
    GPU tipinin ücretiyle: hangi tipte bekledikleri bilinmez), bakiye ve uç nokta ayarı tazelenir, worker'ın artık
    beklemediği yarım indirmeler yeniden denenir."""
    if settings.dry_run or settings.h3_platform != "runpod" or not settings.runpod_endpoint_id:
        return "skip"
    key = guard(KEY)
    r, now = killswitch.r(), time.time()
    raw = r.get(vast_box._k("rp_last_tick"))
    last = float(raw) if raw else 0.0
    r.set(vast_box._k("rp_last_tick"), now)
    h = health(key)
    idle = int((h.get("workers") or {}).get("idle") or 0)
    charged = 0.0
    end = r.get(vast_box._k("rp_job_end"))
    seconds = idle_overlap(last, now, float(end) if end else None)
    busy = vast_box.inflight(now) > 0          # iş sürerken worker'ın süresi işin bedelinde (soğuk başlatma dahil): çift sayım yok
    if idle and seconds >= 1 and not busy and not gpu_billed(key):
        seconds = 0.0                          # RunPod'un harcama hızı yalnızca disk: worker faturalanmıyor (FlashBoot uykusu)
    if idle and seconds >= 1 and not busy and (project := vast_box.last_project()):
        gpu = killswitch.r().get(vast_box._k("rp_last_gpu"))
        gpu = gpu.decode() if isinstance(gpu, bytes) else gpu
        cost = pricing.estimate_runpod(idle * seconds, gpu)     # son işi çalıştıran GPU'nun ücreti (yoksa birincil tip)
        try:
            with killswitch.spend(project, cost, "h3:idle", details={"idle_workers": idle, "idle_seconds": round(seconds, 1),
                                                                     "gpu": gpu}) as ch:
                ch.actual = cost
            charged = cost
        except killswitch.ProjectFrozen:
            pass
    try:
        balance(key, refresh=True)
        endpoint_config(key)
    except Exception as e:                                           # noqa: BLE001 — panel uyarısı; görev sürer
        log.warning("RunPod durumu okunamadı: %s", e)
    swept = None
    try:
        if h3_outputs.unsafe_count(instance()):
            swept = h3_outputs.retrieve(instance(), "", "", limit=h3_outputs.SWEEP_LIMIT, settle=True)
    except Exception as e:                                           # noqa: BLE001
        log.warning("RunPod çıktı taraması yapılamadı: %s", e)
    _cache_set("tick", {"idle_workers": idle, "charged_usd": charged, "swept": swept, "busy": busy})
    return f"idle {idle} worker, {charged:.4f} $"
