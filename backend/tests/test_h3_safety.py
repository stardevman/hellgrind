"""Gerçek kiralamadan önce: çıktı güvencesi (her H3 çıktısı indirilip yerel depoda doğrulanır; eksik çıktılı eski makine
silinmez), makine koşulları, "Yeni makine kirala" ve setup.sh günlüğü. Sahte vast.ai, sahte H3/ComfyUI ve test kovasıyla;
hiçbir istek gerçek servise gitmez.
"""
import hashlib
import json
import pathlib
import subprocess
import time

import pytest
from sqlalchemy import text

from app import runtime_config as rc
from app import storage
from app.config import settings
from app.db import session_scope
from app.engine.providers import GenRequest, TransientError
from app.live import PaidCallBlocked, h3_outputs, vast_box, vast_h3
from app.live.vast_h3 import H3Provider
from tests.test_h3_replacement import env, vast  # noqa: F401 — ortak fikstürler

SETUP = pathlib.Path(__file__).resolve().parents[1] / "vast" / "setup.sh"


def request(**kw):
    return GenRequest(**{**dict(prompt="p", seed=1, resolution="720p", duration_s=4.0), **kw})


def rows(instance_id=None) -> list[dict]:
    with session_scope() as s:
        q = "SELECT job_id, status, kind, size_bytes, sha256, storage_key, meta_key, instance_id, remote, error FROM h3_outputs"
        found = s.execute(text(q + (" WHERE instance_id = :i" if instance_id else "") + " ORDER BY id"),
                          {"i": instance_id} if instance_id else {}).mappings().all()
    return [dict(r) for r in found]


def fail_downloads(vast, n=3):
    """Makinedeki çıktı indirilemez (3 deneme): iş bitti ama çıktı yalnızca makinede kaldı."""
    vast.comfy.view_errors = [503] * n


# ---------- her çıktı kaydedilir, indirilir, doğrulanır ----------

def test_every_h3_output_is_registered_downloaded_and_verified_in_local_storage(h3):
    result = H3Provider().generate(request())
    (row,) = rows()
    assert row["job_id"] == result.request_id and row["status"] == "stored" and row["kind"] == "video"
    assert row["size_bytes"] == len(h3.video) and row["sha256"] == hashlib.sha256(h3.video).hexdigest()
    assert row["storage_key"] == f"h3-outputs/{row['sha256'][:2]}/{row['sha256']}.mp4"
    assert storage.size_of(row["storage_key"]) == len(h3.video) and storage.sha256_of(row["storage_key"]) == row["sha256"]
    meta = json.loads(storage.read_bytes(row["meta_key"]))
    assert meta["job_id"] == result.request_id and meta["output"]["sha256"] == row["sha256"]
    assert meta["provenance"]["license"] == vast_h3.LICENSE and meta["remote"]["gpu_seconds"] == 42.0


def test_a_short_download_is_retried_and_never_stored_as_complete(h3):
    h3.content_errors = ["short"]
    H3Provider().generate(request())
    (row,) = rows()
    assert row["status"] == "stored" and row["size_bytes"] == len(h3.video)
    assert sum(1 for r in h3.requests if r.url.path.endswith("/content")) == 2


def test_a_failed_job_is_recorded_as_failed(h3):
    h3.failing_jobs = 1
    with pytest.raises(vast_h3.JobFailed):
        H3Provider().generate(request())
    (row,) = rows()
    assert row["status"] == "failed" and "CUDA out of memory" in row["error"]


def test_an_unverifiable_copy_keeps_the_output_unsafe_and_a_retry_stores_it_without_regenerating(h3, monkeypatch):
    provider = H3Provider()
    real = storage.sha256_of
    monkeypatch.setattr(storage, "sha256_of", lambda key: "0" * 64)
    with pytest.raises(TransientError, match="uyuşmuyor"):
        provider.generate(request())
    assert [r["status"] for r in rows()] == ["completed"]
    monkeypatch.setattr(storage, "sha256_of", real)
    provider.generate(request())                                       # aynı iş beklenir, yeniden üretilmez
    assert [r["status"] for r in rows()] == ["stored"] and len(h3.submits) == 1


