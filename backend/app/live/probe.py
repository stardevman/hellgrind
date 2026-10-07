"""Bağlantı sınaması: bir API anahtarının sağlayıcı tarafından kabul edilip edilmediğini, üretim yapmadan ve ücret
doğurmadan denetler. Yalnızca panelde "Bağlantıyı sına" düğmesine basılınca çalışır; testler bu modülün ağ çağrısını
çalıştırmaz (httpx taşıyıcısı sahtelenir). DRY_RUN'dan bağımsızdır çünkü ücretli bir işlem yapmaz: yalnızca hesap ya da
model listesini okuyan ücretsiz uçlara gider. MiniMax H3 için vast.ai'den makinenin durumu okunur (GET, ücretsiz) ve
yönetmenin kendi sunucusunun sağlık ucuna bakılır (/health ya da ComfyUI /system_stats; GPU'da iş başlatmaz). RunPod için
hesap bakiyesi (GraphQL), uç noktanın ayarı (veri merkezleri; izinli liste) ve sağlığı (worker ve kuyruk sayıları) okunur;
worker başlatmaz, iş göndermez.
"""
from __future__ import annotations

import httpx

from app.config import settings

TIMEOUT = 10.0
ZERO_REQUEST = "00000000-0000-0000-0000-000000000000"


def _request(service: str, key: str) -> httpx.Request:
    if service == "anthropic":                              # model listesi: ücretsiz
        return httpx.Request("GET", "https://api.anthropic.com/v1/models?limit=1",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if service == "elevenlabs":                             # hesap bilgisi: ücretsiz
        return httpx.Request("GET", "https://api.elevenlabs.io/v1/user", headers={"xi-api-key": key})
    if service == "fal":                                    # var olmayan bir isteğin durumu: kuyruğa iş girmez
        return httpx.Request("GET", f"https://queue.fal.run/{settings.fal_flux_endpoint}/requests/{ZERO_REQUEST}/status",
                             headers={"Authorization": f"Key {key}"})
    raise ValueError(f"Bilinmeyen servis: {service}")


def check(service: str, key: str, transport: httpx.BaseTransport | None = None) -> dict:
    """{"ok": bool | None, "status": int | None, "message": str}. ok=None: anahtar reddedilmedi ama kesin de değil."""
    if not key:
        return {"ok": False, "status": None, "message": "Anahtar tanımlı değil."}
    if service == "vast":
        return check_h3(key, transport)
    if service == "runpod":
        return check_runpod(key)
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport) as client:
            r = client.send(_request(service, key))
    except httpx.HTTPError as e:
        return {"ok": None, "status": None, "message": f"Sağlayıcıya ulaşılamadı: {type(e).__name__}"}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code, "message": "Anahtar reddedildi."}
    if r.status_code == 200 or (service == "fal" and r.status_code in (400, 404, 422)):
        return {"ok": True, "status": r.status_code, "message": "Anahtar kabul edildi."}
    if r.status_code == 429:
        return {"ok": None, "status": 429, "message": "Hız sınırı: anahtar geçerli olabilir, biraz sonra yeniden deneyin."}
    return {"ok": None, "status": r.status_code, "message": f"Beklenmeyen yanıt ({r.status_code}); sağlayıcının durum sayfasına bakın."}


def _h3_one(token: str, url: str, transport) -> dict:
    from app.live import tls_pin
    path = "/system_stats" if settings.h3_backend == "comfyui" else "/health"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    observer = tls_pin.observer_for(url) if transport is None else None
    options = {"transport": transport} if transport is not None else tls_pin.client_options(url, observer)
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True, **options) as client:
            r = client.get(f"{url.rstrip('/')}{path}", headers=headers)
    except tls_pin.PinMismatch as e:
        return {"ok": False, "status": None, "message": str(e)}
    except httpx.ConnectError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            return {"ok": False, "status": None, "message": "Sertifika doğrulanamadı (kendinden imzalı portal): TLS kipini "
                                                            "\"pin\" yapın."}
        how = "SSH tüneli açık mı?" if settings.h3_backend == "comfyui" else "makine çalışıyor mu, adres güncel mi?"
        return {"ok": False, "status": None, "message": f"Sunucuya bağlanılamadı: {how}"}
    except httpx.HTTPError as e:
        return {"ok": None, "status": None, "message": f"Sunucuya ulaşılamadı: {type(e).__name__}"}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code, "message": "Sunucu erişimi reddetti (portal belirteci)."}
    if r.status_code == 200:
        if observer is not None:
            observer.confirm()                       # ilk başarılı bağlantı: sertifika sabitlenir
        return {"ok": True, "status": 200, "message": "Sunucu hazır." if settings.h3_backend == "comfyui"
                else "Sunucu hazır (SGLang arka ucu deneysel)."}
    if r.status_code == 503:
        return {"ok": None, "status": 503, "message": "Sunucu açılıyor (model yükleniyor); birkaç dakika sonra yeniden deneyin."}
    if r.status_code == 530:
        return {"ok": False, "status": 530, "message": "Cloudflare tüneli kapalı: makine durmuş ya da adres değişmiş olabilir."}
    return {"ok": None, "status": r.status_code, "message": f"Beklenmeyen yanıt ({r.status_code}); sunucunun günlüğüne bakın."}


