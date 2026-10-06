"""Bağlantı sınaması: bir API anahtarının sağlayıcı tarafından kabul edilip edilmediğini, üretim yapmadan ve ücret
doğurmadan denetler. Yalnızca panelde "Bağlantıyı sına" düğmesine basılınca çalışır; testler bu modülün ağ çağrısını
çalıştırmaz (httpx taşıyıcısı sahtelenir). DRY_RUN'dan bağımsızdır çünkü ücretli bir işlem yapmaz: yalnızca hesap ya da
model listesini okuyan ücretsiz uçlara gider. MiniMax H3 için vast.ai'den makinenin durumu okunur (GET, ücretsiz) ve
yönetmenin kendi sunucusunun sağlık ucuna bakılır (/health ya da ComfyUI /system_stats; GPU'da iş başlatmaz).
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
    path = "/system_stats" if settings.h3_backend == "comfyui" else "/health"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True) as client:
            r = client.get(f"{url.rstrip('/')}{path}", headers=headers)
    except httpx.ConnectError:
        how = "SSH tüneli açık mı?" if settings.h3_backend == "comfyui" else "makine çalışıyor mu, adres güncel mi?"
        return {"ok": False, "status": None, "message": f"Sunucuya bağlanılamadı: {how}"}
    except httpx.HTTPError as e:
        return {"ok": None, "status": None, "message": f"Sunucuya ulaşılamadı: {type(e).__name__}"}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code, "message": "Sunucu erişimi reddetti (portal belirteci)."}
    if r.status_code == 200:
        return {"ok": True, "status": 200, "message": "Sunucu hazır."}
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
        if settings.h3_territory_authorized:
            return True, f"{line} (lisansın dışladığı bölge; izin alındı olarak işaretli)"
        return False, f"{line}. Bu bölge MiniMax H3 lisansının dışında: üretim başlamaz (başka bölgeden makine seçin)."
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
