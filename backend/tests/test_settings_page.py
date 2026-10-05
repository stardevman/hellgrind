"""Ayarlar sayfası: ücretli servis bağlantıları, ücretsiz/ücretli mod ve fiyat tablosu.

Bütün testler geçici bir .env ve fiyat dosyası, ayrı Redis anahtarları kullanır: geliştirme ortamının .env'ine ve
panelin değişiklik kaydına dokunulmaz. Hiçbir test ağ çağrısı yapmaz (bağlantı sınaması sahte taşıyıcıyla sınanır) ve
ücretli kod çalıştırılmaz; mod testi yalnızca bayrağın değiştiğini görür, ardından ücretsiz moda döner.
"""
import json
import pathlib
import shutil
import sys

import httpx
import pytest

from app import runtime_config as rc
from app.budget import pricing
from app.config import settings

ENV = """# Ücretsiz aşama
DATABASE_URL=postgresql+psycopg://hg:hg@postgres:5432/hellgrind
DRY_RUN=1
# Ücretli entegrasyonlar
FAL_KEY=
ANTHROPIC_API_KEY=
ELEVENLABS_API_KEY=
"""
SECRET = "fal-key-1234567890abcdWXYZ"


@pytest.fixture
def cfg(tmp_path, monkeypatch, api):
    """Geçici .env; değişen ayarlar test bitince geri alınır; Redis anahtarları teste özeldir."""
    path = tmp_path / ".env"
    path.write_text(ENV)
    for f in rc.FIELDS:
        monkeypatch.setattr(settings, f.attr, getattr(settings, f.attr))
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "config_env_file", str(path))
    monkeypatch.setattr(rc, "VERSION_KEY", "hg:test:config:version")
    monkeypatch.setattr(rc, "AUDIT_KEY", "hg:test:config:audit")
    monkeypatch.setattr(rc, "_seen", None)
    from app import live
    monkeypatch.setattr(live, "missing_packages", lambda service: [])   # CI'da ücretli paketler kurulu değil
    rc._redis().delete(rc.AUDIT_KEY)
    yield path
    rc._redis().delete(rc.AUDIT_KEY, rc.VERSION_KEY)


# ---------- servisler ve anahtarlar ----------

def test_lists_every_paid_service_with_its_connections_and_never_reveals_a_key(api, cfg):
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))          # süreç ayarları dosyadan tazelenir
    body = api.get("/api/settings").json()
    assert [s["id"] for s in body["services"]] == ["fal", "elevenlabs", "anthropic"]
    assert body["dry_run"] is True and body["editable"] is True and body["confirmation"] == rc.PAID_CONFIRMATION
    fal = body["services"][0]
    key = next(f for f in fal["fields"] if f["env"] == "FAL_KEY")
    assert key == {"env": "FAL_KEY", "label": "API anahtarı", "kind": "secret", "set": True, "masked": "••••WXYZ"}
    endpoints = {f["env"]: f for f in fal["fields"] if f["kind"] == "model"}
    assert set(endpoints) == {"FAL_FLUX_ENDPOINT", "FAL_WAN_ENDPOINT", "FAL_SEEDANCE_ENDPOINT", "FAL_LIPSYNC_ENDPOINT", "FAL_WAN_V2V_ENDPOINT"}
    assert endpoints["FAL_WAN_ENDPOINT"]["url"] == "https://fal.ai/models/fal-ai/wan/v2.2-a14b/image-to-video"
    assert fal["console"].startswith("https://") and fal["used_by"]
    assert SECRET not in json.dumps(body)


def test_only_fal_is_needed_with_the_default_engines(api, cfg):
    """Konuşma motoru espeak ve Hermes kural tabanlıyken yalnızca fal.ai anahtarı gerekir."""
    body = api.get("/api/settings").json()
    needed = {s["id"]: s["needed"] for s in body["services"]}
    assert needed == {"fal": True, "elevenlabs": False, "anthropic": False}
    assert "espeak" in body["services"][1]["not_needed_reason"]
    api.patch("/api/settings", json={"values": {"TTS_ENGINE": "elevenlabs", "HERMES_MODE": "llm"}})
    assert all(s["needed"] for s in api.get("/api/settings").json()["services"])
    assert set(body["scorer"]) == {"ready", "message"}


