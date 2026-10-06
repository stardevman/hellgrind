"""vast.ai hesabı: H3 makinesinin durumu, başlatılması, durdurulması; yedek makine için teklif arama, kiralama ve silme
(docs.vast.ai › API reference).

  GET    /api/v0/instances/{id}  {"instances": {actual_status, intended_status, dph_total, geolocation, gpu_name, ports...}}
  PUT    /api/v0/instances/{id}  {"state": "running" | "stopped"} -> {"success": true}
  DELETE /api/v0/instances/{id}  makineyi ve diskini siler (disk ücreti durur)
  POST   /api/v0/bundles         teklif arama: {gpu_name: {"in": [...]}, geolocation: {"notin": [...]}, dph_total: {"lte"}...}
  PUT    /api/v0/asks/{offer}    teklifi kirala: {image, disk, runtype, env, onstart, label} -> {"new_contract": id}

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
    ip: str | None = None
    ports: dict | None = None   # {"8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40123"}], ...}

    def host_port(self, container_port: int) -> int | None:
        """Kapsayıcı portunun dışarıdaki karşılığı (portalın kimlik doğrulamalı portu için)."""
        for item in (self.ports or {}).get(f"{container_port}/tcp") or []:
            if str(item.get("HostPort", "")).isdigit():
                return int(item["HostPort"])
        return None

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
    ports = i.get("ports") if isinstance(i.get("ports"), dict) else None
    return Box(status=str(i.get("actual_status") or i.get("cur_state") or "unknown"), intended=i.get("intended_status"),
               dph=float(dph) if isinstance(dph, (int, float)) else None, geolocation=i.get("geolocation"),
               gpu=i.get("gpu_name"), ip=i.get("public_ipaddr"), ports=ports)


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


def instance(instance_id: str | None = None) -> Box:
    """Ücretli akış içinden durum okuma (kilitten geçer). Kimlik verilmezse Ayarlar'daki makine."""
    key = guard("vast_api_key")
    instance_id = instance_id or settings.vast_instance_id
    if not instance_id:
        raise PaidCallBlocked("vast.ai makine kimliği tanımlı değil: Ayarlar › MiniMax H3'ten girin.")
    return read(key, instance_id)


def set_state(state: str, instance_id: str | None = None) -> None:
    """Makineyi başlatır ("running") ya da durdurur ("stopped"). Durdurulmuş makinenin diski vast.ai'de ücretlenmeye
    devam eder; başlatma, makinenin GPU'su başkasına kiralanmışsa başarısız olabilir. Kimlik verilmezse Ayarlar'daki makine."""
    if state not in ("running", "stopped"):
        raise ValueError(state)
    key = guard("vast_api_key")
    instance_id = instance_id or settings.vast_instance_id
    if not instance_id:
        raise PaidCallBlocked("vast.ai makine kimliği tanımlı değil: Ayarlar › MiniMax H3'ten girin.")
    with _client(key) as c:
        r = c.put(f"/instances/{instance_id}", json={"state": state})
    if r.status_code in (401, 403):
        raise PaidCallBlocked("vast.ai API anahtarı reddedildi: Ayarlar › MiniMax H3'ten anahtarı kontrol edin.")
    r.raise_for_status()
    body = r.json() if r.content else {}
    if body.get("success") is False:
        raise RuntimeError(f"vast.ai makineyi {state} yapamadı: {body.get('msg') or body.get('error') or body}")


# ---------- yedek makine: teklif, kiralama, silme ----------

@dataclass(frozen=True)
class Offer:
    id: int
    gpu: str
    dph: float
    geolocation: str | None
    ram_gb: float
    disk_gb: float
    reliability: float | None
    inet_mbps: float | None

    @property
    def country(self) -> str | None:
        return country_of(self.geolocation)


def gpu_names(name: str) -> list[str]:
    """Arama için GPU adı: hem "RTX 5090" hem "RTX_5090" (CLI biçimi) gönderilir."""
    name = (name or "").strip()
    return sorted({name, name.replace(" ", "_"), name.replace("_", " ")} - {""})


def offer_query(gpu: str, max_dph: float, min_ram_gb: int, min_disk_gb: int, min_inet_mbps: int, limit: int = 10) -> dict:
    return {"limit": limit, "type": "ondemand", "order": [["dph_total", "asc"]],
            "rentable": {"eq": True}, "rented": {"eq": False}, "verified": {"eq": True}, "num_gpus": {"eq": 1},
            "gpu_name": {"in": gpu_names(gpu)}, "dph_total": {"lte": max_dph}, "cpu_ram": {"gte": min_ram_gb * 1000},
            "disk_space": {"gte": min_disk_gb}, "inet_down": {"gte": min_inet_mbps}, "reliability": {"gte": 0.98},
            "direct_port_count": {"gte": 4}, "geolocation": {"notin": sorted(EXCLUDED)}}


def search_offers(gpu: str, max_dph: float, min_ram_gb: int, min_disk_gb: int, min_inet_mbps: int) -> list[Offer]:
    """Uygun teklifler, ucuzdan pahalıya. Bölge süzgeci sunucuda ve burada iki kez uygulanır: konumu bilinmeyen ya da
    lisansın dışladığı bölgedeki teklif hiçbir zaman dönmez."""
    key = guard("vast_api_key")
    with _client(key) as c:
        r = c.post("/bundles", json=offer_query(gpu, max_dph, min_ram_gb, min_disk_gb, min_inet_mbps))
    if r.status_code in (401, 403):
        raise PaidCallBlocked("vast.ai API anahtarı reddedildi: Ayarlar › MiniMax H3'ten anahtarı kontrol edin.")
    r.raise_for_status()
    offers = []
    for o in r.json().get("offers") or []:
        offer = Offer(id=int(o.get("ask_contract_id") or o["id"]), gpu=str(o.get("gpu_name") or ""),
                      dph=float(o.get("dph_total") or 0), geolocation=o.get("geolocation"),
                      ram_gb=round(float(o.get("cpu_ram") or 0) / 1000, 1), disk_gb=float(o.get("disk_space") or 0),
                      reliability=o.get("reliability"), inet_mbps=o.get("inet_down"))
        if offer.country and offer.country not in EXCLUDED and offer.dph <= max_dph and offer.ram_gb >= min_ram_gb \
                and offer.disk_gb >= min_disk_gb and (offer.inet_mbps is None or offer.inet_mbps >= min_inet_mbps):
            offers.append(offer)
    return sorted(offers, key=lambda o: o.dph)


def rent(offer_id: int, body: dict) -> str:
    """Teklifi kiralar; yeni makinenin kimliği."""
    key = guard("vast_api_key")
    with _client(key) as c:
        r = c.put(f"/asks/{offer_id}", json=body)
    if r.status_code in (401, 403):
        raise PaidCallBlocked("vast.ai API anahtarı reddedildi: Ayarlar › MiniMax H3'ten anahtarı kontrol edin.")
    r.raise_for_status()
    data = r.json()
    if not data.get("success", True) or not data.get("new_contract"):
        raise RuntimeError(f"vast.ai teklifi kiralayamadı ({offer_id}): {data.get('msg') or data.get('error') or data}")
    return str(data["new_contract"])


def destroy(instance_id: str) -> None:
    """Makineyi diskiyle birlikte siler (geri alınamaz). Yalnızca yerine kurulan makine hazır olduktan sonra çağrılır."""
    key = guard("vast_api_key")
    with _client(key) as c:
        r = c.delete(f"/instances/{instance_id}")
    if r.status_code == 404:
        return
    r.raise_for_status()
