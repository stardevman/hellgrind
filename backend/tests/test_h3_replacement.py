"""Yedek MiniMax H3 makinesi: otomatik başlatma başarısız olunca (durdurulan makinenin GPU'su başkasına kiralanmış) uygun
bölgede yeni teklif bulunur, yönetmen onaylarsa (ya da auto) kiralanır, setup.sh ile kurulur, Ayarlar güncellenir ve
eski makine silinir. Sahte vast.ai ve sahte ComfyUI ile; hiçbir istek gerçek servise gitmez.
"""
import base64
import gzip
import json
import pathlib
import re
import subprocess

import pytest

from app import runtime_config as rc
from app.config import settings
from app.engine.providers import GenRequest
from app.live import PaidCallBlocked, vast_api, vast_box, vast_h3
from app.live.vast_h3 import H3Provider
from tests.h3_fake import FakeComfy, FakeVast
from tests.media_utils import make_video

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SETUP = BACKEND / "vast" / "setup.sh"


def request(**kw):
    return GenRequest(**{**dict(prompt="p", seed=1, resolution="720p", duration_s=4.0), **kw})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Geçici .env (yeni makinenin ayarları buraya yazılır) ve teste özel Redis anahtarları."""
    path = tmp_path / ".env"
    path.write_text("DRY_RUN=0\n")
    for f in rc.FIELDS:
        monkeypatch.setattr(settings, f.attr, getattr(settings, f.attr))
    monkeypatch.setattr(settings, "config_env_file", str(path))
    monkeypatch.setattr(rc, "VERSION_KEY", "hg:test:config:version")
    monkeypatch.setattr(rc, "AUDIT_KEY", "hg:test:config:audit")
    monkeypatch.setattr(rc, "_seen", None)
    yield path
    rc._redis().delete(rc.AUDIT_KEY, rc.VERSION_KEY)


@pytest.fixture
def vast(h3, env, monkeypatch, tmp_path):
    fake = FakeVast(status="stopped")
    monkeypatch.setattr(vast_api, "TRANSPORT", fake.transport())
    comfy = FakeComfy(open(make_video(tmp_path / "c.mp4", seconds=4.0, size="320x180", fps=24), "rb").read())
    monkeypatch.setattr(vast_h3, "TRANSPORT", comfy.transport())
    for name, value in dict(vast_instance_id="4242", h3_auto_start=True, h3_replace_mode="ask", h3_backend="comfyui",
                            h3_min_ram_gb=128, h3_min_disk_gb=200, h3_min_inet_mbps=1000, h3_max_dph=0.95, h3_gpu_name="",
                            h3_allow_nordic=False, h3_tls_mode="verify", h3_idle_stop_minutes=10,
                            h3_setup_timeout_s=60, h3_start_timeout_s=60, dry_run=False).items():
        monkeypatch.setattr(settings, name, value)
    for name, value in dict(START_POLL_S=0, SETUP_POLL_S=0, BOOT_WAIT_S=0).items():
        monkeypatch.setattr(vast_box, name, value)
    r = vast_box._r()
    for name in ("inflight", "last_activity", "last_project", "tick", "box", "starting", "proposal", "replacement",
                 "replacing", "retrieval"):
        r.delete(vast_box._k(name))
    flows = tmp_path / "comfy"
    flows.mkdir(exist_ok=True)
    (flows / "t2v.json").write_text(json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}}}))
    sent = []
    monkeypatch.setattr("app.worker.celery_app.celery.send_task", lambda name, args=None, **kw: sent.append((name, args)))
    fake.sent, fake.comfy = sent, comfy
    return fake


# ---------- teklif arama ----------

def test_the_offer_query_asks_for_the_same_gpu_in_allowed_regions_under_the_cap():
    q = vast_api.offer_query("RTX 5090", 0.95, 64, 150, 300)
    assert q["gpu_name"] == {"in": ["RTX 5090", "RTX_5090"]} and q["dph_total"] == {"lte": 0.95}
    assert q["cpu_ram"] == {"gte": 64000} and q["disk_space"] == {"gte": 150} and q["inet_down"] == {"gte": 300}
    assert q["geolocation"] == {"in": ["CA"]} and q["rentable"] == {"eq": True}
    assert q["order"] == [["dph_total", "asc"]] and q["num_gpus"] == {"eq": 1}


def test_offers_from_excluded_or_unknown_regions_never_come_back(vast, monkeypatch):
    vast.offers = [{"id": 1, "gpu_name": "RTX 5090", "dph_total": 0.7, "geolocation": "Texas, US", "cpu_ram": 128000, "disk_space": 400},
                   {"id": 2, "gpu_name": "RTX 5090", "dph_total": 0.6, "geolocation": None, "cpu_ram": 128000, "disk_space": 400},
                   {"id": 3, "gpu_name": "RTX 5090", "dph_total": 0.9, "geolocation": "Quebec, CA", "cpu_ram": 128000, "disk_space": 400},
                   {"id": 4, "gpu_name": "RTX 5090", "dph_total": 0.8, "geolocation": "Norway, NO", "cpu_ram": 128000, "disk_space": 400},
                   {"id": 5, "gpu_name": "RTX 5090", "dph_total": 0.5, "geolocation": "Serbia, RS", "cpu_ram": 32000, "disk_space": 400}]
    vast.offers.append({"id": 6, "gpu_name": "RTX 5090", "dph_total": 0.4, "geolocation": "Puerto Rico, PR", "cpu_ram": 128000, "disk_space": 400})
    vast.offers.append({"id": 7, "gpu_name": "RTX 5090", "dph_total": 0.45, "geolocation": "Canarias, ES", "cpu_ram": 128000, "disk_space": 400})
    assert [o.id for o in vast_api.search_offers("RTX 5090", 0.95, 64, 150, 300)] == [3]          # yalnızca Kanada
    monkeypatch.setattr(settings, "h3_allow_nordic", True)
    assert [o.id for o in vast_api.search_offers("RTX 5090", 0.95, 64, 150, 300)] == [4, 3]


# ---------- başlatma başarısız ----------

def test_a_box_that_cannot_start_leads_to_a_proposal_waiting_for_approval(vast):
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked, match="onay bekliyor.*RTX 5090, 0.81 \\$/sa, Quebec, CA"):
        H3Provider().generate(request())
    prop = vast_box.proposal()
    assert prop["offer_id"] == 7001 and prop["old_instance_id"] == "4242" and "açılmadı" in prop["reason"]
    assert vast.rents == [] and vast.sent == []                                    # onay olmadan kiralanmaz


def test_auto_mode_rents_without_asking_when_under_the_cap(vast, monkeypatch):
    monkeypatch.setattr(settings, "h3_replace_mode", "auto")
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked, match="Yeni makine kiralanıyor"):
        H3Provider().generate(request())
    assert vast.sent == [("hg.h3_replace", [7001])] and vast_box.job()["state"] == "queued"
    with pytest.raises(PaidCallBlocked, match="Yeni H3 makinesi sırada"):              # ikinci istek eskiyi zorlamaz
        H3Provider().generate(request())
    assert len(vast.sent) == 1


def test_without_a_suitable_offer_or_with_replacement_off_the_reason_is_explained(vast, monkeypatch):
    vast.start_fails = True
    vast.offers = []
    with pytest.raises(PaidCallBlocked, match="Uygun yedek teklif bulunamadı.*0.95 \\$/sa.*128 GB RAM.*200 GB disk.*1000 Mbps"):
        H3Provider().generate(request())
    monkeypatch.setattr(settings, "h3_replace_mode", "off")
    with pytest.raises(PaidCallBlocked, match="Yedek makine kapalı"):
        H3Provider().generate(request())
    assert vast.searches and len(vast.searches) == 1                                # kapalıyken teklif aranmaz


def test_a_box_that_starts_normally_needs_no_replacement(vast):
    assert H3Provider().generate(request(image_url=None)).request_id == "p1"
    assert vast.puts == [{"state": "running"}] and vast.searches == [] and vast_box.proposal() is None


# ---------- kiralama ve kurulum ----------

def test_the_replacement_is_rented_installed_written_to_settings_and_the_old_box_deleted(vast, env):
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request())
    assert vast_box.replace(7001) == "ready"
    (offer, body), = vast.rents
    assert offer == "7001" and body["image"] == settings.h3_box_image and body["runtype"] == "ssh_direct"
    assert body["disk"] == 200 and body["env"]["WEB_PASSWORD"] == "test-h3_api_key"          # portal belirteci korunur
    assert body["env"]["OPEN_BUTTON_TOKEN"] == "test-h3_api_key"
    assert body["env"]["PORTAL_CONFIG"] == ("localhost:1111:11111:/:Instance Portal|localhost:8188:18188:/:ComfyUI"
                                            "|localhost:8189:18189:/:Hellgrind kurulum")
    assert body["env"]["-p 8188:8188"] == "1" and body["env"]["-p 8189:8189"] == "1"
    assert body["env"]["HG_IMAGE_MODEL"] == "qwen-image-2512" and body["env"]["HG_LOG_PORT"] == "18189"
    assert body["onstart"].endswith("entrypoint.sh")
    written = rc.read_env(str(env))
    assert written["VAST_INSTANCE_ID"] == "9001" and written["H3_ENDPOINT_URL"] == "https://203.0.113.9:41234"
    assert written["H3_TLS_MODE"] == "pin" and settings.h3_backend == "comfyui"
    assert settings.vast_instance_id == "9001" and settings.h3_tls_mode == "pin"
    assert vast.deletes == ["4242"] and vast_box.proposal() is None
    job = vast_box.job()
    assert job["state"] == "ready" and job["new_instance_id"] == "9001" and "4242 silindi" in job["message"]
    assert job["log_url"] == "https://203.0.113.9:41236" and {"renting", "booting", "installing", "ready"} <= set(job["steps"])
    assert any(r.url.path == "/system_stats" and r.url.host == "203.0.113.9" for r in vast.comfy.requests)


def test_without_a_portal_token_one_is_generated_and_saved(vast, env, monkeypatch):
    monkeypatch.setattr(settings, "h3_api_key", "")
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request())
    vast_box.replace(7001)
    token = vast.rents[0][1]["env"]["WEB_PASSWORD"]
    assert len(token) >= 24 and rc.read_env(str(env))["H3_API_KEY"] == token
    assert all(r.headers.get("Authorization") == f"Bearer {token}" for r in vast.comfy.requests)


def test_a_failed_setup_stops_the_new_box_and_keeps_the_old_one(vast, monkeypatch):
    monkeypatch.setattr(settings, "h3_setup_timeout_s", 0)
    monkeypatch.setattr(vast_h3, "healthy", lambda url, token: False)
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request())
    assert vast_box.replace(7001) == "failed"
    assert vast_box.job()["state"] == "failed" and "kurulum" in vast_box.job()["message"]
    assert vast.deletes == [] and {"state": "stopped", "id": "9001"} in vast.puts


def test_the_idle_watcher_does_not_stop_a_box_that_is_being_installed(vast, mk_project):
    import time
    pid = mk_project(10.0)
    vast.status = "running"
    r = vast_box._r()
    r.set(vast_box._k("last_activity"), time.time() - 3600)
    r.set(vast_box._k("tick"), time.time() - 60)
    r.set(vast_box._k("last_project"), pid)
    r.set(vast_box._k("replacing"), time.time())
    assert vast_box.idle_tick(queue_empty=lambda: True).startswith("kuruluyor") and vast.puts == []


# ---------- onay ucu ----------

def test_the_panel_shows_the_proposal_and_approval_starts_the_rental(api, vast, monkeypatch):
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request())
    state = api.get("/api/settings/vast/replacement").json()
    assert state["mode"] == "ask" and state["proposal"]["offer_id"] == 7001 and state["job"] is None
    r = api.post("/api/settings/vast/replacement", json={"action": "approve"})
    assert r.status_code == 200 and r.json()["job"]["state"] == "queued" and vast.sent == [("hg.h3_replace", [7001])]
    assert api.post("/api/settings/vast/replacement", json={"action": "approve"}).status_code == 409   # zaten sürüyor
    assert api.post("/api/settings/vast/replacement", json={"action": "reject"}).json()["proposal"] is None
    assert api.post("/api/settings/vast/replacement", json={"action": "reject"}).status_code == 404


def test_nothing_is_rented_in_free_mode(api, vast, monkeypatch):
    vast.start_fails = True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request())
    monkeypatch.setattr(settings, "config_env_file", "")                  # .env'deki DRY_RUN=0 yeniden okunmasın
    monkeypatch.setattr(settings, "dry_run", True)
    assert api.post("/api/settings/vast/replacement", json={"action": "approve"}).status_code == 409
    assert vast_box.replace(7001) == "skip" and vast.rents == []
    from app.worker.tasks import h3_replace
    assert h3_replace(7001) == "skip"


# ---------- setup.sh ----------

def test_setup_sh_is_valid_bash_and_installs_what_the_workflows_need():
    assert subprocess.run(["bash", "-n", str(SETUP)], capture_output=True).returncode == 0
    text = SETUP.read_text()
    for name in ("minimax_h3_fl2va_pruned_int8_convrot.safetensors", "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
                 "minimax_h3_video_vae_int8_convrot.safetensors", "minimax_h3_audio_vae_fp32.safetensors",
                 "qwen_image_2512_fp8_e4m3fn.safetensors", "qwen_2.5_vl_7b_fp8_scaled.safetensors", "qwen_image_vae.safetensors",
                 "flux1-schnell-fp8.safetensors", "--listen 127.0.0.1", "--port $PORT"):
        assert name in text
    assert "flux1-dev" not in text                                              # ticari olmayan lisans: kurulmaz


def test_the_onstart_command_embeds_setup_sh_within_vast_limits(monkeypatch):
    cmd = vast_box.onstart_script()
    blob = re.search(r"echo (\S+) \| base64 -d \| gunzip", cmd).group(1)
    assert gzip.decompress(base64.b64decode(blob)) == SETUP.read_bytes() and len(cmd) < 16_000
    monkeypatch.setattr(settings, "h3_setup_script_url", "https://example.org/setup.sh")
    assert vast_box.onstart_script().startswith("mkdir -p /root/hellgrind && curl -fsSL https://example.org/setup.sh")


@pytest.mark.parametrize("name", ["keyframe.qwen-image-2512.json", "keyframe.flux-schnell.json"])
def test_the_example_keyframe_workflows_are_valid_and_use_apache_models(name):
    path = BACKEND.parent / "comfy-workflows" / "examples" / name
    if not path.exists():
        pytest.skip("depo kökü bağlı değil")
    flow = vast_h3.validate_workflow("keyframe", path.read_text())
    assert {"prompt", "seed", "width", "height"} <= vast_h3.placeholders(flow)
    assert "flux1-dev" not in json.dumps(flow)                                 # ticari olmayan lisanslı model yok
