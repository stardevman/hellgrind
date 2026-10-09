"""MiniMax H3 on RunPod Serverless (varsayılan platform): iş gönderme, çıktı güvencesi, bedel, bölge (izinli liste), bakiye
koruması, uç nokta ayarı, dakikalık görev ve panel uçları. Sahte RunPod ve test kovasıyla; hiçbir istek gerçek servise
gitmez."""
import hashlib
import json
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import storage
from app.budget import pricing
from app.config import settings
from app.db import session_scope
from app.engine.providers import GenRequest, TransientError
from app.live import PaidCallBlocked, h3_outputs, runpod_h3, vast_box, vast_h3
from tests.media_utils import make_video
from tests.runpod_fake import FakeRunpod

RATE_5090 = 0.000439


def request(**kw):
    return GenRequest(**{**dict(prompt="p", seed=1, resolution="720p", duration_s=5.0), **kw})


@pytest.fixture
def runpod(monkeypatch, tmp_path, paid, s3):
    from PIL import Image
    video = pathlib.Path(make_video(tmp_path / "rp.mp4", seconds=5.0, size="320x180", fps=24)).read_bytes()
    png = tmp_path / "kf.png"
    Image.new("RGB", (64, 36), (20, 90, 160)).save(png)
    fake = FakeRunpod(video, png.read_bytes())
    monkeypatch.setattr(runpod_h3, "TRANSPORT", fake.transport())
    monkeypatch.setattr(runpod_h3, "POLL_S", 0)
    flows = tmp_path / "comfy"
    flows.mkdir()
    for kind, needs in (("t2v", ""), ("i2v", "{{first_frame}}"), ("r2v", "{{reference_1}}"), ("keyframe", "")):
        (flows / f"{kind}.json").write_text(json.dumps({
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "seed": "{{seed}}", "img": needs}},
            "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}))
    for name, value in dict(h3_platform="runpod", runpod_endpoint_id="ep123", runpod_datacenter="CA-MTL-1",
                            runpod_network_volume_id="vol1", runpod_gpu_types="RTX 5090,RTX PRO 6000",
                            runpod_idle_timeout_s=60, runpod_min_balance_usd=10.0, h3_allow_nordic=False,
                            h3_comfy_workflow_dir=str(flows), work_dir=str(tmp_path / "work")).items():
        monkeypatch.setattr(settings, name, value)
    with session_scope() as s:
        s.execute(text("DELETE FROM h3_outputs"))
    r = vast_box._r()
    for name in ("rp_balance", "rp_config", "rp_health", "rp_tick", "rp_last_tick", "rp_balance_error", "inflight",
                 "last_activity", "last_project", "retrieval"):
        r.delete(vast_box._k(name))
    for pattern in ("rp_job:*", "rp_pending:*"):                 # önceki testlerin gönderilmiş iş kayıtları
        for key in r.scan_iter(vast_box._k(pattern)):
            r.delete(key)
    return fake


def rows() -> list[dict]:
    q = """SELECT job_id, status, kind, backend, instance_id, server, size_bytes, sha256, storage_key, meta_key, remote, error
           FROM h3_outputs ORDER BY id"""
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(q)).mappings().all()]


# ---------- üretim ----------

def test_a_clip_is_submitted_waited_downloaded_verified_and_charged_by_gpu_time(runpod, tmp_path):
    from app.live.vast_h3 import data_uri
    from PIL import Image
    kf = tmp_path / "first.jpg"
    Image.new("RGB", (320, 180), (200, 120, 40)).save(kf)
    result = runpod_h3.get_live_provider().generate(request(image_url=data_uri(str(kf))))
    (run,) = runpod.runs
    flow, images = run["input"]["workflow"], run["input"]["images"]
    assert flow["6"]["inputs"]["text"] == f"p {vast_h3.FIRST_FRAME_NOTE}" and flow["6"]["inputs"]["seed"] == 1
    assert flow["6"]["inputs"]["img"] == images[0]["name"] and images[0]["name"].startswith("hg_")
    assert flow["9"]["inputs"]["filename_prefix"].startswith("hellgrind/i2v_")
    assert run["policy"]["executionTimeout"] == settings.h3_timeout_s * 1000 and run["input"]["hellgrind"]["kind"] == "video"
    assert result.request_id == "job-1-e1" and result.meta["gpu"] == "NVIDIA GeForce RTX 5090"
    assert result.meta["gpu_seconds"] == 72.0                         # 30 sn çalışma + 42 sn soğuk başlatma (ilk iş)
    assert result.cost_usd == round(72.0 * RATE_5090, 4)
    (row,) = rows()
    assert (row["status"], row["backend"], row["instance_id"], row["server"]) == ("stored", "runpod", "runpod:ep123",
                                                                                   "runpod://ep123")
    assert row["sha256"] == hashlib.sha256(runpod.video).hexdigest() and storage.sha256_of(row["storage_key"]) == row["sha256"]
    meta = json.loads(storage.read_bytes(row["meta_key"]))
    assert meta["remote"]["gpu"] == "NVIDIA GeForce RTX 5090" and meta["provenance"]["server_location"] == "CA-MTL-1"
    assert result.meta["provenance"]["unet_variant"] == "fp8_scaled" and run["input"]["hellgrind"]["kind"] == "video"
    assert "data" not in json.dumps(meta["remote"]["files"])        # çıktı verisi meta veriye yazılmaz


