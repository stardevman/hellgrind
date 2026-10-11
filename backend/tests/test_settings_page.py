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
RUNPOD_API_KEY=
RUNPOD_ENDPOINT_ID=
VAST_API_KEY=
VAST_INSTANCE_ID=
H3_API_KEY=
H3_ENDPOINT_URL=
FAL_KEY=
ANTHROPIC_API_KEY=
ELEVENLABS_API_KEY=
"""
SECRET = "fal-key-1234567890abcdWXYZ"
VAST_SECRET = "vast-api-key-0987654321zyxQRST"
H3_SECRET = "portal-token-1234567890abcPQRS"
RUNPOD_SECRET = "runpod-api-key-1122334455aabbRPKY"
# Ücretli moda geçmek için gereken tek servis: MiniMax H3'ün platformu. Varsayılan RunPod (anahtar + uç nokta).
RP_ENV = (ENV.replace("RUNPOD_API_KEY=", f"RUNPOD_API_KEY={RUNPOD_SECRET}")
          .replace("RUNPOD_ENDPOINT_ID=", "RUNPOD_ENDPOINT_ID=ep123"))
# vast.ai (deneysel): anahtar, makine kimliği, sunucu adresi ve Platform=vast
H3_ENV = (ENV.replace("VAST_API_KEY=", f"VAST_API_KEY={VAST_SECRET}").replace("VAST_INSTANCE_ID=", "VAST_INSTANCE_ID=4242")
          .replace("H3_API_KEY=", f"H3_API_KEY={H3_SECRET}").replace("H3_ENDPOINT_URL=", "H3_ENDPOINT_URL=https://h3.test")
          + "H3_PLATFORM=vast\n")


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
    assert [s["id"] for s in body["services"]] == ["runpod", "vast", "fal", "elevenlabs", "anthropic"]
    assert body["dry_run"] is True and body["editable"] is True and body["confirmation"] == rc.PAID_CONFIRMATION
    fal = body["services"][2]
    key = next(f for f in fal["fields"] if f["env"] == "FAL_KEY")
    assert key == {"env": "FAL_KEY", "label": "API anahtarı", "kind": "secret", "set": True, "masked": "••••WXYZ"}
    endpoints = {f["env"]: f for f in fal["fields"] if f["kind"] == "model"}
    assert set(endpoints) == {"FAL_FLUX_ENDPOINT", "FAL_WAN_ENDPOINT", "FAL_SEEDANCE_ENDPOINT", "FAL_LIPSYNC_ENDPOINT", "FAL_WAN_V2V_ENDPOINT",
                              "FAL_KONTEXT_ENDPOINT", "FAL_H3MAX_I2V_ENDPOINT", "FAL_H3MAX_REF_ENDPOINT",
                              "FAL_SEEDANCE25_I2V_ENDPOINT", "FAL_SEEDANCE25_REF_ENDPOINT",
                              "FAL_SEEDANCE25_DRAFT_COMPLETE_ENDPOINT", "FAL_QWEN_IMAGE_ENDPOINT", "FAL_QWEN_EDIT_ENDPOINT",
                              "FAL_QWEN_EDIT_LORA_ENDPOINT"}
    assert endpoints["FAL_H3MAX_I2V_ENDPOINT"]["default"] == "minimax/h3-max/image-to-video"
    assert endpoints["FAL_SEEDANCE25_REF_ENDPOINT"]["default"] == "bytedance/seedance-2.5/reference-to-video"
    assert not any("/us/" in f["default"] for f in endpoints.values())
    assert endpoints["FAL_WAN_ENDPOINT"]["url"] == "https://fal.ai/models/fal-ai/wan/v2.2-a14b/image-to-video"
    assert fal["console"].startswith("https://") and fal["used_by"]
    assert SECRET not in json.dumps(body)


def test_runpod_is_listed_first_as_the_default_h3_platform(api, cfg):
    cfg.write_text(RP_ENV)
    services = api.get("/api/settings").json()["services"]
    rp, vast = services[0], services[1]
    assert rp["name"] == "MiniMax H3 · RunPod" and rp["key_env"] == "RUNPOD_API_KEY" and rp["active"] is True
    fields = {f["env"]: f for f in rp["fields"]}
    assert fields["H3_PLATFORM"]["value"] == "runpod" and fields["H3_PLATFORM"]["choices"] == ["kapali", "runpod", "vast"]
    assert fields["H3_PLATFORM"]["default"] == "kapali"                 # RunPod uç noktası silindi
    # geliştirme .env'i konteynere yüklenir: dosyada olmayan alanlar için varsayılanlar sınanır
    assert fields["RUNPOD_ENDPOINT_ID"]["value"] == "ep123" and fields["RUNPOD_DATACENTER"]["default"] == "CA-MTL-1"
    assert (fields["RUNPOD_IDLE_TIMEOUT_S"]["default"], fields["RUNPOD_MIN_BALANCE_USD"]["default"]) == ("60", "10.0")
    assert fields["H3_ALLOW_NORDIC"]["default"] == "false" and fields["H3_STEPS"]["default"] == "9"
    assert vast["name"] == "MiniMax H3 · vast.ai (deneysel)" and vast["needed"] is False and "deneysel" in vast["not_needed_reason"]
    assert RUNPOD_SECRET not in api.get("/api/settings").text


def test_the_vast_server_keeps_its_address_and_machine_settings(api, cfg):
    cfg.write_text(H3_ENV)
    vast = api.get("/api/settings").json()["services"][1]
    assert vast["name"] == "MiniMax H3 · vast.ai (deneysel)" and vast["console"] == "https://cloud.vast.ai/instances/"
    fields = {f["env"]: f for f in vast["fields"]}
    assert vast["key_env"] == "VAST_API_KEY"
    assert fields["VAST_API_KEY"] == {"env": "VAST_API_KEY", "label": "vast.ai API anahtarı", "kind": "secret", "set": True,
                                      "masked": "••••QRST"}
    assert fields["H3_API_KEY"]["kind"] == "secret" and fields["H3_API_KEY"]["masked"] == "••••PQRS"
    assert fields["VAST_INSTANCE_ID"]["value"] == "4242" and fields["H3_BACKEND"]["value"] == "comfyui"   # SGLang deneysel
    assert fields["H3_BACKEND"]["choices"] == ["comfyui", "sglang"] and "deneysel" in fields["H3_BACKEND"]["label"]
    # değerler geliştirme .env'inden gelebilir: varsayılanlar sınanır
    assert (fields["H3_REPLACE_MODE"]["default"], fields["H3_MAX_DPH"]["default"], fields["H3_MIN_RAM_GB"]["default"]) == ("ask", "0.95", "128")
    assert fields["H3_MIN_RAM_GB"]["minimum"] == 96 and fields["H3_TLS_MODE"]["choices"] == ["verify", "pin", "off"]
    assert fields["H3_IMAGE_MODEL"]["choices"] == ["qwen-image-2512", "flux-schnell", "none"]
    assert fields["H3_ENDPOINT_URL"]["kind"] == "url" and fields["H3_ENDPOINT_URL"]["value"] == "https://h3.test"
    assert fields["H3_REF_ENDPOINT_URL"]["value"] == ""
    assert (fields["H3_IDLE_STOP_MINUTES"]["value"], fields["H3_AUTO_START"]["value"]) == ("10", "false")
    text = api.get("/api/settings").text
    assert vast["active"] is True and H3_SECRET not in text and VAST_SECRET not in text


def test_replacement_settings_are_typed(api, cfg):
    r = api.patch("/api/settings", json={"values": {"H3_MAX_DPH": "0.8", "H3_MIN_RAM_GB": "96", "H3_REPLACE_MODE": "auto",
                                                    "H3_TLS_MODE": "pin", "H3_GPU_NAME": "RTX 5090"}})
    assert r.status_code == 200, r.text
    assert (settings.h3_max_dph, settings.h3_min_ram_gb, settings.h3_replace_mode) == (0.8, 96, "auto")
    assert settings.h3_tls_mode == "pin" and settings.h3_gpu_name == "RTX 5090"


@pytest.mark.parametrize("env,value", [("H3_MIN_RAM_GB", "64"), ("H3_MIN_DISK_GB", "120"), ("H3_MIN_INET_MBPS", "50")])
def test_machine_minimums_have_floors(api, cfg, env, value):
    r = api.patch("/api/settings", json={"values": {env: value}})
    assert r.status_code == 422 and "en az" in r.json()["detail"]


def test_new_machine_defaults_fit_h3_and_the_image_model():
    from app.config import Settings
    defaults = {k: Settings.model_fields[k].default for k in ("h3_min_ram_gb", "h3_min_disk_gb", "h3_min_inet_mbps")}
    assert defaults == {"h3_min_ram_gb": 128, "h3_min_disk_gb": 200, "h3_min_inet_mbps": 1000}
    floors = {f.env: f.minimum for f in rc.FIELDS if f.minimum is not None}
    assert floors["H3_MIN_RAM_GB"] == 96


def test_on_off_and_number_settings_are_typed_when_applied(api, cfg):
    r = api.patch("/api/settings", json={"values": {"H3_AUTO_START": "true", "H3_STEPS": "8", "H3_IDLE_STOP_MINUTES": "0",
                                                    "H3_MODEL_REVISION": "fl2va int8 convrot + turbo v4"}})
    assert r.status_code == 200, r.text
    assert settings.h3_auto_start is True and settings.h3_steps == 8 and settings.h3_idle_stop_minutes == 0
    assert settings.h3_model_revision == "fl2va int8 convrot + turbo v4"
    api.patch("/api/settings", json={"values": {"H3_AUTO_START": "false"}})
    assert settings.h3_auto_start is False
    fields = {f["env"]: f for s_ in api.get("/api/settings").json()["services"] for f in s_["fields"]}
    assert (fields["H3_AUTO_START"]["value"], fields["H3_STEPS"]["value"]) == ("false", "8")


def test_only_the_h3_server_is_needed_with_the_default_engines(api, cfg):
    """Konuşma motoru espeak ve Hermes kural tabanlıyken yalnızca MiniMax H3 platformu (RunPod) gerekir; fal.ai isteğe
    bağlıdır, vast.ai deneysel ve kapalıdır."""
    body = api.get("/api/settings").json()
    needed = {s["id"]: s["needed"] for s in body["services"]}
    assert needed == {"runpod": True, "vast": False, "fal": False, "elevenlabs": False, "anthropic": False}
    assert "isteğe bağlı" in body["services"][2]["not_needed_reason"] and "espeak" in body["services"][3]["not_needed_reason"]
    api.patch("/api/settings", json={"values": {"FAL_KEY": SECRET, "TTS_ENGINE": "elevenlabs", "HERMES_MODE": "llm"}})
    assert all(s["needed"] for s in api.get("/api/settings").json()["services"] if s["id"] != "vast")
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
    ({"H3_ENDPOINT_URL": "ftp://1.2.3.4"}, "sunucu adresi"),
    ({"H3_ENDPOINT_URL": "https://h3.test\nDRY_RUN=0"}, "sunucu adresi"),         # satır enjeksiyonu
    ({"H3_STEPS": "7"}, "50, 20, 9, 8, 5"),
    ({"H3_BACKEND": "vllm"}, "comfyui, sglang"),
    ({"H3_MAX_DPH": "ucuz"}, "sıfırdan büyük bir sayı"),
    ({"H3_MIN_DISK_GB": "0"}, "sıfırdan büyük bir sayı"),
    ({"H3_REPLACE_MODE": "always"}, "ask, auto, off"),
    ({"H3_MODEL_REVISION": "kötü\nDRY_RUN=0"}, "satır sonu"),
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

def test_a_server_address_is_saved_without_a_trailing_slash_and_can_be_cleared(api, cfg):
    r = api.patch("/api/settings", json={"values": {"H3_ENDPOINT_URL": "http://203.0.113.7:40123/"}})
    assert r.status_code == 200 and settings.h3_endpoint_url == "http://203.0.113.7:40123"
    assert "\nH3_ENDPOINT_URL=http://203.0.113.7:40123\n" in cfg.read_text()
    api.patch("/api/settings", json={"values": {"H3_ENDPOINT_URL": ""}})
    assert settings.h3_endpoint_url == ""


def test_paid_mode_needs_the_runpod_endpoint_not_a_fal_key(api, cfg, monkeypatch):
    from app.api import services
    cfg.write_text(ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))       # fal anahtarı tek başına yetmez
    r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
    assert r.status_code == 422 and "RunPod tanımlı değil" in r.json()["detail"] and settings.dry_run is True
    cfg.write_text(RP_ENV.replace("RUNPOD_ENDPOINT_ID=ep123", "RUNPOD_ENDPOINT_ID="))   # anahtar tek başına da yetmez
    rc._redis().incr(rc.VERSION_KEY)
    assert api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION}).status_code == 422
    cfg.write_text(RP_ENV)
    rc._redis().incr(rc.VERSION_KEY)
    monkeypatch.setattr(services, "_busy", lambda s: [])
    try:
        assert api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION}).status_code == 200
    finally:
        api.post("/api/settings/mode", json={"paid": False})


def test_paid_mode_on_vast_needs_only_the_vast_key(api, cfg, monkeypatch):
    from app.api import services
    # Makine henüz yokken de geçilir: ilk makine ücretli modda "Yeni makine kirala" ile kurulur.
    cfg.write_text(H3_ENV.replace("VAST_INSTANCE_ID=4242", "VAST_INSTANCE_ID="))
    rc._redis().incr(rc.VERSION_KEY)
    monkeypatch.setattr(services, "_busy", lambda s: [])
    try:
        r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
        assert r.status_code == 200 and settings.dry_run is False
    finally:
        api.post("/api/settings/mode", json={"paid": False})
    assert settings.dry_run is True


def test_paid_mode_needs_the_paid_packages(api, cfg, monkeypatch):
    from app import live
    cfg.write_text(H3_ENV.replace("FAL_KEY=", f"FAL_KEY={SECRET}"))      # fal anahtarı varsa paketi de gerekir
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
    cfg.write_text(H3_ENV)
    for confirm in (None, "evet", "ücretli mod"):
        r = api.post("/api/settings/mode", json={"paid": True, "confirm": confirm})
        assert r.status_code == 422 and rc.PAID_CONFIRMATION in r.json()["detail"]
    assert settings.dry_run is True and "DRY_RUN=1" in cfg.read_text()


def test_paid_mode_is_refused_while_production_runs(api, cfg, monkeypatch):
    from app.api import services
    cfg.write_text(H3_ENV)
    monkeypatch.setattr(services, "_busy", lambda s: ["Film S001_SH01"])
    r = api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION})
    assert r.status_code == 409 and "S001_SH01" in r.json()["detail"] and settings.dry_run is True


def test_switching_to_paid_and_back_is_written_and_logged(api, cfg, monkeypatch):
    from app.api import services
    cfg.write_text(H3_ENV)                                              # fal.ai anahtarı olmadan da
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
    (lambda t: t.replace("self_hosted:\n  h3:", "self_hosted:\n  h4:"), "self_hosted.h3 eksik"),
    (lambda t: t.replace("gpu_hourly_usd: 0.95", "gpu_hourly_usd: 0"), "self_hosted.h3.gpu_hourly_usd"),
    (lambda t: t.replace('"480p": 8 ', '"480p": -5 '), "self_hosted.h3.render_seconds.480p"),
    (lambda t: t.replace("keyframe_seconds: 40", "keyframe_seconds: -1"), "self_hosted.h3.keyframe_seconds"),
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


def test_the_h3_connection_check_reads_the_box_and_the_servers_health(api, cfg, monkeypatch):
    from app.live import probe
    seen = []
    assert api.post("/api/settings/test/vast").json() == {"ok": False, "status": None, "message": "Anahtar tanımlı değil."}
    cfg.write_text(H3_ENV)
    rc._redis().incr(rc.VERSION_KEY)                                     # panel kaydetmiş gibi: süreç dosyayı yeniden okur

    def handler(request):
        seen.append(request)
        if request.url.host == "console.vast.ai":
            return httpx.Response(200, json={"instances": {"actual_status": "running", "dph_total": 0.87,
                                                           "geolocation": "Quebec, CA", "gpu_name": "RTX 5090"}})
        return httpx.Response(200)
    real = probe.check
    monkeypatch.setattr(probe, "check", lambda service, key, transport=None: real(service, key, httpx.MockTransport(handler)))
    r = api.post("/api/settings/test/vast").json()
    assert r["ok"] is True and "Quebec, CA" in r["message"] and r["message"].endswith("Sunucu hazır.")
    assert [(q.method, str(q.url)) for q in seen] == [("GET", "https://console.vast.ai/api/v0/instances/4242"),
                                                      ("GET", "https://h3.test/system_stats")]          # ComfyUI
    assert seen[0].headers["authorization"] == f"Bearer {VAST_SECRET}" and seen[1].headers["authorization"] == f"Bearer {H3_SECRET}"


I2V = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
       "8": {"class_type": "LoadImage", "inputs": {"image": "{{first_frame}}"}}}


def test_comfyui_workflows_are_validated_before_they_are_saved(api, cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "h3_comfy_workflow_dir", str(tmp_path / "comfy"))
    flows = {w["kind"]: w for w in api.get("/api/settings").json()["comfy"]["workflows"]}
    assert set(flows) == {"t2v", "i2v", "r2v", "keyframe", "edit_keyframe"} and not any(w["present"] for w in flows.values())
    bad = api.put("/api/settings/comfy/i2v", json={"text": json.dumps({"6": I2V["6"]})})
    assert bad.status_code == 422 and "first_frame" in bad.json()["detail"]
    ui = api.put("/api/settings/comfy/i2v", json={"text": json.dumps({"nodes": [], "links": []})})
    assert ui.status_code == 422 and "Export (API)" in ui.json()["detail"]
    ok = api.put("/api/settings/comfy/i2v", json={"text": json.dumps(I2V)})
    assert ok.status_code == 200 and (tmp_path / "comfy" / "i2v.json").exists()
    i2v = next(w for w in ok.json()["workflows"] if w["kind"] == "i2v")
    assert i2v["present"] and i2v["placeholders"] == ["first_frame", "prompt"] and i2v["error"] is None
    assert api.put("/api/settings/comfy/nope", json={"text": "{}"}).status_code == 422
    assert api.delete("/api/settings/comfy/i2v").status_code == 200 and not (tmp_path / "comfy" / "i2v.json").exists()


def test_the_box_cannot_be_started_or_stopped_in_free_mode(api, cfg):
    r = api.post("/api/settings/vast/instance", json={"action": "start"})
    assert r.status_code == 409 and "Ücretsiz modda" in r.json()["detail"]
    assert api.post("/api/settings/vast/instance", json={"action": "reboot"}).status_code == 422


def test_going_back_to_free_mode_stops_a_running_box(api, cfg, monkeypatch):
    from app.api import services
    from app.live import vast_api
    from tests.h3_fake import FakeVast
    fake = FakeVast()
    cfg.write_text(H3_ENV.replace(VAST_SECRET, "test-vast_api_key"))
    rc._redis().incr(rc.VERSION_KEY)
    monkeypatch.setattr(services, "_busy", lambda s: [])
    monkeypatch.setattr(vast_api, "TRANSPORT", fake.transport())
    try:
        assert api.post("/api/settings/mode", json={"paid": True, "confirm": rc.PAID_CONFIRMATION}).status_code == 200
    finally:
        back = api.post("/api/settings/mode", json={"paid": False}).json()
    assert back["dry_run"] is True and fake.puts == [{"state": "stopped"}] and "durduruldu" in back["vast_note"]