def test_comfy_keyframes_are_archived_too_and_carry_a_job_unique_file_prefix(vast, tmp_path):
    from PIL import Image
    png = tmp_path / "kf.png"
    Image.new("RGB", (64, 36), (120, 40, 20)).save(png)
    vast.comfy.image, vast.comfy.output_ext = png.read_bytes(), ".png"
    vast.status = "running"
    (tmp_path / "comfy" / "keyframe.json").write_text(json.dumps({
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}))
    result = vast_h3.keyframe_image("a bridge", seed=7)
    (row,) = rows("4242")
    assert row["kind"] == "keyframe" and row["status"] == "stored" and row["storage_key"].endswith(".png")
    prefix = vast.comfy.prompts[-1]["9"]["inputs"]["filename_prefix"]
    assert prefix.startswith("hellgrind/keyframe_") and row["remote"]["prefix"] == prefix and result.local_path


# ---------- eski makine yalnızca çıktıları güvendeyse silinir ----------

def retained(vast):
    """Eski makine (4242) bir çıktısını makinede bırakmış; açılmıyor, yedek makine kuruluyor."""
    vast.status = "running"
    fail_downloads(vast)
    with pytest.raises(TransientError):
        H3Provider().generate(request(image_url=None))
    assert [r["status"] for r in rows("4242")] == ["completed"]
    vast.status, vast.start_fails = "stopped", True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request(image_url=None, seed=2))
    return vast_box.replace(7001)


def test_the_old_box_is_kept_while_any_of_its_outputs_exists_only_on_it(vast, env):
    assert retained(vast) == "ready"
    assert vast.deletes == []
    job = vast_box.job()
    assert job["retained"] == {"instance_id": "4242", "missing": 1} and "4242 SİLİNMEDİ" in job["message"]


def test_retrieving_the_missing_outputs_starts_the_old_box_then_deletes_it(vast, env):
    retained(vast)
    vast.start_fails = False
    assert vast_box.retrieve_outputs("4242", destroy_after=True) == "done"
    assert [r["status"] for r in rows("4242")] == ["stored"] and vast.deletes == ["4242"]
    assert {"state": "running"} in vast.puts
    state = vast_box.retrieval()
    assert state["state"] == "done" and state["counts"]["stored"] == 1 and "4242 silindi" in state["message"]


def test_an_old_box_that_does_not_start_is_neither_deleted_nor_forgotten(vast, env, monkeypatch):
    retained(vast)
    monkeypatch.setattr(vast_box, "START_GRACE_S", 0)
    assert vast_box.retrieve_outputs("4242", destroy_after=True) == "failed"
    assert vast.deletes == [] and [r["status"] for r in rows("4242")] == ["completed"]
    assert "açılmadı" in vast_box.retrieval()["message"]


def test_accepting_the_loss_marks_outputs_lost_and_deletes_the_old_box(vast, env):
    retained(vast)
    assert vast_box.abandon("4242") == {"instance_id": "4242", "lost": 1}
    assert [r["status"] for r in rows("4242")] == ["lost"] and vast.deletes == ["4242"]
    assert vast_box.job()["retained"] is None
    with pytest.raises(PaidCallBlocked, match="Ayarlardaki makine"):
        vast_box.abandon(settings.vast_instance_id)