def test_the_unet_variant_setting_reaches_the_workflow(runpod, monkeypatch):
    (pathlib.Path(settings.h3_comfy_workflow_dir) / "t2v.json").write_text(json.dumps({
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
        "7": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_h3_fl2va_pruned_{{unet_variant}}.safetensors"}},
        "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}))
    provider = runpod_h3.get_live_provider()
    provider.generate(request())
    monkeypatch.setattr(settings, "h3_unet_variant", "int8_convrot")
    result = provider.generate(request())                                  # tür işin özetinde: yeni iş gönderilir
    names = [r["input"]["workflow"]["7"]["inputs"]["unet_name"] for r in runpod.runs]
    assert names == ["minimax_h3_fl2va_pruned_fp8_scaled.safetensors", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"]
    assert result.meta["provenance"]["unet_variant"] == "int8_convrot"


def test_a_warm_worker_charges_no_cold_start(runpod):
    provider = runpod_h3.get_live_provider()
    provider.generate(request())
    second = provider.generate(request(seed=2))
    assert second.meta["gpu_seconds"] == 30.0 and second.cost_usd == round(30.0 * RATE_5090, 4)


def test_a_pro_6000_job_is_charged_at_its_own_rate(runpod):
    runpod.gpu, runpod.cold_start_s = "NVIDIA RTX PRO 6000 Blackwell Server Edition", 0
    assert runpod_h3.get_live_provider().generate(request()).cost_usd == round(30.0 * 0.000969, 4)


@pytest.mark.parametrize("mode", ["url", "official"])
def test_large_outputs_come_by_url_and_the_official_worker_format_is_read(runpod, mode):
    runpod.mode = mode
    runpod_h3.get_live_provider().generate(request())
    (row,) = rows()
    assert row["status"] == "stored" and row["size_bytes"] == len(runpod.video)
    assert any(r.url.host == "bucket.test" for r in runpod.requests) is (mode == "url")


def test_a_corrupted_output_is_never_stored_and_a_retry_reuses_the_job(runpod):
    runpod.tamper = True
    provider = runpod_h3.get_live_provider()
    with pytest.raises(TransientError, match="SHA-256"):
        provider.generate(request())
    assert [r["status"] for r in rows()] == ["completed"]
    runpod.tamper = False
    runpod.jobs["job-1-e1"].pop("output")
    provider.generate(request())
    assert [r["status"] for r in rows()] == ["stored"] and len(runpod.runs) == 1


def test_a_failed_job_is_recorded_and_retried_with_a_new_submission(runpod):
    runpod.failing = 1
    provider = runpod_h3.get_live_provider()
    with pytest.raises(runpod_h3.JobFailed, match="CUDA out of memory"):
        provider.generate(request())
    provider.generate(request())
    assert [r["status"] for r in rows()] == ["failed", "stored"] and len(runpod.runs) == 2


def test_storyboard_keyframes_run_on_the_same_endpoint(runpod):
    from app.engine import storyboard
    assert storyboard.board_source() == "comfyui"
    result = runpod_h3.keyframe_image("a bridge", 7, None)
    assert runpod.runs[-1]["input"]["hellgrind"]["kind"] == "keyframe" and result.local_path.endswith(".png")
    assert rows()[-1]["kind"] == "keyframe" and rows()[-1]["status"] == "stored"


def test_the_provider_routes_h3_to_runpod_by_default(runpod):
    from app.engine.providers import get_provider
    assert isinstance(get_provider("h3"), runpod_h3.RunpodProvider)


def test_a_whole_wave_is_submitted_together(monkeypatch):
    """Dalganın bütün işleri aynı anda gönderilir (gen_concurrency 3 olsa da): engel 5 iş birlikte gelmeden açılmaz."""
    from app.engine import iteration
    barrier = threading.Barrier(5, timeout=5)
    monkeypatch.setattr(settings, "gen_concurrency", 3)
    monkeypatch.setattr(iteration, "run_one", lambda c, tier, wave, i, res: barrier.wait())
    assert len(iteration.run_wave(SimpleNamespace(provider=SimpleNamespace(whole_wave=True)), "probe", 0, 5, "480p")) == 5


# ---------- korumalar: bölge ve bakiye ----------

@pytest.mark.parametrize("dcs,match", [(["US-TX-3"], "izinli listede olmayan"), ([], "kısıtı yok"),
                                       (["CA-MTL-1", "EUR-NO-1"], "EUR-NO-1")])
def test_an_endpoint_outside_the_allowlist_never_starts(runpod, dcs, match):
    runpod.config["dataCenterIds"] = dcs
    with pytest.raises(PaidCallBlocked, match=match):
        runpod_h3.get_live_provider()
    assert runpod.runs == []


def test_nordic_datacenters_are_opt_in(runpod, monkeypatch):
    runpod.config["dataCenterIds"] = ["EUR-NO-1", "EUR-IS-2"]
    monkeypatch.setattr(settings, "runpod_datacenter", "EUR-IS-2")
    with pytest.raises(PaidCallBlocked, match="Kuzey"):
        runpod_h3.get_live_provider()
    monkeypatch.setattr(settings, "h3_allow_nordic", True)
    assert isinstance(runpod_h3.get_live_provider(), runpod_h3.RunpodProvider)


def test_an_unreadable_endpoint_config_blocks_production(runpod, monkeypatch):
    real = runpod.handle
    monkeypatch.setattr(runpod, "handle", lambda req: __import__("httpx").Response(500) if "/serverless/" in req.url.path
                        else real(req))
    monkeypatch.setattr(runpod_h3, "TRANSPORT", runpod.transport())
    with pytest.raises(PaidCallBlocked, match="okunamadı"):
        runpod_h3.get_live_provider()


def test_a_low_balance_stops_new_jobs_and_warns(runpod, mk_project):
    runpod.balance = 4.2
    vast_box._r().set(vast_box._k("last_project"), mk_project(10.0))
    with pytest.raises(PaidCallBlocked, match="bakiyesi düşük \\(4.20 \\$ < eşik 10.00"):
        runpod_h3.get_live_provider()
    assert runpod.runs == []


def test_an_unreadable_balance_warns_but_does_not_stop(runpod):
    runpod.graphql_error = True
    assert isinstance(runpod_h3.get_live_provider(), runpod_h3.RunpodProvider)
    assert runpod_h3._cache_get("balance_error", 60)["error"]


# ---------- uç nokta ayarı ----------

def test_applying_settings_writes_gpu_pools_datacenter_volume_flex_and_idle_timeout(runpod, monkeypatch):
    monkeypatch.setattr(settings, "runpod_idle_timeout_s", 90)
    cfg = runpod_h3.apply_config()
    (body,) = runpod.patches
    assert body == {"gpu": {"pools": ["ADA_32_PRO", "BLACKWELL_96"]}, "workers": {"min": 0, "idleTimeout": 90},
                    "dataCenterIds": ["CA-MTL-1"], "flashboot": "FLASHBOOT", "networkVolumes": ["vol1"]}
    assert cfg["idle_timeout"] == 90 and cfg["datacenters"] == ["CA-MTL-1"]
    monkeypatch.setattr(settings, "runpod_datacenter", "EUR-NO-1")
    with pytest.raises(PaidCallBlocked):
        runpod_h3.apply_config()
    monkeypatch.setattr(settings, "runpod_datacenter", "CA-MTL-1")
    monkeypatch.setattr(settings, "runpod_gpu_types", "RTX 4090")
    with pytest.raises(PaidCallBlocked, match="Bilinmeyen GPU tipi"):
        runpod_h3.apply_config()


# ---------- dakikalık görev ----------

def test_the_tick_charges_idle_workers_and_retrieves_left_behind_outputs(runpod, mk_project, monkeypatch):
    pid = mk_project(10.0)
    provider = runpod_h3.get_live_provider()
    real = runpod_h3.collect
    monkeypatch.setattr(runpod_h3, "collect", lambda *a, **k: (_ for _ in ()).throw(TransientError("worker çöktü")))
    with pytest.raises(TransientError):
        provider.generate(request())
    monkeypatch.setattr(runpod_h3, "collect", real)
    with session_scope() as s:
        s.execute(text("UPDATE h3_outputs SET remote = jsonb_set(remote, '{completed_at}', to_jsonb(0))"))
    r = vast_box._r()
    r.set(vast_box._k("last_project"), pid)
    r.set(vast_box._k("rp_last_tick"), time.time() - 60)
    runpod.health["workers"]["idle"] = 2
    from app.worker.tasks import runpod_tick
    assert runpod_tick().startswith("idle 2 worker")
    assert [x["status"] for x in rows()] == ["stored"]
    with session_scope() as s:
        spent = s.execute(text("SELECT spent_usd FROM projects WHERE id = :p"), {"p": pid}).scalar()
    assert float(spent) == pytest.approx(2 * 60 * RATE_5090, abs=0.002)      # son işi çalıştıran GPU'nun ücreti, ~60 sn


def test_an_unknown_job_is_closed_as_lost_and_not_swept_again(runpod):
    """RunPod işi tanımıyorsa (404: kuyruk boşaltıldı, iptal ya da sonuç silindi) kayıt `lost` olur; süpürme bitip durur."""
    h3_outputs.submitted("runpod://ep123", "job-old", "video", "fl2va", None, {}, backend="runpod", instance_id="runpod:ep123")
    runpod.expired.add("job-old")
    counts = h3_outputs.retrieve("runpod:ep123", "", "")
    assert counts["missing"] == 1 and counts["remaining"] == 0
    assert rows()[0]["status"] == "lost" and "404" in rows()[0]["error"] and "30 dk" in rows()[0]["error"]


def test_nothing_runs_in_free_mode(runpod, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    from app.worker.tasks import runpod_tick
    assert runpod_tick() == "skip" and runpod.requests == []
    with pytest.raises(PaidCallBlocked):
        runpod_h3.get_live_provider()


# ---------- fiyat ----------

def test_runpod_prices_by_gpu_type_and_reserves_at_the_primary_gpu():
    prices = pricing.table()
    assert pricing.runpod_rate("NVIDIA GeForce RTX 5090", prices) == RATE_5090
    assert pricing.runpod_rate("NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition", prices) == 0.000969
    assert pricing.runpod_rate("NVIDIA H100", prices) == 0.000969                    # bilinmeyen tip: en pahalısı
    assert pricing.runpod_rate(None, prices) == RATE_5090                            # GPU bilinmiyor: birincil tip
    assert pricing.estimate_video("h3", "720p", 5.0, prices) == round(22 * 5 * RATE_5090, 4)
    assert pricing.estimate_video("h3", "720p", 5.0, prices, mode="ref2va") == round(14 * 5 * RATE_5090, 4)


def test_the_pricing_table_validates_the_serverless_section():
    raw = pathlib.Path(settings.pricing_file).read_text()
    assert pricing.validate_table(raw)["serverless"]["runpod"]["per_second"]["RTX 5090"] == RATE_5090
    with pytest.raises(pricing.PricingError, match="serverless.runpod.per_second"):
        pricing.validate_table(raw.replace('"RTX 5090": 0.000439', '"RTX 5090": 0'))


# ---------- panel ----------

def test_the_panel_shows_state_and_runs_refresh_and_apply_only_in_paid_mode(api, runpod, monkeypatch):
    from app import runtime_config as rc
    monkeypatch.setattr(rc, "VERSION_KEY", "hg:test:config:version")
    monkeypatch.setattr(rc, "AUDIT_KEY", "hg:test:config:audit")
    state = api.get("/api/settings/runpod").json()
    assert state["config"] is None and state["allowed_datacenters"] == ["CA-MTL-1", "CA-MTL-2", "CA-MTL-3"]
    assert state["desired"]["gpu"]["pools"] == ["ADA_32_PRO", "BLACKWELL_96"] and state["region_problem"] is None
    state = api.post("/api/settings/runpod", json={"action": "refresh"}).json()
    assert state["balance"]["usd"] == 50.0 and state["config"]["datacenters"] == ["CA-MTL-1"]
    assert state["health"]["workers"] == {"idle": 0, "running": 0}
    assert api.post("/api/settings/runpod", json={"action": "apply"}).status_code == 200 and runpod.patches
    monkeypatch.setattr(settings, "dry_run", True)
    assert api.post("/api/settings/runpod", json={"action": "refresh"}).status_code == 409


def test_the_probe_reads_balance_region_and_workers_without_starting_any(runpod):
    from app.live import probe
    r = probe.check("runpod", "test-runpod_api_key")
    assert r["ok"] is True and "Bakiye 50,00 $" in r["message"] and "CA-MTL-1" in r["message"]
    assert "0 boşta" in r["message"] and runpod.runs == []
    runpod.config["dataCenterIds"] = ["US-GA-1"]
    assert probe.check("runpod", "test-runpod_api_key")["ok"] is False


# ---------- ilk gerçek testlerden ----------

MEASURED = {                     # 2026-10-08, RTX 5090, fp8_scaled, turbo, 480p, 4 sn klip: faturalanan tutarlar
    ("fl2va", False): (0.0179, 0.0192), ("ref2va", False): (0.0116, 0.0118),
    ("fl2va", True): (0.0791, 0.0791), ("ref2va", True): (0.0743, 0.0743)}


@pytest.mark.parametrize("mode,cold", list(MEASURED))
def test_estimates_are_within_25_percent_of_the_measured_cost(runpod, mode, cold):
    low, high = MEASURED[(mode, cold)]
    estimate = pricing.estimate_video("h3", "480p", 4, mode=mode, cold=cold)
    assert 0.75 * low <= estimate <= 1.25 * high, (estimate, low, high)


def test_keyframe_estimates_match_warm_and_cold_measurements(runpod):
    warm, cold = pricing.estimate_keyframe("h3"), pricing.estimate_keyframe("h3", cold=True)
    assert 0.75 * 0.0169 <= warm <= 1.25 * 0.0182 and 0.75 * 0.0606 <= cold <= 1.25 * 0.0609


def test_pool_ids_map_to_gpu_rates():
    assert pricing.runpod_rate("ADA_32_PRO") == RATE_5090 and pricing.runpod_rate("BLACKWELL_96") == 0.000969


def test_only_one_job_of_a_wave_reserves_the_cold_start_and_none_after_a_job(runpod):
    vast_box._r().delete(vast_box._k("rp_cold_claim"), vast_box._k("rp_warm"))
    assert runpod_h3.cold_start_due() is True and runpod_h3.cold_start_due() is False
    vast_box._r().delete(vast_box._k("rp_cold_claim"))
    runpod_h3.get_live_provider().generate(request())                      # biten iş worker'ı sıcak bırakır
    vast_box._r().delete(vast_box._k("rp_cold_claim"))
    assert runpod_h3.cold_start_due() is False


def test_the_cold_start_is_reported_in_the_event_feed(runpod, mk_project):
    pid = mk_project(10.0)
    runpod_h3.get_live_provider().generate(request(extra={"project_id": pid}))
    from app.budget import killswitch
    events = [e for e in killswitch.events(pid) if e["kind"] == "runpod_cold_start"] if hasattr(killswitch, "events") else None
    raw = [__import__("json").loads(x) for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    cold = [e for e in (events or raw) if e["kind"] == "runpod_cold_start"]
    assert cold and cold[0]["seconds"] == 42.0 and cold[0]["model"] == "H3 FL2VA"


def test_idle_workers_are_not_charged_while_a_job_runs(runpod, mk_project):
    pid = mk_project(10.0)
    r = vast_box._r()
    r.set(vast_box._k("last_project"), pid)
    r.set(vast_box._k("rp_last_tick"), time.time() - 60)
    runpod.health["workers"]["idle"] = 1
    vast_box.track("job-key", pid)                                          # soğuk başlatma sürüyor: işin bedelinde
    runpod_h3.tick()
    with session_scope() as s:
        assert float(s.execute(text("SELECT spent_usd FROM projects WHERE id = :p"), {"p": pid}).scalar()) == 0.0
    vast_box.untrack("job-key")
    r.set(vast_box._k("rp_last_tick"), time.time() - 60)
    r.set(vast_box._k("rp_last_gpu"), "NVIDIA GeForce RTX 5090")
    runpod_h3.tick()
    with session_scope() as s:
        spent = float(s.execute(text("SELECT spent_usd FROM projects WHERE id = :p"), {"p": pid}).scalar())
        idle_row = s.execute(text("SELECT details FROM service_calls WHERE project_id = :p AND label = 'h3:idle'"), {"p": pid}).scalar()
    assert spent == pytest.approx(60 * RATE_5090, abs=0.002)               # son işi çalıştıran GPU'nun ücreti
    assert idle_row["idle_seconds"] >= 59 and idle_row["gpu"] == "NVIDIA GeForce RTX 5090"


def test_history_shows_the_real_idle_seconds(api, runpod, mk_project):
    pid = mk_project(10.0)
    from app.budget import killswitch
    with killswitch.spend(pid, 0.01, "h3:idle", details={"idle_seconds": 61.5, "idle_workers": 1}) as ch:
        ch.actual = 0.01
    rows = api.get("/api/history", params={"project_id": pid}).json()["items"]
    assert [x["duration_s"] for x in rows if x["label"] == "h3:idle"] == [61.5]


def test_a_ref2va_shot_never_sends_a_keyframe_job(runpod, sample_scene, monkeypatch):
    """Ref2VA ilk kareyi kullanmaz: storyboard için yerel (ücretsiz) kare çizilir; RunPod'a keyframe işi gitmez. Ref2VA
    çekim bazında elle seçilir."""
    from app.engine import storyboard
    from app.engine.iteration import keyframe_blocks
    from app.models import Asset, Character, Scene
    pid, codes = sample_scene()
    sid = codes["S002_SH01"]
    with session_scope() as s:
        s.get(Scene, sid).h3_mode = "ref2va"
        for ch in s.query(Character).filter(Character.project_id == pid):
            ch.reference_images = [f"assets/{ch.id}/still/a.png"]               # karakterin referansı...
            if ch.asset_id:
                s.get(Asset, ch.asset_id).status = "locked"                     # ...ve onaylı varlığı
    assert keyframe_blocks(pid, sid) is False                                  # görüntü öncesi kare istenmez
    storyboard.make_board(pid, sid)
    keyframes = [r for r in runpod.runs if r["input"]["hellgrind"]["kind"] == "keyframe"]
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert keyframes == [] and sc.board_source == "local" and sc.board_key


def test_an_fl2va_board_is_drawn_at_most_once_per_render(runpod, sample_scene, monkeypatch):
    """Kalite kapısını geçemeyen ücretli kare yeniden çizilmez (eskiden 3 deneme = 3 ayrı ücretli kare)."""
    from app.engine import storyboard
    from app.quality import gate
    monkeypatch.setattr(gate, "score_frame", lambda *a, **k: gate.Score(clip=0.81, face=None, composite=0.81,
                                                                       anomalies=[{"type": "low_clip"}], passed=False))
    pid, codes = sample_scene()
    storyboard.make_board(pid, codes["S001_SH01"])
    assert [r["input"]["hellgrind"]["kind"] for r in runpod.runs] == ["keyframe"]


def test_fl2va_gets_the_full_size_clean_frame_not_the_board(runpod, sample_scene, monkeypatch, tmp_path):
    """Storyboard karesi 960x540 (yazılı bant), keyframe.json 1280x704 üretir: FL2VA'ya giden ilk kare tam boy yazısız
    kare olmalı ve H3 tuvaliyle aynı en-boy oranında."""
    import io
    from PIL import Image
    from app import storage
    from app.engine import storyboard
    from app.engine.iteration import keyframe_of
    from app.models import Scene
    from app.quality import gate
    buf = io.BytesIO()
    Image.new("RGB", (1280, 704), (30, 60, 90)).save(buf, "PNG")
    runpod.image = buf.getvalue()
    monkeypatch.setattr(gate, "score_frame", lambda *a, **k: gate.Score(clip=0.95, face=None, composite=0.95, anomalies=[], passed=True))
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    storyboard.make_board(pid, sid)
    with session_scope() as s:
        sc = s.get(Scene, sid)
        from app.core.hashing import visual_fingerprint
        from app.core.state import load_assets, load_characters
        fp = visual_fingerprint(sc, load_characters(s, sid), load_assets(s, sid), None)
        key = keyframe_of(sc, fp)
        board_key = sc.board_key
    assert key == storyboard.clean_key(board_key)
    clean = Image.open(io.BytesIO(storage.read_bytes(key)))
    board = Image.open(io.BytesIO(storage.read_bytes(board_key)))
    assert clean.size == (1280, 704) and board.size == (960, 540)
    from app.live import vast_h3
    assert abs(clean.size[0] / clean.size[1] - vast_h3.SIZES["720p"][0] / vast_h3.SIZES["720p"][1]) < 0.01
