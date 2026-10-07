"""İzinli bölge listesi (her arka uç): yalnızca açıkça onaylanmış veri merkezleri ve ülkeler; yasaklı topraklar ve konumu
bilinmeyenler hiçbir zaman. Ağ yok."""
import pytest

from app.config import settings
from app.core import regions


@pytest.mark.parametrize("dc,ok", [("CA-MTL-1", True), ("CA-MTL-2", True), ("ca-mtl-3", True), ("EUR-NO-1", False),
                                   ("EUR-IS-1", False), ("US-TX-3", False), ("EU-RO-1", False), ("", False), (None, False)])
def test_runpod_defaults_to_canada_only(dc, ok):
    assert regions.datacenter_allowed(dc) is ok


def test_nordic_datacenters_open_only_on_request(monkeypatch):
    monkeypatch.setattr(settings, "h3_allow_nordic", True)
    assert regions.datacenter_allowed("EUR-NO-1") and regions.datacenter_allowed("EUR-IS-3")
    assert regions.datacenter_allowed("EUR-IS-4")                      # yeni İzlanda merkezleri de aynı ayara bağlı
    assert not regions.datacenter_allowed("EU-SE-1") and not regions.datacenter_allowed("EUR-NO-2")
    assert regions.runpod_datacenters()[-1] == "EUR-IS-3"


@pytest.mark.parametrize("geo", ["Puerto Rico, PR", "San Juan, Puerto Rico, US", "Puerto Rico, US", "Guam, GU",
                                 "St. Thomas, VI", "Pago Pago, AS", "Saipan, MP", "Wake Island, UM",
                                 "Canary Islands, IC", "Tenerife, Canarias, ES", "Cayenne, GF", "Saint-Denis, RE",
                                 "Guadeloupe, GP", "Martinique, MQ", "Mayotte, YT", "Saint-Martin, MF",
                                 "Ponta Delgada, Azores, PT", "Funchal, Madeira, PT", "Texas, US", "Bavaria, DE",
                                 "Seoul, KR", "London, GB", None, "", "Somewhere"])
def test_forbidden_and_unknown_locations_are_never_accepted(geo, monkeypatch):
    monkeypatch.setattr(settings, "h3_allow_nordic", True)
    assert regions.location_allowed(geo) is False


def test_only_approved_countries_are_accepted(monkeypatch):
    assert regions.location_allowed("Quebec, CA") and not regions.location_allowed("Oslo, NO")
    assert not regions.location_allowed("Istanbul, TR")                 # izinli listede değil (yasaklı da değil)
    monkeypatch.setattr(settings, "h3_allow_nordic", True)
    assert regions.location_allowed("Oslo, NO") and regions.location_allowed("Reykjavik, IS")
    # Kanada'da bile yasaklı toprak adı geçiyorsa reddedilir (yanlış etiketli teklif)
    assert not regions.location_allowed("Puerto Rico, CA")