def _box_line(key: str, transport) -> tuple[bool | None, str]:
    """vast.ai'den makinenin durumu, saatlik ücreti, konumu ve GPU'su (ücretsiz, salt okunur)."""
    from app.live import PaidCallBlocked, vast_api
    try:
        box = vast_api.read(key, settings.vast_instance_id, transport=transport)
    except PaidCallBlocked as e:
        return False, str(e)
    except httpx.HTTPError as e:
        return None, f"vast.ai'ye ulaşılamadı ({type(e).__name__})"
    price = f"{box.dph:.2f} $/sa".replace(".", ",") if box.dph is not None else "ücret bilinmiyor"
    line = f"Makine: {box.status} · {price} · {box.geolocation or 'konum bilinmiyor'} · {box.gpu or 'GPU bilinmiyor'}"
    if box.excluded:
        return False, f"{line}. Bu konum izinli listede değil: üretim başlamaz (başka bölgeden makine seçin)."
    if not box.running:
        return None, f"{line}. Makine çalışmıyor."
    return True, line


def check_h3(key: str, transport: httpx.BaseTransport | None = None) -> dict:
    """vast.ai hesabı ve makine (tanımlıysa), sonra H3 sunucuları: FL2VA / ComfyUI (zorunlu) ve tanımlıysa Ref2VA. En kötü
    sonuç döner; mesaj hepsini anlatır. Hiçbiri iş başlatmaz."""
    from app.live import probe_key
    if not settings.h3_endpoint_url:
        return {"ok": False, "status": None, "message": "Sunucu adresi tanımlı değil."}
    token = probe_key("h3_api_key")
    messages, oks, status = [], [], None
    if settings.vast_instance_id:
        ok, line = _box_line(key, transport)
        oks.append(ok)
        messages.append(line)
    else:
        messages.append("Makine kimliği tanımlı değil: boşta durdurma, otomatik başlatma ve konum denetimi kapalı.")
        oks.append(None)
    main = _h3_one(token, settings.h3_endpoint_url, transport)
    oks.append(main["ok"])
    status = main["status"]
    messages.append(main["message"])
    if settings.h3_ref_endpoint_url and settings.h3_ref_endpoint_url != settings.h3_endpoint_url:
        ref = _h3_one(token, settings.h3_ref_endpoint_url, transport)
        oks.append(ref["ok"])
        messages.append(f"Ref2VA: {ref['message']}")
    ok = False if False in oks else (None if None in oks else True)
    return {"ok": ok, "status": status, "message": " ".join(messages)}


def check_runpod(key: str) -> dict:
    """RunPod: bakiye, uç nokta ayarı (izinli veri merkezleri) ve sağlık. Hepsi salt okunur; worker başlatmaz."""
    from app.live import runpod_h3
    messages, oks = [], []
    try:
        b = runpod_h3.balance(key, refresh=True)
        low = b["usd"] < float(settings.runpod_min_balance_usd)
        oks.append(False if low else True)
        messages.append(f"Bakiye {b['usd']:.2f} $".replace(".", ",") + (f" (eşik {settings.runpod_min_balance_usd} $ altında: "
                                                                       "yeni iş başlamaz)" if low else "") + ".")
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return {"ok": False, "status": e.response.status_code, "message": "RunPod API anahtarı reddedildi."}
        oks.append(None)
        messages.append(f"Bakiye okunamadı (HTTP {e.response.status_code}).")
    except Exception as e:                                       # noqa: BLE001
        oks.append(None)
        messages.append(f"Bakiye okunamadı ({type(e).__name__}).")
    if not settings.runpod_endpoint_id:
        return {"ok": False, "status": None, "message": " ".join(messages + ["Uç nokta kimliği tanımlı değil."])}
    try:
        cfg = runpod_h3.endpoint_config(key, refresh=True)
    except Exception as e:                                       # noqa: BLE001
        cfg = None
        messages.append(f"Uç nokta ayarı okunamadı ({type(e).__name__}).")
    problem = runpod_h3.region_problem(cfg)
    if problem:
        oks.append(False)
        messages.append(problem)
    elif cfg:
        oks.append(True)
        messages.append(f"Uç nokta: {', '.join(cfg['datacenters'])} · GPU {', '.join(cfg['pools']) or '?'} · en az "
                        f"{cfg['workers_min']} / en çok {cfg['workers_max']} worker · boşta {cfg['idle_timeout']} sn.")
    try:
        h = runpod_h3.health(key)
        w, j = h.get("workers") or {}, h.get("jobs") or {}
        oks.append(True)
        messages.append(f"Worker: {w.get('idle', 0)} boşta, {w.get('running', 0)} çalışıyor; kuyrukta {j.get('inQueue', 0)} iş.")
    except Exception as e:                                       # noqa: BLE001
        oks.append(None)
        messages.append(f"Uç nokta sağlığı okunamadı ({type(e).__name__}).")
    ok = False if False in oks else (None if None in oks else True)
    return {"ok": ok, "status": None, "message": " ".join(messages)}
