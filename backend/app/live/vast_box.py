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
- **Yedek makine:** otomatik başlatma başarısız olursa (durdurulan makinenin GPU'su başkasına kiralanmış olabilir) uygun
  bölgelerde aynı GPU tipinde, fiyat tavanı ve en az RAM/disk/bağlantı koşullarına uyan teklif aranır. Yönetmen panelde
  onaylarsa (ya da `h3_replace_mode=auto` ise) kiralanır, `vast/setup.sh` ile kurulur (ComfyUI 127.0.0.1'de, portal
  kimlik doğrulamalı), yeni kimlik ve adres Ayarlar'a yazılır; yeni makine hazır olunca eski durdurulmuş makine silinir.
- **İlk makine:** Ayarlar › Makine › "Yeni makine kirala" aynı akışı (teklif → onay → kiralama → setup.sh) ilk makine için
  çalıştırır; hiçbir makine silinmez. Kurulum günlüğü portalın kimlik doğrulamalı 8189 portundan panelde izlenir.
- **Çıktı güvencesi:** eski makine yalnızca ürettiği her çıktı yerel depoya indirilip doğrulanmışsa silinir
  (app/live/h3_outputs.py); eksik varsa makine silinmez ve panelde listelenir. Yönetmen eksikleri indirtebilir (makine
  gerekirse başlatılır) ya da kaybı kabul edip silebilir. Makine çalışırken boşta bekleme görevi eksikleri yeniden indirir.

Yalnızca ücretli modda çalışır; vast.ai API anahtarı ve makine kimliği yoksa yönetim kapalıdır (iş kaydı yine tutulur).
Redis anahtarları veritabanı adına göre ayrılır: testler geliştirme yığınının kaydına dokunmaz.
"""
import base64
import gzip
import json
import logging
import os
import secrets
import shlex
import time

from sqlalchemy.engine import make_url

from app.budget import killswitch, pricing
from app.config import settings
from app.live import PaidCallBlocked, h3_outputs, key_state, optional, vast_api

log = logging.getLogger("hellgrind.live.box")

STALE_S = 600            # kalp atışı bu kadar eskiyen iş kayıttan düşer (çökmüş worker)
MAX_TICK_S = 300         # beat aksarsa tek seferde en çok bu kadar süre yazılır
START_POLL_S = 10.0
BOOT_WAIT_S = 300        # başlatılan makine bu sürede "running" olmazsa GPU'su başkasına kiralanmış sayılır
START_GRACE_S = 60       # başlatma isteğinden hemen sonra durum bir süre "exited" kalabilir
SETUP_POLL_S = 30.0
BOX_CACHE_S = 600
SETUP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vast", "setup.sh")
COMFY_PORT = 8188        # portalın kimlik doğrulamalı portu; ComfyUI makinede 127.0.0.1:18188'de
LOG_PORT = 8189          # kurulum günlüğü (setup.sh, makinede 127.0.0.1:18189); portal kimlik doğrular
PORTAL_CONFIG = ("localhost:1111:11111:/:Instance Portal|localhost:8188:18188:/:ComfyUI"
                 "|localhost:8189:18189:/:Hellgrind kurulum")
ACTIVE_JOB = ("queued", "renting", "booting", "installing")
JOB_TR = {"queued": "sırada", "renting": "kiralanıyor", "booting": "açılıyor", "installing": "kuruluyor (modeller iniyor)",
          "ready": "hazır", "failed": "başarısız"}
RETRIEVAL_TR = {"starting": "eski makine başlatılıyor", "waiting": "sunucu bekleniyor", "downloading": "indiriliyor",
                "done": "bitti", "failed": "başarısız"}
LOG_LINES = 120


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
    """Üretimden önce: makine çalışıyor mu, bölgesi uygun mu; kapalıysa ve izin varsa başlatır. Başlatma başarısız olursa
    yedek makine önerir ya da kiralar (her durumda bu çağrı Türkçe bir PaidCallBlocked ile durur: yeni makinenin kurulumu
    uzun sürer). `healthy()` sunucunun hazır olup olmadığını söyler. Yönetim kapalıysa hiçbir şey yapmaz."""
    if not configured():
        return None
    box = _cache(vast_api.instance())
    if (problem := territory_problem(box)) is not None:
        raise PaidCallBlocked(problem)
    if box.running:
        return box
    current = job()
    if current and current.get("state") in ACTIVE_JOB:
        raise PaidCallBlocked(f"Yeni H3 makinesi {JOB_TR[current['state']]}: bitince sahneyi yeniden üretin "
                              "(Ayarlar › MiniMax H3 › Makine).")
    if not settings.h3_auto_start:
        raise PaidCallBlocked(f"vast.ai makinesi çalışmıyor (durum: {box.status}): Ayarlar › MiniMax H3'ten başlatın ya da "
                              "otomatik başlatmayı açın.")
    r = _r()
    if r.set(_k("starting"), time.time(), nx=True, ex=max(60, int(settings.h3_start_timeout_s))):
        try:
            vast_api.set_state("running")
        except PaidCallBlocked:
            raise
        except Exception as e:                       # noqa: BLE001 — başlatılamayan makine: yedek makineye geçilir
            r.delete(_k("starting"))
            replace_or_propose(f"vast.ai makineyi başlatamadı ({type(e).__name__}: {str(e)[:120]}).")
        _event("h3_started", reason="auto_start")
    boot_started = time.monotonic()
    while True:
        box = _cache(vast_api.instance())
        if box.running:
            break
        elapsed = time.monotonic() - boot_started
        if (box.status in ("exited", "offline", "unknown") and elapsed >= START_GRACE_S) or elapsed >= BOOT_WAIT_S:
            r.delete(_k("starting"))
            replace_or_propose(f"Durdurulmuş makine {BOOT_WAIT_S // 60} dakikada açılmadı (durum: {box.status}; GPU'su "
                               "başkasına kiralanmış olabilir).")
        time.sleep(START_POLL_S)
    deadline = time.monotonic() + float(settings.h3_start_timeout_s)
    while time.monotonic() < deadline:
        try:
            if healthy():
                r.delete(_k("starting"))
                touch()
                return box
        except Exception as e:                       # noqa: BLE001 — sunucu açılırken bağlantı hataları beklenir
            log.info("H3 sunucusu henüz hazır değil: %s", e)
        time.sleep(START_POLL_S)
    raise PaidCallBlocked(f"vast.ai makinesi açıldı ama sunucu {settings.h3_start_timeout_s} sn içinde hazır olmadı: "
                          "sunucunun makine açılışında kendiliğinden başladığını (supervisor ya da onstart) kontrol edin.")


# ---------- yedek makine ----------

def _json(name: str) -> dict | None:
    raw = _r().get(_k(name))
    return json.loads(raw) if raw else None


def proposal() -> dict | None:
    """Onay bekleyen yedek makine önerisi."""
    return _json("proposal")


def job() -> dict | None:
    """Yedek makinenin kiralanma ve kurulum durumu."""
    return _json("replacement")


def _set_job(**fields) -> dict:
    current = {**(job() or {}), **fields, "updated_at": time.time()}
    if "state" in fields:                                   # aşama süreleri (kurulumu ölçmek için)
        current["steps"] = {**(current.get("steps") or {}), fields["state"]: time.time()}
    _r().set(_k("replacement"), json.dumps(current, ensure_ascii=False), ex=7 * 24 * 3600)
    return current


def conditions() -> str:
    return (f"{settings.h3_gpu_name or (cached() or {}).get('gpu') or 'aynı GPU'}, en çok {settings.h3_max_dph} $/sa, "
            f"en az {settings.h3_min_ram_gb} GB RAM, {settings.h3_min_disk_gb} GB disk, {settings.h3_min_inet_mbps} Mbps, "
            "AB/BK/G. Kore/ABD dışı")


def find_offer() -> vast_api.Offer | None:
    gpu = settings.h3_gpu_name or (cached() or {}).get("gpu") or "RTX 5090"
    offers = vast_api.search_offers(gpu, float(settings.h3_max_dph), int(settings.h3_min_ram_gb),
                                    int(settings.h3_min_disk_gb), int(settings.h3_min_inet_mbps))
    return offers[0] if offers else None


def _propose(offer: vast_api.Offer, reason: str, kind: str, old: str | None) -> dict:
    prop = {"offer_id": offer.id, "gpu": offer.gpu, "dph": offer.dph, "geolocation": offer.geolocation,
            "ram_gb": offer.ram_gb, "disk_gb": offer.disk_gb, "reliability": offer.reliability, "inet_mbps": offer.inet_mbps,
            "reason": reason, "kind": kind, "old_instance_id": old or None, "at": time.time()}
    _r().set(_k("proposal"), json.dumps(prop, ensure_ascii=False), ex=24 * 3600)
    return prop


def propose_initial() -> dict:
    """Ayarlar › Makine › "Yeni makine kirala": uygun teklifi bulur ve onaya sunar (her zaman sorulur; hiçbir makine
    silinmez). Uygun teklif yoksa koşulları anlatan PaidCallBlocked."""
    offer = find_offer()
    if offer is None:
        raise PaidCallBlocked(f"Uygun teklif bulunamadı ({conditions()}); koşulları Ayarlar › MiniMax H3'ten gevşetin.")
    current = f" Ayarlardaki makine ({settings.vast_instance_id}) silinmez." if settings.vast_instance_id else ""
    prop = _propose(offer, f"Yeni makine (Ayarlar'dan istendi).{current}", "initial", None)
    _event("h3_replacement_proposed", gpu=offer.gpu, dph=offer.dph, geolocation=offer.geolocation, initial=True)
    return prop


def replace_or_propose(reason: str):
    """Başlatılamayan makinenin yerine: kapalıysa durur; uygun teklif yoksa koşulları anlatır; varsa öneri kaydedilir ve
    yönetmenin onayı beklenir ya da (auto) kiralama hemen başlar. Her durumda PaidCallBlocked fırlatır."""
    mode = settings.h3_replace_mode
    if mode == "off":
        raise PaidCallBlocked(f"{reason} Yedek makine kapalı: Ayarlar › MiniMax H3 › Yedek makine.")
    offer = find_offer()
    if offer is None:
        raise PaidCallBlocked(f"{reason} Uygun yedek teklif bulunamadı ({conditions()}); koşulları Ayarlar'dan gevşetin.")
    prop = _propose(offer, reason, "replacement", settings.vast_instance_id)
    where = f"{offer.gpu}, {offer.dph:.2f} $/sa, {offer.geolocation}"
    if mode == "auto":
        start_replacement(prop)
        raise PaidCallBlocked(f"{reason} Yeni makine kiralanıyor ({where}); kurulum (modeller ~100 GB) 30-60 dk sürebilir, "
                              "bitince sahneyi yeniden üretin.")
    _event("h3_replacement_proposed", gpu=offer.gpu, dph=offer.dph, geolocation=offer.geolocation)
    raise PaidCallBlocked(f"{reason} Yeni makine önerisi onay bekliyor ({where}): Ayarlar › MiniMax H3 › Makine.")


def start_replacement(prop: dict) -> dict:
    """Kiralama ve kurulum işini kuyruğa verir (worker'da `replace`)."""
    from app.worker.celery_app import celery
    _r().delete(_k("replacement"))                           # önceki işin alanları (günlük adresi, ilerleme) taşınmasın
    state = _set_job(state="queued", offer=prop, kind=prop.get("kind") or "replacement",
                     old_instance_id=prop.get("old_instance_id"), new_instance_id=None, message=None, url=None,
                     log_url=None, retained=None, started_at=time.time(), steps={"queued": time.time()})
    celery.send_task("hg.h3_replace", args=[prop["offer_id"]])
    _event("h3_replacing", gpu=prop.get("gpu"), dph=prop.get("dph"), geolocation=prop.get("geolocation"))
    return state


def onstart_script() -> str:
    """Yeni makinenin açılış komutu: setup.sh'yi yazar (Ayarlar'daki adresten ya da isteğe gömülü, gzip + base64), arka
    planda çalıştırır ve temel imajın açılışını (portal, supervisor) başlatır. vast.ai onstart sınırı 16 KB'tır."""
    target = "/root/hellgrind/setup.sh"
    if settings.h3_setup_script_url:
        write = f"curl -fsSL {shlex.quote(settings.h3_setup_script_url)} -o {target}"
    else:
        with open(SETUP_SCRIPT, "rb") as f:
            blob = base64.b64encode(gzip.compress(f.read(), mtime=0)).decode("ascii")
        write = f"echo {blob} | base64 -d | gunzip > {target}"
    return (f"mkdir -p /root/hellgrind && {write} && chmod +x {target} && (nohup {target} >/dev/null 2>&1 &); "
            "entrypoint.sh")


def rent_body(token: str) -> dict:
    return {"image": settings.h3_box_image, "disk": int(settings.h3_min_disk_gb), "runtype": "ssh_direct",
            "label": "hellgrind-h3",
            "env": {"WEB_PASSWORD": token, "OPEN_BUTTON_TOKEN": token, "ENABLE_AUTH": "true", "ENABLE_HTTPS": "true",
                    "PORTAL_CONFIG": PORTAL_CONFIG, "OPEN_BUTTON_PORT": "1111", "HG_IMAGE_MODEL": settings.h3_image_model,
                    "HG_LOG_PORT": str(LOG_PORT + 10000),
                    "-p 1111:1111": "1", f"-p {COMFY_PORT}:{COMFY_PORT}": "1", f"-p {LOG_PORT}:{LOG_PORT}": "1"},
            "onstart": onstart_script()}


def save_settings(values: dict[str, str], detail: str) -> None:
    """Yeni makinenin ayarlarını .env'e yazar (bütün süreçler tazelenir); dosya bağlı değilse yalnızca bu sürece uygular."""
    from app import runtime_config as rc
    try:
        rc.update(values)
    except rc.ConfigError:
        log.warning("ayar dosyası yazılamadı (%s); yalnızca bu süreçte geçerli", detail)
        for env, value in values.items():
            f = rc.BY_ENV[env]
            setattr(settings, f.attr, rc._typed(f, value))


def replace(offer_id: int) -> str:
    """Worker görevi: teklifi kiralar, açılmasını ve kurulumun bitmesini bekler, Ayarlar'ı günceller, eski makineyi siler."""
    from app.live import vast_h3
    if settings.dry_run:
        return "skip"
    from app.live import tls_pin
    r = _r()
    current = job() or {}                                   # yalnızca öneride adı geçen makine (ilk makinede yok)
    old = current["old_instance_id"] if "old_instance_id" in current else (proposal() or {}).get("old_instance_id")
    r.set(_k("replacing"), time.time(), ex=int(settings.h3_setup_timeout_s) + 3600)
    new_id = None
    try:
        token = optional("h3_api_key") or secrets.token_urlsafe(24)
        _set_job(state="renting", old_instance_id=old)
        new_id = vast_api.rent(int(offer_id), rent_body(token))
        save_settings({"VAST_INSTANCE_ID": new_id, "H3_API_KEY": token, "H3_BACKEND": "comfyui", "H3_TLS_MODE": "pin",
                       "H3_REF_ENDPOINT_URL": ""}, "yeni makine kiralandı")
        r.delete(_k("proposal"))
        _set_job(state="booting", new_instance_id=new_id)
        deadline = time.monotonic() + max(BOOT_WAIT_S * 4, 1200)
        while True:
            box = _cache(vast_api.instance(new_id))
            port = box.host_port(COMFY_PORT)
            if box.running and box.ip and port:
                break
            if box.status in ("exited", "offline") or time.monotonic() >= deadline:
                raise RuntimeError(f"yeni makine açılmadı (durum: {box.status}, adres: {box.ip}:{port})")
            time.sleep(START_POLL_S)
        url = f"https://{box.ip}:{port}"
        log_port = box.host_port(LOG_PORT)
        tls_pin.forget(tls_pin.endpoint_of(url))               # aynı adreste eski bir makinenin parmak izi kalmasın
        if log_port:
            tls_pin.forget(f"{box.ip}:{log_port}")
        save_settings({"H3_ENDPOINT_URL": url}, "yeni makinenin adresi")
        _set_job(state="installing", url=url, log_url=f"https://{box.ip}:{log_port}" if log_port else None)
        deadline = time.monotonic() + float(settings.h3_setup_timeout_s)
        while not _healthy(vast_h3, url, token):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"kurulum {settings.h3_setup_timeout_s} sn içinde bitmedi (makinede "
                                   "/var/log/hellgrind/setup.log)")
            time.sleep(SETUP_POLL_S)
        touch()
        message = f"Yeni makine hazır ({url})."
        if old and str(old) != str(new_id):
            message += " " + _retire(old)
        _set_job(state="ready", message=message)
        _event("h3_replaced", new_instance_id=new_id, old_instance_id=old)
        return "ready"
    except Exception as e:                                       # noqa: BLE001
        message = f"{type(e).__name__}: {str(e)[:300]}"
        _set_job(state="failed", message=message)
        _event("h3_replace_failed", error=message)
        if new_id:
            try:
                vast_api.set_state("stopped", new_id)            # yarım kalan yeni makine para yakmasın
            except Exception:                                    # noqa: BLE001
                log.warning("yarım kalan yeni makine durdurulamadı: %s", new_id)
        return "failed"
    finally:
        r.delete(_k("replacing"))


def _retire(old: str) -> str:
    """Eski makineyi siler: yalnızca ürettiği her çıktı yerel depoda doğrulanmışsa ve makine çalışmıyorsa. Eksik çıktı
    varsa makine kalır, panelde listelenir (indir ya da kaybı kabul et)."""
    missing = h3_outputs.unsafe_count(old)
    if missing:
        _set_job(retained={"instance_id": old, "missing": missing})
        _event("h3_outputs_retained", instance_id=old, missing=missing)
        return (f"Eski makine {old} SİLİNMEDİ: ürettiği {missing} çıktı yerel depoda yok (Ayarlar › MiniMax H3 › Makine › "
                "Çıktı güvencesi).")
    try:
        if vast_api.instance(old).running:                       # çalışan makine silinmez
            return f"Eski makine {old} çalışıyor; silinmedi."
        vast_api.destroy(old)
    except PaidCallBlocked:                                      # eski makine zaten yok
        return f"Eski makine {old} vast.ai'de bulunamadı."
    return f"Eski makine {old} silindi (bütün çıktıları yerel depoda doğrulandı)."


def _set_retrieval(**fields) -> dict:
    current = {**(retrieval() or {}), **fields, "updated_at": time.time()}
    _r().set(_k("retrieval"), json.dumps(current, ensure_ascii=False), ex=7 * 24 * 3600)
    return current


def retrieval() -> dict | None:
    """Eksik çıktıların yeniden indirilme durumu."""
    return _json("retrieval")


def start_retrieval(instance_id: str, destroy_after: bool) -> dict:
    from app.worker.celery_app import celery
    _r().delete(_k("retrieval"))
    state = _set_retrieval(state="starting", instance_id=str(instance_id), destroy_after=destroy_after, message=None,
                           counts=None, started_at=time.time())
    celery.send_task("hg.h3_retrieve", args=[str(instance_id), bool(destroy_after)])
    return state


def retrieve_outputs(instance_id: str, destroy_after: bool = False) -> str:
    """Worker görevi: makinenin yerel depoda olmayan çıktılarını indirir. Makine çalışmıyorsa başlatılır (yönetmenin
    isteği; saatlik ücret işler) ve iş bitince yeniden durdurulur. destroy_after: hepsi indirildiyse eski makine silinir
    (Ayarlar'daki makine hiçbir zaman silinmez)."""
    from app.live import vast_h3
    if settings.dry_run:
        return "skip"
    instance_id = str(instance_id)
    current = instance_id == str(settings.vast_instance_id)
    started = False
    try:
        token = optional("h3_api_key")
        box = vast_api.instance(instance_id)
        if not box.running:
            _set_retrieval(state="starting")
            vast_api.set_state("running", instance_id)
            started = True
            boot_started = time.monotonic()
            while True:
                box = vast_api.instance(instance_id)
                if box.running:
                    break
                elapsed = time.monotonic() - boot_started
                if (box.status in ("exited", "offline") and elapsed >= START_GRACE_S) or elapsed >= BOOT_WAIT_S:
                    raise RuntimeError(f"makine {BOOT_WAIT_S // 60} dakikada açılmadı (durum: {box.status}; GPU'su "
                                       "başkasına kiralanmış olabilir)")
                time.sleep(START_POLL_S)
        port = box.host_port(COMFY_PORT)
        url = settings.h3_endpoint_url if current or not (box.ip and port) else f"https://{box.ip}:{port}"
        _set_retrieval(state="waiting", url=url)
        deadline = time.monotonic() + float(settings.h3_start_timeout_s)
        while not _healthy(vast_h3, url, token):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"sunucu {settings.h3_start_timeout_s} sn içinde hazır olmadı")
            time.sleep(START_POLL_S)
        _set_retrieval(state="downloading")
        counts = h3_outputs.retrieve(instance_id, url, token)
        message = f"{counts['stored']} çıktı indirildi ve doğrulandı; {counts['remaining']} eksik kaldı."
        if counts["remaining"] == 0 and destroy_after and not current:
            vast_api.destroy(instance_id)
            started = False
            message += f" Eski makine {instance_id} silindi."
        _set_retrieval(state="done", counts=counts, message=message)
        _event("h3_outputs_retrieved", instance_id=instance_id, stored=counts["stored"], remaining=counts["remaining"])
        return "done"
    except Exception as e:                                       # noqa: BLE001
        message = f"{type(e).__name__}: {str(e)[:300]}"
        _set_retrieval(state="failed", message=message)
        return "failed"
    finally:
        if started and not current:
            try:
                vast_api.set_state("stopped", instance_id)       # eski makine para yakmasın
            except Exception:                                    # noqa: BLE001
                log.warning("eski makine yeniden durdurulamadı: %s", instance_id)


