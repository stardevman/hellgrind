"""H3 makinesinin yönetimi (vast.ai): iş kaydı, boşta bekleme bedeli, boşta durdurma, otomatik başlatma, bölge denetimi.

- **İş kaydı:** her H3 işi sürerken Redis'te kayıtlıdır (kalp atışıyla); son etkinlik zamanı ve son etkin proje tutulur.
- **Boşta bekleme:** beat görevi (`hg.h3_idle`, dakikada bir) makine çalışırken kuyruk boşsa geçen süreyi saatlik ücretle
  son etkin projeye yazar (`h3:idle`, bütçe rezervasyonundan geçer; Kill-Switch ve hız bekçisi bunu görür). Bütçe
  yetmezse makine hemen durdurulur.
- **Boşta durdurma:** kuyruk `h3_idle_stop_minutes` dakika boş kalırsa (ve sunucunun kendi kuyruğu da boşsa) makine
  vast.ai API'siyle durdurulur.
- **Otomatik başlatma:** `h3_auto_start` açıksa iş geldiğinde durmuş makine başlatılır ve sunucu hazır olana dek beklenir.
- **Bölge:** makine lisansın dışladığı bir bölgedeyse (AB, BK, G. Kore, ABD) yönetmen izin aldığını bildirmedikçe üretim
  başlamaz.

Yalnızca ücretli modda çalışır; vast.ai API anahtarı ve makine kimliği yoksa yönetim kapalıdır (iş kaydı yine tutulur).
Redis anahtarları veritabanı adına göre ayrılır: testler geliştirme yığınının kaydına dokunmaz.
"""
import json
import logging
import time

from sqlalchemy.engine import make_url

from app.budget import killswitch, pricing
from app.config import settings
from app.live import PaidCallBlocked, key_state, vast_api

log = logging.getLogger("hellgrind.live.box")

STALE_S = 600            # kalp atışı bu kadar eskiyen iş kayıttan düşer (çökmüş worker)
MAX_TICK_S = 300         # beat aksarsa tek seferde en çok bu kadar süre yazılır
START_POLL_S = 10.0
BOX_CACHE_S = 600


def _prefix() -> str:
    return f"hg:h3:{make_url(settings.database_url).database}:"


def _k(name: str) -> str:
    return _prefix() + name


def _r():
    return killswitch.r()


def configured() -> bool:
    return bool(settings.vast_instance_id) and key_state("vast_api_key")["set"]


# ---------- iş kaydı ----------

def touch(project_id: str | None = None) -> None:
    r = _r()
    r.set(_k("last_activity"), time.time())
    if project_id:
        r.set(_k("last_project"), str(project_id))


def track(job_key: str, project_id: str | None = None) -> None:
    _r().zadd(_k("inflight"), {job_key: time.time()})
    touch(project_id)


def heartbeat(job_key: str) -> None:
    _r().zadd(_k("inflight"), {job_key: time.time()})
    touch()


def untrack(job_key: str) -> None:
    _r().zrem(_k("inflight"), job_key)
    touch()


def inflight(now: float | None = None) -> int:
    r, now = _r(), now or time.time()
    r.zremrangebyscore(_k("inflight"), "-inf", now - STALE_S)
    return int(r.zcard(_k("inflight")))


def last_project() -> str | None:
    value = _r().get(_k("last_project"))
    return value.decode() if isinstance(value, bytes) else value


# ---------- makine ----------

def _cache(box: vast_api.Box) -> vast_api.Box:
    _r().set(_k("box"), json.dumps({"status": box.status, "geolocation": box.geolocation, "dph": box.dph,
                                    "gpu": box.gpu, "at": time.time()}), ex=BOX_CACHE_S)
    return box


def cached() -> dict | None:
    """Son okunan makine bilgisi (klip meta verisindeki sunucu konumu için)."""
    raw = _r().get(_k("box"))
    return json.loads(raw) if raw else None


def territory_problem(box: vast_api.Box) -> str | None:
    if box.excluded and not settings.h3_territory_authorized:
        return (f"H3 makinesi lisansın dışladığı bir bölgede ({box.geolocation}): MiniMax H3 Community License AB, Birleşik "
                "Krallık, Güney Kore ve ABD'de kullanımı ayrı izin olmadan kapsamaz. Başka bölgeden makine seçin ya da "
                "MiniMax'ten izin aldıysanız Ayarlar › MiniMax H3 › Bölge izni'ni açın.")
    return None


