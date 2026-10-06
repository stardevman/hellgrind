"""vast.ai hesabı: H3 makinesinin durumu, başlatılması ve durdurulması (docs.vast.ai › API reference › instances).

  GET /api/v0/instances/{id}   {"instances": {actual_status, intended_status, dph_total, geolocation, gpu_name, ...}}
  PUT /api/v0/instances/{id}   {"state": "running" | "stopped"} -> {"success": true}

Başlatma ve durdurma ücretli kilitten geçer (`guard("vast_api_key")`): yalnızca ücretli modda çalışır. Durum okuma
bağlantı sınamasında da kullanılır (ücretsiz, salt okunur; app/live/probe.py). Testler sahte taşıyıcı bağlar.
"""
import re
from dataclasses import dataclass

import httpx

from app.config import settings
from app.live import PaidCallBlocked, guard

API = "https://console.vast.ai/api/v0"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TRANSPORT: httpx.BaseTransport | None = None

# Lisansın dışladığı bölgeler (MiniMax H3 Community License, "Excluded Territories"): AB, Birleşik Krallık, Güney Kore, ABD.
EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
      "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
EXCLUDED = EU | {"GB", "UK", "KR", "US"}


@dataclass(frozen=True)
class Box:
    status: str                 # running | stopped | exited | loading | offline | unknown
    intended: str | None
    dph: float | None           # vast.ai'nin bildirdiği saatlik toplam ücret
    geolocation: str | None     # ör. "Norway, NO"
    gpu: str | None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def country(self) -> str | None:
        return country_of(self.geolocation)

    @property
    def excluded(self) -> bool:
        return self.country in EXCLUDED


def country_of(geolocation: str | None) -> str | None:
    """"California, US" -> "US". Kod yoksa None."""
    m = re.search(r"\b([A-Z]{2})\s*$", geolocation or "")
    return m.group(1) if m else None


def _client(key: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                        timeout=TIMEOUT, transport=transport or TRANSPORT, follow_redirects=True)


def _box(data: dict) -> Box:
    i = data.get("instances") or data.get("instance") or {}
    if isinstance(i, list):
        i = i[0] if i else {}
    dph = i.get("dph_total")
    return Box(status=str(i.get("actual_status") or i.get("cur_state") or "unknown"), intended=i.get("intended_status"),
               dph=float(dph) if isinstance(dph, (int, float)) else None, geolocation=i.get("geolocation"),
               gpu=i.get("gpu_name"))


def read(key: str, instance_id: str, transport: httpx.BaseTransport | None = None) -> Box:
    """Makinenin durumu (ücretsiz, salt okunur). 401/403: anahtar reddedildi."""
    with _client(key, transport) as c:
        r = c.get(f"/instances/{instance_id}")
    if r.status_code in (401, 403):
        raise PaidCallBlocked("vast.ai API anahtarı reddedildi: Ayarlar › MiniMax H3'ten anahtarı kontrol edin.")
    if r.status_code == 404:
        raise PaidCallBlocked(f"vast.ai'de {instance_id} kimlikli makine bulunamadı: makine kimliğini kontrol edin.")
    r.raise_for_status()
    return _box(r.json())


def instance() -> Box:
    """Ücretli akış içinden durum okuma (kilitten geçer)."""
    key = guard("vast_api_key")
    if not settings.vast_instance_id:
        raise PaidCallBlocked("vast.ai makine kimliği tanımlı değil: Ayarlar › MiniMax H3'ten girin.")
    return read(key, settings.vast_instance_id)


def set_state(state: str) -> None:
    """Makineyi başlatır ("running") ya da durdurur ("stopped"). Durdurulmuş makinenin diski vast.ai'de ücretlenmeye
    devam eder; başlatma, makinenin GPU'su başkasına kiralanmışsa başarısız olabilir."""
    if state not in ("running", "stopped"):
        raise ValueError(state)
    key = guard("vast_api_key")
    if not settings.vast_instance_id:
        raise PaidCallBlocked("vast.ai makine kimliği tanımlı değil: Ayarlar › MiniMax H3'ten girin.")
    with _client(key) as c:
        r = c.put(f"/instances/{settings.vast_instance_id}", json={"state": state})
    if r.status_code in (401, 403):
        raise PaidCallBlocked("vast.ai API anahtarı reddedildi: Ayarlar › MiniMax H3'ten anahtarı kontrol edin.")
    r.raise_for_status()
    body = r.json() if r.content else {}
    if body.get("success") is False:
        raise RuntimeError(f"vast.ai makineyi {state} yapamadı: {body.get('msg') or body.get('error') or body}")
