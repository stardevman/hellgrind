"""Bağlantı sınaması: bir API anahtarının sağlayıcı tarafından kabul edilip edilmediğini, üretim yapmadan ve ücret
doğurmadan denetler. Yalnızca panelde "Bağlantıyı sına" düğmesine basılınca çalışır; testler bu modülün ağ çağrısını
çalıştırmaz (httpx taşıyıcısı sahtelenir). DRY_RUN'dan bağımsızdır çünkü ücretli bir işlem yapmaz: yalnızca hesap ya da
model listesini okuyan ücretsiz uçlara gider. MiniMax H3 için yönetmenin kendi sunucusunun /health ucuna bakılır
(sunucu zaten açıktır; sınama GPU'da iş başlatmaz).
"""
from __future__ import annotations

import httpx

from app.config import settings

TIMEOUT = 10.0
ZERO_REQUEST = "00000000-0000-0000-0000-000000000000"


def _request(service: str, key: str, url: str | None = None) -> httpx.Request:
    if service == "vast":                                   # kendi H3 sunucusu: hazır mı (iş başlatmaz)
        return httpx.Request("GET", f"{url.rstrip('/')}/health", headers={"Authorization": f"Bearer {key}"})
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


def _h3_one(key: str, url: str, transport) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=True) as client:
            r = client.send(_request("vast", key, url))
    except httpx.ConnectError:
        return {"ok": False, "status": None, "message": "Sunucuya bağlanılamadı: vast.ai'de makine çalışıyor mu, adres güncel mi?"}
    except httpx.HTTPError as e:
        return {"ok": None, "status": None, "message": f"Sunucuya ulaşılamadı: {type(e).__name__}"}
    if r.status_code in (401, 403):
        return {"ok": False, "status": r.status_code, "message": "Anahtar reddedildi."}
    if r.status_code == 200:
        return {"ok": True, "status": 200, "message": "Sunucu hazır, anahtar kabul edildi."}
    if r.status_code == 503:
        return {"ok": None, "status": 503, "message": "Sunucu açılıyor (model yükleniyor); birkaç dakika sonra yeniden deneyin."}
    if r.status_code == 530:
        return {"ok": False, "status": 530, "message": "Cloudflare tüneli kapalı: makine durmuş ya da adres değişmiş olabilir."}
    return {"ok": None, "status": r.status_code, "message": f"Beklenmeyen yanıt ({r.status_code}); sunucunun günlüğüne bakın."}


def check_h3(key: str, transport: httpx.BaseTransport | None = None) -> dict:
    """MiniMax H3 sunucuları: FL2VA (zorunlu) ve tanımlıysa Ref2VA. İkisinden kötü olanın sonucu döner."""
    if not settings.h3_endpoint_url:
        return {"ok": False, "status": None, "message": "Sunucu adresi tanımlı değil."}
    main = _h3_one(key, settings.h3_endpoint_url, transport)
    if not settings.h3_ref_endpoint_url or main["ok"] is not True:
        return main
    ref = _h3_one(key, settings.h3_ref_endpoint_url, transport)
    if ref["ok"] is True:
        return {**main, "message": "İki sunucu da hazır (FL2VA ve Ref2VA), anahtar kabul edildi."}
    return {**ref, "message": f"Ana sunucu hazır; referans sunucusu (Ref2VA): {ref['message']}"}