def test_saving_writes_the_env_file_in_place_and_takes_effect_without_a_restart(api, cfg):
    r = api.patch("/api/settings", json={"values": {"FAL_KEY": SECRET, "HERMES_MODEL": "claude-opus-5-5", "TTS_ENGINE": "elevenlabs"}})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["changed"]) == ["FAL_KEY", "HERMES_MODEL", "TTS_ENGINE"]
    text = cfg.read_text()
    assert f"\nFAL_KEY={SECRET}\n" in text and "# Ücretli entegrasyonlar\n" in text and text.startswith("# Ücretsiz aşama\nDATABASE_URL=")
    assert "HERMES_MODEL=claude-opus-5-5" in text and "TTS_ENGINE=elevenlabs" in text      # olmayanlar sona eklenir
    assert settings.fal_key == SECRET and settings.hermes_model == "claude-opus-5-5" and settings.tts_engine == "elevenlabs"
    audit = r.json()["audit"]
    assert {a["field"] for a in audit} == {"FAL_KEY", "HERMES_MODEL", "TTS_ENGINE"}
    assert SECRET not in json.dumps(audit) and SECRET not in r.text
    assert api.patch("/api/settings", json={"values": {"FAL_KEY": SECRET}}).json()["changed"] == []   # değişiklik yok


def test_clearing_a_key_empties_it(api, cfg):
    api.patch("/api/settings", json={"values": {"FAL_KEY": SECRET}})
    r = api.patch("/api/settings", json={"values": {"FAL_KEY": ""}})
    assert "\nFAL_KEY=\n" in cfg.read_text() and settings.fal_key == ""
    assert r.json()["audit"][0]["action"] == "cleared"


@pytest.mark.parametrize("values,needle", [
    ({"DATABASE_URL": "postgresql://x"}, "panelden düzenlenemez"),
    ({"DRY_RUN": "0"}, "panelden düzenlenemez"),                       # mod yalnızca onaylı uçtan değişir
    ({"FAL_KEY": "has space 123456"}, "anahtar"),
    ({"FAL_KEY": "short"}, "anahtar"),
    ({"FAL_WAN_ENDPOINT": "fal-ai/wan\nDRY_RUN=0"}, "model"),           # satır enjeksiyonu
    ({"HERMES_MODE": "magic"}, "rules, llm"),
])
def test_rejects_anything_outside_the_whitelist_or_malformed(api, cfg, values, needle):
    r = api.patch("/api/settings", json={"values": values})
    assert r.status_code == 422 and needle in r.json()["detail"]
    assert cfg.read_text() == ENV


def test_editing_is_off_when_no_env_file_is_mounted(api, cfg, monkeypatch):
    monkeypatch.setattr(settings, "config_env_file", "")
    assert api.get("/api/settings").json()["editable"] is False
    assert api.patch("/api/settings", json={"values": {"HERMES_MODEL": "x-model"}}).status_code == 422


def test_other_processes_pick_up_a_change_and_only_whitelisted_keys(cfg):
    cfg.write_text(ENV.replace("DATABASE_URL=postgresql+psycopg://hg:hg@postgres:5432/hellgrind", "DATABASE_URL=postgresql://evil")
                   + "HERMES_MODEL=claude-haiku-4-5-20251001\n")
    before_db = settings.database_url
    rc._redis().incr(rc.VERSION_KEY)                                     # başka bir süreç kaydetti
    rc.refresh_if_changed()
    assert settings.hermes_model == "claude-haiku-4-5-20251001" and settings.database_url == before_db


# ---------- mod ----------

def test_paid_mode_needs_a_fal_key(api, cfg):
    r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
    assert r.status_code == 422 and "fal.ai anahtarı" in r.json()["detail"] and settings.dry_run is True


def test_paid_mode_needs_the_paid_packages(api, cfg, monkeypatch):
    from app import live
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))
    monkeypatch.setattr(live, "missing_packages", lambda service: ["fal_client"] if service == "fal" else [])
    r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
    assert r.status_code == 422 and "fal_client" in r.json()["detail"] and settings.dry_run is True
    fal = next(s for s in api.get("/api/settings").json()["services"] if s["id"] == "fal")
    assert fal["missing_packages"] == ["fal_client"]


def test_missing_paid_package_is_a_clear_blocked_call_not_an_import_error(monkeypatch):
    import importlib.util
    from app import live
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "fal_client" else real(name, *a))
    monkeypatch.delitem(sys.modules, "fal_client", raising=False)   # test_live_fal gerçek paketi önceden yüklemiş olabilir
    with pytest.raises(live.PaidCallBlocked, match="requirements-live.txt"):
        live.require_package("fal_client")
    assert live.missing_packages("fal") == ["fal_client"] and live.missing_packages("elevenlabs") == []


def test_paid_mode_needs_the_exact_confirmation(api, cfg):
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))
    for confirm in (None, "evet", "ücretli mod"):
        r = api.post("/api/settings/mode", json={"paid": True, "confirm": confirm})
        assert r.status_code == 422 and rc.PAID_CONFIRMATION in r.json()["detail"]
    assert settings.dry_run is True and "DRY_RUN=1" in cfg.read_text()