def ensure_running(healthy) -> vast_api.Box | None:
    """Üretimden önce: makine çalışıyor mu, bölgesi uygun mu; kapalıysa ve izin varsa başlatır. `healthy()` sunucunun
    hazır olup olmadığını söyler. Yönetim kapalıysa (anahtar ya da kimlik yok) hiçbir şey yapmaz."""
    if not configured():
        return None
    box = _cache(vast_api.instance())
    if (problem := territory_problem(box)) is not None:
        raise PaidCallBlocked(problem)
    if box.running:
        return box
    if not settings.h3_auto_start:
        raise PaidCallBlocked(f"vast.ai makinesi çalışmıyor (durum: {box.status}): Ayarlar › MiniMax H3'ten başlatın ya da "
                              "otomatik başlatmayı açın.")
    r = _r()
    if r.set(_k("starting"), time.time(), nx=True, ex=max(60, int(settings.h3_start_timeout_s))):
        vast_api.set_state("running")
        _event("h3_started", reason="auto_start")
    deadline = time.monotonic() + float(settings.h3_start_timeout_s)
    while time.monotonic() < deadline:
        time.sleep(START_POLL_S)
        try:
            if healthy():
                r.delete(_k("starting"))
                touch()
                return _cache(vast_api.instance())
        except Exception as e:                       # noqa: BLE001 — sunucu açılırken bağlantı hataları beklenir
            log.info("H3 sunucusu henüz hazır değil: %s", e)
    raise PaidCallBlocked(f"vast.ai makinesi başlatıldı ama sunucu {settings.h3_start_timeout_s} sn içinde hazır olmadı: "
                          "sunucunun makine açılışında kendiliğinden başladığını (supervisor ya da onstart) kontrol edin.")


def stop(reason: str) -> None:
    vast_api.set_state("stopped")
    r = _r()
    r.delete(_k("starting"))
    r.set(_k("last_activity"), time.time())
    _event("h3_stopped", reason=reason)


def _event(kind: str, **data) -> None:
    project = last_project()
    if project:
        try:
            killswitch.event(project, kind, **data)
        except Exception:                            # noqa: BLE001 — olay yazılamasa da yönetim sürer
            log.warning("olay yazılamadı: %s", kind)


# ---------- boşta bekleme ----------

def charge_idle(seconds: float) -> float:
    """Boşta geçen süreyi son etkin projenin bütçesine yazar. Bütçe ya da proje durumu izin vermezse makineyi durdurur."""
    project = last_project()
    cost = pricing.estimate_gpu("h3", seconds)
    if not project or cost <= 0:
        return 0.0
    try:
        with killswitch.spend(project, cost, "h3:idle", details={"idle_seconds": round(seconds, 1)}) as charge:
            charge.actual = cost
    except killswitch.ProjectFrozen:
        stop("budget")
        return 0.0
    return cost


def idle_tick(queue_empty=None) -> str:
    """Beat görevi: boşta bekleme bedeli ve boşta durdurma. `queue_empty()` sunucunun kendi kuyruğuna bakar."""
    if settings.dry_run or not configured():
        return "skip"
    r, now = _r(), time.time()
    raw = r.get(_k("tick"))
    last_tick = float(raw) if raw else 0.0
    r.set(_k("tick"), now)
    box = _cache(vast_api.instance())
    if not box.running:
        return f"makine {box.status}"
    if inflight(now):
        return "busy"
    raw = r.get(_k("last_activity"))
    if not raw:                                       # makine çalışıyor ama hiç iş görülmedi: süre şimdi başlar
        r.set(_k("last_activity"), now)
        return "idle 0s"
    idle_since = float(raw)
    seconds = min(now - last_tick, MAX_TICK_S, now - idle_since) if last_tick else 0.0
    if seconds >= 1:
        charge_idle(seconds)
    minutes = int(settings.h3_idle_stop_minutes or 0)
    if minutes > 0 and now - idle_since >= minutes * 60:
        try:
            empty = queue_empty() if queue_empty else True
        except Exception:                            # noqa: BLE001 — kuyruk okunamazsa durdurma ertelenir
            empty = False
        if empty:
            stop("idle")
            return "stopped"
    return f"idle {now - idle_since:.0f}s"