def abandon(instance_id: str) -> dict:
    """Yönetmen kaybı kabul etti: eksik çıktılar `lost` olur ve eski makine silinir (Ayarlar'daki makine silinmez)."""
    instance_id = str(instance_id)
    if instance_id == str(settings.vast_instance_id):
        raise PaidCallBlocked("Ayarlardaki makine buradan silinmez; önce yeni makineye geçin.")
    if vast_api.instance(instance_id).running:
        raise PaidCallBlocked(f"Makine {instance_id} çalışıyor: önce eksikleri indirin ya da makineyi durdurun.")
    lost = h3_outputs.mark_lost(instance_id)
    vast_api.destroy(instance_id)
    _event("h3_outputs_abandoned", instance_id=instance_id, lost=lost)
    current = job()
    if current and (current.get("retained") or {}).get("instance_id") == instance_id:
        _set_job(retained=None, message=f"{current.get('message') or ''} Eski makine {instance_id} silindi "
                                        f"({lost} çıktının kaybı kabul edildi).".strip())
    return {"instance_id": instance_id, "lost": lost}


def setup_log() -> dict:
    """Yeni makinenin kurulum günlüğü (setup.sh), portalın kimlik doğrulamalı günlük portundan: son satırlar, aşama süreleri
    ve kurulum bittiyse toplam süre."""
    from app.live import vast_h3
    current = job() or {}
    url = current.get("log_url")
    if not url:
        return {"available": False, "lines": [], "message": "Makine açılınca günlük görünür."}
    from app.live import tls_pin
    token = optional("h3_api_key")
    observer = tls_pin.observer_for(url) if vast_h3.TRANSPORT is None else None
    with vast_h3._client(url, token, observer) as c:
        r = c.get("/setup.log", headers={"Range": "bytes=-65536"})
    if r.status_code in (200, 206) and observer is not None:
        observer.confirm()                                       # günlük portunun sertifikası ilk başarıda sabitlenir
    if r.status_code not in (200, 206):
        return {"available": False, "lines": [], "message": f"Günlük okunamadı (HTTP {r.status_code}); kurulum betiği "
                                                            "henüz başlamamış olabilir."}
    lines = r.text.splitlines()
    if r.status_code == 206 and lines:
        lines = lines[1:]                                        # yarım ilk satır
    return {"available": True, "lines": lines[-LOG_LINES:], **parse_setup_log(lines)}


