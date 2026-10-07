"""MiniMax H3 için izinli bölgeler (her arka uç: RunPod, vast.ai). Ağ kodu yok; panel de kullanır.

Yasaklı liste yerine **izinli liste**: yalnızca burada açıkça onaylanmış veri merkezleri ve ülkeler kabul edilir; konumu
bilinmeyen makine ya da teklif hiçbir zaman seçilmez. Lisans (MiniMax H3 Community License §V.4) AB, Birleşik Krallık,
Güney Kore ve ABD'yi kapsamaz; bunların denizaşırı toprakları da (ABD: Porto Riko, Guam, ABD Virjin Adaları, Amerikan
Samoası, Kuzey Mariana Adaları, ABD Küçük Dış Adaları; AB en dış bölgeleri: Kanarya Adaları, Fransız Guyanası, Réunion,
Guadeloupe, Martinik, Mayotte, Saint-Martin, Azorlar, Madeira) yasaklıdır ve izinli listede olsalar bile reddedilir.

- RunPod: varsayılan CA-MTL-1, CA-MTL-2, CA-MTL-3 (Kanada, Montreal). EUR-NO-1 (Norveç) ve EUR-IS-* (İzlanda) yalnızca
  Ayarlar › Kuzey veri merkezleri açıkken (varsayılan kapalı).
- vast.ai (deneysel): varsayılan yalnızca Kanada; Kuzey açıkken Norveç ve İzlanda.
"""
import re

from app.config import settings

RUNPOD_DEFAULT = ("CA-MTL-1", "CA-MTL-2", "CA-MTL-3")
RUNPOD_NORDIC = ("EUR-NO-1", "EUR-IS-1", "EUR-IS-2", "EUR-IS-3")
DATACENTER_COUNTRY = {"CA-MTL-1": "CA", "CA-MTL-2": "CA", "CA-MTL-3": "CA", "EUR-NO-1": "NO",
                      "EUR-IS-1": "IS", "EUR-IS-2": "IS", "EUR-IS-3": "IS"}
VAST_DEFAULT = ("CA",)
VAST_NORDIC = ("NO", "IS")

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
      "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
LICENSE_EXCLUDED = EU | {"GB", "UK", "KR", "US"}
US_TERRITORIES = {"PR", "GU", "VI", "AS", "MP", "UM"}
EU_OUTERMOST = {"IC", "GF", "RE", "GP", "MQ", "YT", "MF"}           # Azorlar ve Madeira'nın ülke kodu yok: adla
FORBIDDEN = LICENSE_EXCLUDED | US_TERRITORIES | EU_OUTERMOST
FORBIDDEN_NAMES = re.compile(
    r"puerto\s*rico|guam|virgin\s+islands|american\s+samoa|mariana|minor\s+outlying|canar(y|ias)|azores|a[cç]ores|"
    r"madeira|french\s+guiana|guyane|r[eé]union|guadeloupe|martinique|mayotte|saint[\s-]martin", re.I)

COUNTRY_TR = {"CA": "Kanada", "NO": "Norveç", "IS": "İzlanda"}


def runpod_datacenters() -> tuple[str, ...]:
    return RUNPOD_DEFAULT + (RUNPOD_NORDIC if settings.h3_allow_nordic else ())


def datacenter_allowed(dc: str | None) -> bool:
    """RunPod veri merkezi izinli listede mi. EUR-IS-* önekiyle yeni İzlanda merkezleri de Kuzey ayarına bağlıdır."""
    if not dc:
        return False
    dc = dc.strip().upper()
    if dc in RUNPOD_DEFAULT:
        return True
    return bool(settings.h3_allow_nordic) and (dc == "EUR-NO-1" or re.fullmatch(r"EUR-IS-\d+", dc) is not None)


def vast_countries() -> tuple[str, ...]:
    return VAST_DEFAULT + (VAST_NORDIC if settings.h3_allow_nordic else ())


def country_of(geolocation: str | None) -> str | None:
    """"Quebec, CA" -> "CA". Kod yoksa None."""
    m = re.search(r"\b([A-Z]{2})\s*$", geolocation or "")
    return m.group(1) if m else None


def location_allowed(geolocation: str | None) -> bool:
    """vast.ai konumu ("Bölge, ÜLKE") izinli mi: ülke izinli listede olmalı; yasaklı ülke, toprak ya da bölge adı (ör.
    "Puerto Rico, US", "Canarias, ES") ve konumu bilinmeyen her zaman reddedilir."""
    country = country_of(geolocation)
    if country is None or FORBIDDEN_NAMES.search(geolocation or ""):
        return False
    if country in FORBIDDEN:
        return False
    return country in vast_countries()


def allowed_text() -> str:
    """Panel ve hata metinleri için izinli liste."""
    countries = ", ".join(COUNTRY_TR.get(c, c) for c in vast_countries())
    return f"RunPod: {', '.join(runpod_datacenters())}; vast.ai: {countries}"
