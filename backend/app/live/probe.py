"""Bağlantı sınaması: bir API anahtarının sağlayıcı tarafından kabul edilip edilmediğini, üretim yapmadan ve ücret
doğurmadan denetler. Yalnızca panelde "Bağlantıyı sına" düğmesine basılınca çalışır; testler bu modülün ağ çağrısını
çalıştırmaz (httpx taşıyıcısı sahtelenir). DRY_RUN'dan bağımsızdır çünkü ücretli bir işlem yapmaz: yalnızca hesap ya da
model listesini okuyan ücretsiz uçlara gider.
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