def test_the_panel_lists_unsafe_outputs_and_offers_both_decisions(api, vast, env):
    retained(vast)
    state = api.get("/api/settings/vast/replacement").json()
    old = next(m for m in state["outputs"]["machines"] if m["instance_id"] == "4242")
    assert old["unsafe"] == 1 and old["current"] is False and state["outputs"]["unsafe"][0]["status"] == "completed"
    assert state["tls"]["mode"] == "pin" and state["instance_id"] == "9001"
    r = api.post("/api/settings/vast/outputs", json={"action": "retrieve", "instance_id": "4242", "destroy_after": True})
    assert r.status_code == 200 and ("hg.h3_retrieve", ["4242", True]) in vast.sent
    assert api.post("/api/settings/vast/outputs", json={"action": "retrieve", "instance_id": "4242"}).status_code == 409
    vast_box._r().delete(vast_box._k("retrieval"))
    r = api.post("/api/settings/vast/outputs", json={"action": "abandon", "instance_id": "4242"})
    assert r.status_code == 200 and r.json()["lost"] == 1 and vast.deletes == ["4242"]


def test_the_idle_watcher_retrieves_downloads_left_behind_on_the_running_box(vast, mk_project):
    vast.status = "running"
    fail_downloads(vast)
    with pytest.raises(TransientError):
        H3Provider().generate(request(image_url=None))
    with session_scope() as s:                                          # worker çöktü: indirme yarım kaldı
        s.execute(text("""UPDATE h3_outputs SET remote = jsonb_set(remote, '{completed_at}', to_jsonb(0))"""))
    r = vast_box._r()
    r.set(vast_box._k("last_activity"), time.time())
    r.set(vast_box._k("last_project"), mk_project(10.0))
    vast_box.idle_tick(queue_empty=lambda: True)
    assert [x["status"] for x in rows("4242")] == ["stored"]


def test_an_output_whose_history_was_lost_is_found_by_its_unique_prefix(vast):
    h3_outputs.submitted("https://h3.test", "lost-1", "video", "fl2va", None, {"prefix": "hellgrind/i2v_abcdef0123456789"})
    with session_scope() as s:
        s.execute(text("UPDATE h3_outputs SET instance_id = '4242', submitted_at = now() - interval '1 day'"))
    counts = h3_outputs.retrieve("4242", "https://h3.test", "tok", settle=True)
    assert counts["stored"] == 1 and counts["remaining"] == 0
    view = [x for x in vast.comfy.requests if x.url.path == "/view"]
    assert view[-1].url.params["filename"] == "i2v_abcdef0123456789_00001_.mp4"
    assert view[-1].url.params["subfolder"] == "hellgrind"


def test_a_lost_output_that_cannot_be_found_stays_listed(vast):
    vast.comfy.files_missing = True
    h3_outputs.submitted("https://h3.test", "lost-2", "video", "fl2va", None, {"prefix": "hellgrind/i2v_0000"})
    with session_scope() as s:
        s.execute(text("UPDATE h3_outputs SET instance_id = '4242'"))
    counts = h3_outputs.retrieve("4242", "https://h3.test", "tok")
    assert counts["missing"] == 1 and counts["remaining"] == 1
    assert "tanımıyor" in rows("4242")[0]["error"]


def test_nothing_is_retrieved_or_deleted_in_free_mode(api, vast, monkeypatch):
    monkeypatch.setattr(settings, "config_env_file", "")
    monkeypatch.setattr(settings, "dry_run", True)
    assert api.post("/api/settings/vast/outputs", json={"action": "abandon", "instance_id": "4242"}).status_code == 409
    assert vast_box.retrieve_outputs("4242") == "skip" and vast.deletes == []
    from app.worker.tasks import h3_retrieve
    assert h3_retrieve("4242", True) == "skip"


# ---------- yeni makine kirala ----------