def parse_setup_log(lines: list[str]) -> dict:
    """setup.sh satırları: "[hellgrind 12:03:04 +5 dk 12 sn] ..."; bitişte "KURULUM_BITTI durum=ok sure=1234"."""
    import re
    out: dict = {"step": None, "done": False, "ok": None, "total_s": None, "phases": []}
    for line in lines:
        if "[hellgrind" not in line:
            continue
        if m := re.search(r"KURULUM_BITTI durum=(\w+) sure=(\d+)", line):
            out.update(done=True, ok=m.group(1) == "ok", total_s=int(m.group(2)))
        elif m := re.search(r"== (.+?) bitti \((\d+) sn\) ==", line):
            out["phases"].append({"name": m.group(1), "seconds": int(m.group(2))})
        elif m := re.search(r"\] == (.+?) ==$", line):
            out["step"] = m.group(1)
    return out


def _healthy(vast_h3, url: str, token: str) -> bool:
    try:
        return vast_h3.healthy(url, token)
    except Exception:                                            # noqa: BLE001 — kurulum sürerken bağlantı hatası olağan
        return False


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


def sweep() -> dict | None:
    """Çalışan makinede yarım kalmış indirmeleri (worker çöktü, indirme hatası) yeniden dener; süren işlere dokunmaz."""
    try:
        if not h3_outputs.unsafe_count(settings.vast_instance_id):
            return None
        counts = h3_outputs.retrieve(settings.vast_instance_id, settings.h3_endpoint_url.rstrip("/"),
                                     optional("h3_api_key"), limit=h3_outputs.SWEEP_LIMIT, settle=True)
    except Exception as e:                                       # noqa: BLE001 — boşta bekleme yine işler
        log.warning("çıktı taraması yapılamadı: %s", e)
        return None
    if counts["stored"]:
        _event("h3_outputs_retrieved", instance_id=settings.vast_instance_id, stored=counts["stored"],
               remaining=counts["remaining"])
    return counts


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
    sweep()
    raw = r.get(_k("last_activity"))
    if not raw:                                       # makine çalışıyor ama hiç iş görülmedi: süre şimdi başlar
        r.set(_k("last_activity"), now)
        return "idle 0s"
    idle_since = float(raw)
    seconds = min(now - last_tick, MAX_TICK_S, now - idle_since) if last_tick else 0.0
    if seconds >= 1:
        charge_idle(seconds)
    minutes = int(settings.h3_idle_stop_minutes or 0)
    if r.get(_k("replacing")):                        # yeni makine kuruluyor: indirme sürerken durdurulmaz
        return f"kuruluyor {now - idle_since:.0f}s"
    if minutes > 0 and now - idle_since >= minutes * 60:
        try:
            empty = queue_empty() if queue_empty else True
        except Exception:                            # noqa: BLE001 — kuyruk okunamazsa durdurma ertelenir
            empty = False
        if empty:
            stop("idle")
            return "stopped"
    return f"idle {now - idle_since:.0f}s"