def test_paid_mode_is_refused_while_production_runs(api, cfg, monkeypatch):
    from app.api import services
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))
    monkeypatch.setattr(services, "_busy", lambda s: ["Film S001_SH01"])
    r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
    assert r.status_code == 409 and "S001_SH01" in r.json()["detail"] and settings.dry_run is True


def test_switching_to_paid_and_back_is_written_and_logged(api, cfg, monkeypatch):
    from app.api import services
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))
    monkeypatch.setattr(services, "_busy", lambda s: [])
    try:
        r = api.post("/api/settings/mode", json={"paid": True, "confirm": "Ücretli Moda Geç"})   # büyük/küçük harf duyarsız
        assert r.status_code == 200 and r.json()["changed"] is True
        assert "DRY_RUN=0" in cfg.read_text() and settings.dry_run is False
    finally:
        back = api.post("/api/settings/mode", json={"paid": False})       # dönüş onaysız
    assert back.json()["dry_run"] is True and "DRY_RUN=1" in cfg.read_text() and settings.dry_run is True
    assert [a["detail"] for a in back.json()["audit"][:2]] == ["ücretsiz moda dönüldü", "ücretli moda geçildi"]


# ---------- fiyat tablosu ----------

@pytest.fixture
def prices(tmp_path, monkeypatch):
    path = tmp_path / "pricing.yaml"
    shutil.copy(pathlib.Path(__file__).resolve().parents[1] / "pricing.yaml", path)
    monkeypatch.setattr(settings, "pricing_file", str(path))
    pricing.table.cache_clear()
    yield path
    pricing.table.cache_clear()


def test_pricing_is_read_and_saved_with_comments_and_used_at_once(api, cfg, prices):
    body = api.get("/api/settings/pricing").json()
    assert body["table"]["video_per_second"]["fal_wan"]["720p"] == 0.08 and "# Birim fiyatlar" in body["text"]
    new = body["text"].replace('"720p": 0.08', '"720p": 0.1', 1)
    r = api.put("/api/settings/pricing", json={"text": new})
    assert r.status_code == 200 and prices.read_text() == new
    assert pricing.estimate_video("fal_wan", "720p", 5) == 0.5                       # önbellek boşaltıldı


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t.replace('"720p": 0.242', '"720p": -1'), "sıfırdan büyük"),
    (lambda t: t.replace("  seedance:", "  seedanc:"), "video_per_second.seedance eksik"),
    (lambda t: t + "\n: : :\n", "YAML okunamadı"),
    (lambda t: t.replace("lipsync_per_minute: 5.0", "lipsync_per_minute: bedava"), "lipsync_per_minute"),
])
def test_invalid_pricing_is_refused_and_the_file_is_kept(api, cfg, prices, mutate, needle):
    before = prices.read_text()
    r = api.put("/api/settings/pricing", json={"text": mutate(before)})
    assert r.status_code == 422 and needle in r.json()["detail"] and prices.read_text() == before


def test_the_shipped_pricing_table_is_valid():
    pricing.validate_table((pathlib.Path(__file__).resolve().parents[1] / "pricing.yaml").read_text())


# ---------- bağlantı sınaması (ağ yok) ----------

def transport(status, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(status)
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("service,status,ok", [
    ("anthropic", 200, True), ("anthropic", 401, False), ("elevenlabs", 200, True), ("elevenlabs", 403, False),
    ("fal", 404, True), ("fal", 401, False), ("fal", 500, None), ("anthropic", 429, None),
])
def test_connection_check_reads_the_providers_answer(service, status, ok):
    from app.live import probe
    seen = []
    r = probe.check(service, SECRET, transport=transport(status, seen))
    assert r["ok"] is ok and r["status"] == status and len(seen) == 1
    assert seen[0].method == "GET"                                       # yalnızca okuyan, ücretsiz uçlar
    header = {"anthropic": "x-api-key", "elevenlabs": "xi-api-key", "fal": "authorization"}[service]
    assert SECRET in seen[0].headers[header]


def test_connection_check_without_a_key_makes_no_request(api, cfg):
    from app.live import probe
    seen = []
    assert probe.check("fal", "", transport=transport(200, seen)) == {"ok": False, "status": None, "message": "Anahtar tanımlı değil."}
    assert seen == []
    settings.fal_key = ""
    assert api.post("/api/settings/test/fal").json()["ok"] is False
    assert api.post("/api/settings/test/nope").status_code == 404