def test_provisioning_reuses_the_replacement_flow_and_deletes_nothing(api, vast, env):
    vast.status = "running"                                             # Ayarlar'daki makine (4242) çalışıyor
    r = api.post("/api/settings/vast/provision")
    assert r.status_code == 200, r.text
    prop = r.json()["proposal"]
    assert prop["kind"] == "initial" and prop["old_instance_id"] is None and "4242" in prop["reason"]
    q = vast.searches[-1]
    assert q["cpu_ram"] == {"gte": 128000} and q["disk_space"] == {"gte": 200} and q["inet_down"] == {"gte": 1000}
    assert api.post("/api/settings/vast/replacement", json={"action": "approve"}).json()["job"]["kind"] == "initial"
    assert vast.sent == [("hg.h3_replace", [7001])]
    assert vast_box.replace(7001) == "ready"
    assert vast.deletes == [] and settings.vast_instance_id == "9001" and settings.h3_tls_mode == "pin"
    job = vast_box.job()
    assert "silin" not in job["message"] and job["old_instance_id"] is None
    assert rc.read_env(str(env))["H3_ENDPOINT_URL"] == "https://203.0.113.9:41234"


def test_provisioning_needs_paid_mode_and_a_suitable_offer(api, vast, monkeypatch):
    vast.offers = []
    r = api.post("/api/settings/vast/provision")
    assert r.status_code == 409 and "Uygun teklif bulunamadı" in r.json()["detail"] and "128 GB RAM" in r.json()["detail"]
    monkeypatch.setattr(settings, "config_env_file", "")
    monkeypatch.setattr(settings, "dry_run", True)
    assert api.post("/api/settings/vast/provision").status_code == 409 and vast.rents == []


# ---------- kurulum günlüğü ----------

LOG = """[hellgrind 10:00:00 +0 dk 00 sn] kurulum başladı (görsel modeli: qwen-image-2512)
[hellgrind 10:00:01 +0 dk 01 sn] == 1/4 ComfyUI ve Python ortamı ==
[hellgrind 10:03:01 +3 dk 01 sn] == 1/4 ComfyUI ve Python ortamı bitti (180 sn) ==
[hellgrind 10:03:01 +3 dk 01 sn] == 2/4 MiniMax H3 modelleri ==
[hellgrind 10:03:05 +3 dk 05 sn] indi: minimax_h3_fl2va_pruned_int8_convrot.safetensors (21504 MB, 160 sn, 134 MB/sn)
"""


def test_the_setup_log_is_read_through_the_portal_and_parsed(vast, env):
    vast.status, vast.start_fails = "stopped", True
    with pytest.raises(PaidCallBlocked):
        H3Provider().generate(request(image_url=None))
    vast_box.replace(7001)
    vast.comfy.setup_log = LOG
    log = vast_box.setup_log()
    assert log["available"] and log["step"] == "2/4 MiniMax H3 modelleri" and not log["done"]
    assert log["phases"] == [{"name": "1/4 ComfyUI ve Python ortamı", "seconds": 180}]
    vast.comfy.setup_log = LOG + "[hellgrind 10:40:00 +40 dk 00 sn] KURULUM_BITTI durum=ok sure=2400\n"
    log = vast_box.setup_log()
    assert log["done"] and log["ok"] and log["total_s"] == 2400
    assert any(r.url.path == "/setup.log" and r.url.port == 41236 for r in vast.comfy.requests)


def test_setup_sh_prints_the_lines_the_panel_parses():
    """setup.sh'nin zaman yardımcıları çalıştırılır: çıktı panelin ayrıştırıcısıyla okunabilmeli."""
    helpers = [line for line in SETUP.read_text().splitlines()
               if line.startswith(("since()", "step()", "phase_end()", "phase()"))]
    assert len(helpers) == 4
    script = "\n".join(["START=$(date +%s)", 'PHASE=""', "PHASE_T=0", *helpers,
                        'phase "1/4 Deneme"', 'phase "2/4 İkinci"', "phase_end",
                        'step "KURULUM_BITTI durum=ok sure=$(( $(date +%s) - START ))"'])
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.splitlines()
    parsed = vast_box.parse_setup_log(out)
    assert [p["name"] for p in parsed["phases"]] == ["1/4 Deneme", "2/4 İkinci"] and parsed["done"] and parsed["ok"]
    text_ = SETUP.read_text()
    assert "toplam kurulum süresi" in text_ and "portal sertifikası SHA-256" in text_ and "http.server" in text_
