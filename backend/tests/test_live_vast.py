"""ÜCRETLİ MiniMax H3 modülleri (vast.ai makinesi; SGLang ya da ComfyUI arka ucu; makine yönetimi): sahte sunucularla.
Hiçbir test gerçek bir sunucuya ya da vast.ai'ye istek göndermez; `h3` fixture'ı kilidi yalnızca bellekte açar ve httpx'e
sahte taşıyıcı bağlar.
"""
import base64
import io
import json
import time

import httpx
import pytest
from PIL import Image

from app.budget import history, pricing
from app.config import settings
from app.engine.providers import GenRequest, LocalProvider, TransientError, get_provider, route_engine
from app.live import PaidCallBlocked, probe, vast_api, vast_box, vast_h3
from app.live.vast_h3 import H3Provider, JobFailed, build_job, frames_for, get_live_provider, seconds_for, sglang_body
from tests.h3_fake import FakeComfy, FakeVast
from tests.media_utils import make_video
from tests.media_utils import probe as media_probe


def request(extra=None, **kw):
    base = dict(prompt="Kerem köprüde koşuyor", seed=4321, resolution="720p", duration_s=4.0, negative="")
    return GenRequest(**{**base, **kw}, extra=extra or {})


def png(tmp_path, size=(2400, 1200), name="kare.png"):
    path = tmp_path / name
    Image.new("RGB", size, (200, 120, 40)).save(path)
    return str(path)


def tags(path) -> dict:
    return media_probe(path)["format"].get("tags", {})


# ---------- sağlayıcı seçimi ve kilit ----------

def test_new_shots_go_to_h3_and_only_a_chosen_seedance_is_kept():
    from types import SimpleNamespace as NS
    assert route_engine(NS(engine=None, action_complexity=0.95)) == "h3"
    assert route_engine(NS(engine="fal_wan", action_complexity=0.1)) == "h3"
    assert route_engine(NS(engine="seedance", action_complexity=0.1)) == "seedance"


def test_free_mode_never_reaches_the_h3_module(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    assert isinstance(get_provider("h3"), LocalProvider)
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        get_live_provider()


def test_the_lock_is_the_vast_key_and_a_server_address_is_required(paid, monkeypatch):
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "vast_api_key", "")
    with pytest.raises(PaidCallBlocked, match="VAST_API_KEY"):
        get_live_provider()
    monkeypatch.setattr(settings, "vast_api_key", "anahtar-1234")
    monkeypatch.setattr(settings, "h3_endpoint_url", "")
    with pytest.raises(PaidCallBlocked, match="sunucu adresi tanımlı değil"):
        get_live_provider()


def test_paid_mode_hands_back_the_h3_provider(h3):
    provider = get_provider("h3")
    assert isinstance(provider, H3Provider) and provider.key_name == "vast_api_key" and provider.image_first is False


# ---------- iş ----------

@pytest.mark.parametrize("asked,sent", [(0.5, 4), (3.0, 4), (4.0, 4), (4.2, 5), (7.5, 8), (15.0, 15), (40.0, 15)])
def test_duration_is_clamped_to_the_models_4_to_15_seconds(asked, sent):
    assert seconds_for(asked) == sent


@pytest.mark.parametrize("seconds,frames", [(4, 107), (5, 124), (6, 158), (15, 362)])
def test_frames_follow_the_17k_plus_5_grid(seconds, frames):
    assert frames_for(seconds) == frames and (frames - 5) % 17 == 0 and frames >= 24 * seconds


def test_a_text_only_job_is_t2va_on_the_calibrated_canvas(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_steps", "8")                              # panelden metin olarak gelebilir
    job = build_job(request(duration_s=3.0))
    body = sglang_body(job)
    assert (job.task, job.url, job.width, job.height, job.seconds, job.steps) == ("t2va", "https://h3.test", 1280, 704, 4, 8)
    assert body["target"] == {"short_edge": 704, "aspect_ratio": "16:9", "duration_seconds": 4.0} and body["conditions"] == []
    assert (body["seed"], body["num_inference_steps"], body["flow_shift"], body["audio_flow_shift"]) == (4321, 8, 12.0, 3.0)
    assert (build_job(request(resolution="480p")).width, build_job(request(resolution="480p")).height) == (864, 480)


def test_negatives_arrive_as_positive_constraints_and_never_as_a_negative_prompt(h3):
    job = build_job(request(negative="blurry, watermark, extra people, crowd, lens flare"))
    body = sglang_body(job)
    assert "negative_prompt" not in body and "blurry" not in body["prompt"] and "watermark" not in body["prompt"]
    assert body["prompt"].endswith("Visual constraints: only the listed characters are present and the surroundings are "
                                   "otherwise unpopulated; sharp focus with crisp, clean contours; a clean, unmarked image area.")
    assert job.unmapped == ("lens flare",)


def test_a_first_frame_makes_it_fl2va_with_the_frame_as_keyframe(h3, tmp_path):
    uri = H3Provider().upload(png(tmp_path))
    body = sglang_body(build_job(request(image_url=uri)))
    assert body["task"] == "fl2va" and body["conditions"] == [{"type": "image", "uri": uri, "role": "keyframe", "frame_index": 0}]


def test_uploaded_frames_are_downscaled_jpeg_data_uris(h3, tmp_path):
    uri = H3Provider().upload(png(tmp_path, size=(3000, 1500)))
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as im:
        assert max(im.size) == vast_h3.IMAGE_EDGE and im.format == "JPEG"


REFS = [{"name": "Kerem", "key": "assets/k/still/a.png"}, {"name": "Kerem", "key": "assets/k/frames/a_00.jpg"},
        {"name": "Ayşe", "key": "assets/a/still/b.png"}]


def test_ref2va_is_used_only_when_the_director_chose_it(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test/")
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: f"data:image/jpeg;base64,{key}")
    assert build_job(request({"reference_set": REFS})).task == "t2va"                 # seçim yoksa referans gitmez
    job = build_job(request({"reference_set": REFS, "h3_mode": "ref2va"}, image_url="data:image/jpeg;base64,ilk"))
    body = sglang_body(job)
    assert job.task == "ref2va" and job.url == "https://h3-ref.test" and job.names == ["Kerem", "Kerem", "Ayşe"]
    assert [(c["role"], c["uri"].split(",")[1]) for c in body["conditions"]] == [
        ("reference", "assets/k/still/a.png"), ("reference", "assets/k/frames/a_00.jpg"), ("reference", "assets/a/still/b.png"),
        ("keyframe", "ilk")]
    assert body["prompt"].startswith("Character references: <Picture 1>, <Picture 2> show Kerem; <Picture 3> shows Ayşe.")


def test_ref2va_needs_references_and_on_sglang_its_own_server(h3, monkeypatch):
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: "data:image/jpeg;base64,eA==")
    with pytest.raises(PaidCallBlocked, match="Ref2VA sunucusu tanımlı değil"):
        build_job(request({"reference_set": REFS, "h3_mode": "ref2va"}))
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    with pytest.raises(PaidCallBlocked, match="referans görseli yok"):
        build_job(request({"reference_set": [], "h3_mode": "ref2va"}))
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "")
    monkeypatch.setattr(settings, "h3_backend", "comfyui")                       # ComfyUI: aynı sunucu, r2v iş akışı
    assert build_job(request({"reference_set": REFS, "h3_mode": "ref2va"})).url == "https://h3.test"


def test_at_most_nine_references_are_sent(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: "data:image/jpeg;base64,eA==")
    many = [{"name": f"K{i}", "key": f"assets/{i}/still/x.png"} for i in range(12)]
    assert len(build_job(request({"reference_set": many, "h3_mode": "ref2va"})).references) == vast_h3.MAX_REFERENCES


# ---------- SGLang ----------

def test_generate_submits_polls_downloads_and_charges_the_reported_gpu_time(h3):
    with history.recording() as call:
        result = H3Provider().generate(request(extra={"project_id": "proje-1"}))
    assert len(h3.submits) == 1 and result.request_id == "video_1" and result.engine == "h3"
    assert all(r.headers["Authorization"] == "Bearer test-h3_api_key" for r in h3.requests)    # portal belirteci
    assert result.cost_usd == pricing.estimate_gpu("h3", 42.0) and result.meta["gpu_seconds"] == 42.0
    assert [s["model"] for s in call.steps] == [settings.h3_model] * 2 and call.steps[0]["task"] == "t2va"
    assert call.steps[0]["backend"] == "sglang" and call.steps[1]["gpu_seconds"] == 42.0
    assert vast_box.inflight() == 0 and vast_box.last_project() == "proje-1"


def test_every_clip_carries_its_provenance_in_the_record_and_the_mp4(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_model_revision", "fl2va int8 convrot + turbo v4")
    result = H3Provider().generate(request())
    prov = result.meta["provenance"]
    assert prov == {"model": "MiniMaxAI/MiniMax-H3", "model_revision": "fl2va int8 convrot + turbo v4", "backend": "sglang",
                    "task": "t2va", "steps": int(settings.h3_steps), "server": "h3.test", "server_location": None,
                    "vast_instance_id": None, "license": "MiniMax H3 Community License", "training_allowed": False}
    embedded = tags(result.local_path)
    assert json.loads(embedded["comment"]) == prov and "section V.3" in embedded["copyright"]


def test_without_a_portal_token_no_authorization_header_is_sent(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_api_key", "")                               # SSH tüneli
    H3Provider().generate(request())
    assert all("Authorization" not in r.headers for r in h3.requests)


def test_without_a_reported_gpu_time_the_estimate_is_charged(h3):
    h3.inference_time_s = None
    provider = H3Provider()
    assert provider.generate(request(resolution="480p")).cost_usd == provider.estimate(request(resolution="480p"))


def test_the_estimate_follows_the_calibrated_prices():
    hosted = pricing.self_hosted("h3")
    assert (hosted["gpu_hourly_usd"], hosted["render_seconds"]) == (0.95, {"480p": 8, "720p": 16.5})
    expected = round(0.95 / 3600 * 8 * 4, 4)
    assert H3Provider().estimate(request(resolution="480p", duration_s=3.0)) == expected     # 3 sn çekim 4 sn üretilir
    assert LocalProvider("h3").estimate(request(resolution="480p", duration_s=3.0)) == expected  # ücretsiz modda da


def test_a_shot_shorter_than_four_seconds_is_cut_to_its_length(h3):
    result = H3Provider().generate(request(duration_s=3.0))
    assert media_probe(result.local_path)["duration"] == pytest.approx(3.0, abs=0.1)
    assert "comment" in tags(result.local_path)                                   # kesilen klipte de kaynak bilgisi


def test_overloaded_servers_are_transient(h3):
    h3.submit_errors = [503]
    with pytest.raises(TransientError):
        H3Provider().generate(request())


def test_a_rejected_token_stops_with_a_turkish_explanation(h3):
    h3.submit_errors = [401]
    with pytest.raises(PaidCallBlocked, match="erişimi reddetti"):
        H3Provider().generate(request())


def test_an_unreachable_server_stops_instead_of_retrying(h3, monkeypatch, tmp_path):
    def refuse(request):
        raise httpx.ConnectError("bağlantı reddedildi", request=request)
    monkeypatch.setattr(vast_h3, "TRANSPORT", httpx.MockTransport(refuse))
    with pytest.raises(PaidCallBlocked, match="makine çalışıyor mu"):
        H3Provider().generate(request())
    (tmp_path / "comfy").mkdir(exist_ok=True)
    (tmp_path / "comfy" / "t2v.json").write_text(json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}}}))
    monkeypatch.setattr(settings, "h3_backend", "comfyui")
    with pytest.raises(PaidCallBlocked, match="SSH tüneli"):
        H3Provider().generate(request())


def test_an_invalid_request_is_not_retried_and_shows_the_servers_reason(h3):
    h3.submit_errors = [422]
    with pytest.raises(RuntimeError, match="HTTP 422.*istek reddedildi") as e:
        H3Provider().generate(request())
    assert not isinstance(e.value, (TransientError, PaidCallBlocked))


def test_a_failed_job_is_retried_with_a_new_submission(h3):
    h3.failing_jobs = 1
    provider = H3Provider()
    with pytest.raises(JobFailed, match="CUDA out of memory"):
        provider.generate(request())
    assert provider.generate(request()).request_id == "video_2" and len(h3.submits) == 2


def test_a_lost_download_does_not_run_the_job_on_the_gpu_again(h3):
    h3.content_errors = [503, 503, 503]
    provider = H3Provider()
    with pytest.raises(TransientError, match="indirilemedi"):
        provider.generate(request())
    with history.recording() as call:
        result = provider.generate(request())
    assert len(h3.submits) == 1 and result.request_id == "video_1" and call.steps[0]["reused"] is True


def test_a_job_that_outlives_the_timeout_is_awaited_again_not_resubmitted(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_timeout_s", 0)
    h3.never_finish = True
    provider = H3Provider()
    with pytest.raises(TransientError, match="bitmedi"):
        provider.generate(request())
    h3.never_finish = False
    assert provider.generate(request()).request_id == "video_1" and len(h3.submits) == 1


# ---------- ComfyUI ----------

I2V = {"3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}", "steps": "{{steps}}"}},
       "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
       "8": {"class_type": "LoadImage", "inputs": {"image": "{{first_frame}}"}},
       "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"width": "{{width}}", "height": "{{height}}",
                                                               "length": "{{frames}}", "fps": "{{fps}}"}},
       "9": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}


@pytest.fixture
def comfy(h3, monkeypatch, tmp_path):
    video = make_video(tmp_path / "comfy.mp4", seconds=4.0, size="320x180", fps=24)
    still = tmp_path / "still.png"
    Image.new("RGB", (320, 180), (30, 140, 200)).save(still)
    fake = FakeComfy(open(video, "rb").read(), still.read_bytes())
    monkeypatch.setattr(vast_h3, "TRANSPORT", fake.transport())
    monkeypatch.setattr(settings, "h3_backend", "comfyui")
    monkeypatch.setattr(settings, "h3_endpoint_url", "http://host.docker.internal:8188")
    vast_h3.Comfy._uploaded.clear()
    flows = tmp_path / "comfy"
    flows.mkdir(exist_ok=True)
    (flows / "i2v.json").write_text(json.dumps(I2V))
    (flows / "t2v.json").write_text(json.dumps({k: v for k, v in I2V.items() if k != "8"}))
    r2v = {**I2V, "8": {"class_type": "LoadImage", "inputs": {"image": "{{reference_1}}"}},
           "10": {"class_type": "LoadImage", "inputs": {"image": "{{reference_2}}"}}}
    (flows / "r2v.json").write_text(json.dumps(r2v))
    (flows / "keyframe.json").write_text(json.dumps({"6": I2V["6"], "3": I2V["3"],
                                                     "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "kf"}}}))
    return fake


def test_a_workflow_must_be_an_api_export_with_its_placeholders():
    with pytest.raises(ValueError, match="JSON okunamadı"):
        vast_h3.validate_workflow("i2v", "{nope")
    with pytest.raises(ValueError, match="API biçimi değil"):
        vast_h3.validate_workflow("i2v", json.dumps({"nodes": [], "links": []}))        # arayüz biçimi
    with pytest.raises(ValueError, match="first_frame"):
        vast_h3.validate_workflow("i2v", json.dumps({k: v for k, v in I2V.items() if k != "8"}))
    assert vast_h3.placeholders(vast_h3.validate_workflow("i2v", json.dumps(I2V))) >= {"prompt", "seed", "first_frame"}


def test_whole_placeholders_keep_their_type_and_embedded_ones_become_text():
    filled = vast_h3.fill({"a": "{{seed}}", "b": "shot {{seed}} of {{prompt}}", "c": ["{{width}}"], "d": "{{unknown}}"},
                          {"seed": 7, "prompt": "köprü", "width": 1280})
    assert filled == {"a": 7, "b": "shot 7 of köprü", "c": [1280], "d": "{{unknown}}"}


def test_comfyui_uploads_the_first_frame_fills_the_workflow_and_reads_the_gpu_time(comfy, tmp_path):
    uri = H3Provider().upload(png(tmp_path))
    with history.recording() as call:
        result = H3Provider().generate(request(image_url=uri, duration_s=5.0))
    (sent,) = comfy.prompts
    assert sent["8"]["inputs"]["image"] == comfy.uploads[0] and comfy.uploads[0].startswith("hg_")
    assert sent["3"]["inputs"] == {"seed": 4321, "steps": int(settings.h3_steps)}
    assert sent["5"]["inputs"] == {"width": 1280, "height": 704, "length": 124, "fps": 24}
    assert sent["6"]["inputs"]["text"] == "Kerem köprüde koşuyor"
    assert result.request_id == "p1" and result.meta["gpu_seconds"] == 30.0
    assert result.cost_usd == pricing.estimate_gpu("h3", 30.0) and result.meta["provenance"]["backend"] == "comfyui"
    assert call.steps[0]["backend"] == "comfyui" and call.steps[0]["keyframe"] is True
    assert media_probe(result.local_path)["video"]


def test_the_same_frame_is_uploaded_once(comfy, tmp_path):
    uri = H3Provider().upload(png(tmp_path))
    provider = H3Provider()
    provider.generate(request(image_url=uri, seed=1))
    provider.generate(request(image_url=uri, seed=2))
    assert len(comfy.uploads) == 1 and len(comfy.prompts) == 2


def test_comfyui_ref2va_fills_every_reference_slot(comfy, monkeypatch):
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: "data:image/jpeg;base64," + base64.b64encode(key.encode()).decode())
    H3Provider().generate(request({"reference_set": [{"name": "Kerem", "key": "assets/k/still/a.png"}], "h3_mode": "ref2va"}))
    (sent,) = comfy.prompts
    assert sent["8"]["inputs"]["image"] == sent["10"]["inputs"]["image"] == comfy.uploads[0]   # fazla yuva son görselle
    assert sent["6"]["inputs"]["text"].startswith("Character references: <Picture 1> shows Kerem.")


def test_comfyui_errors_are_explained(comfy):
    comfy.reject_next = True
    with pytest.raises(RuntimeError, match="ComfyUI iş akışını reddetti.*Value not in list"):
        H3Provider().generate(request())
    comfy.failing = 1
    with pytest.raises(JobFailed, match="Allocation on device failed"):
        H3Provider().generate(request())


def test_a_missing_workflow_blocks_with_a_turkish_message(comfy, tmp_path):
    (tmp_path / "comfy" / "t2v.json").unlink()
    with pytest.raises(PaidCallBlocked, match="t2v.json"):
        H3Provider().generate(request())


def test_a_storyboard_keyframe_can_be_drawn_on_the_h3_box(comfy):
    comfy.output_ext = ".png"
    result = vast_h3.keyframe_image("A rooftop at sunset", 99, "proje-1")
    with Image.open(result.local_path) as im:
        assert im.size == (320, 180)
    assert comfy.prompts[0]["3"]["inputs"]["seed"] == 99 and result.cost_usd == pricing.estimate_gpu("h3", 30.0)
    assert vast_h3.comfy_keyframes_ready()


# ---------- makine yönetimi ----------

@pytest.fixture
def box(h3, monkeypatch):
    fake = FakeVast()
    monkeypatch.setattr(vast_api, "TRANSPORT", fake.transport())
    monkeypatch.setattr(settings, "vast_instance_id", "4242")
    monkeypatch.setattr(vast_box, "START_POLL_S", 0)
    r = vast_box._r()
    for name in ("inflight", "last_activity", "last_project", "tick", "box", "starting"):
        r.delete(vast_box._k(name))
    return fake


def test_a_running_box_in_an_allowed_region_is_used_and_its_location_recorded(box):
    result = H3Provider().generate(request())
    assert result.meta["provenance"]["server_location"] == "Norway, NO"
    assert result.meta["provenance"]["vast_instance_id"] == "4242" and box.puts == []


def test_a_box_in_an_excluded_territory_is_refused_unless_authorized(box, monkeypatch):
    box.geolocation = "Virginia, US"
    with pytest.raises(PaidCallBlocked, match="lisansın dışladığı"):
        H3Provider().generate(request())
    monkeypatch.setattr(settings, "h3_territory_authorized", True)
    assert H3Provider().generate(request()).meta["provenance"]["server_location"] == "Virginia, US"


@pytest.mark.parametrize("geo,excluded", [("Norway, NO", False), ("Bavaria, DE", True), ("London, GB", True),
                                          ("Seoul, KR", True), ("Ontario, CA", False), ("Istanbul, TR", False), (None, False)])
def test_excluded_territories(geo, excluded):
    assert vast_api.Box("running", None, None, geo, None).excluded is excluded


def test_a_stopped_box_is_started_only_when_auto_start_is_on(box, monkeypatch):
    box.status = "stopped"
    with pytest.raises(PaidCallBlocked, match="makinesi çalışmıyor"):
        H3Provider().generate(request())
    monkeypatch.setattr(settings, "h3_auto_start", True)
    assert H3Provider().generate(request()).request_id == "video_1"
    assert box.puts == [{"state": "running"}]


def idle(seconds_idle: float, last_tick_ago: float, project=None):
    r, now = vast_box._r(), time.time()
    r.set(vast_box._k("last_activity"), now - seconds_idle)
    r.set(vast_box._k("tick"), now - last_tick_ago)
    if project:
        r.set(vast_box._k("last_project"), project)


def ledger(pid):
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:
        return [(p, k, float(a)) for p, k, a in s.execute(text(
            "SELECT provider, kind::text, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid})]


def test_idle_time_is_charged_to_the_last_project_and_the_box_stops_after_the_idle_limit(box, mk_project):
    pid = mk_project(10.0)
    idle(seconds_idle=120, last_tick_ago=60, project=pid)
    assert vast_box.idle_tick(queue_empty=lambda: True).startswith("idle")
    (commit,) = [x for x in ledger(pid) if x[1] == "commit"]
    assert commit[0] == "h3:idle" and commit[2] == pytest.approx(pricing.estimate_gpu("h3", 60), abs=0.0002)
    assert box.puts == []
    idle(seconds_idle=11 * 60, last_tick_ago=60, project=pid)
    assert vast_box.idle_tick(queue_empty=lambda: True) == "stopped" and box.puts == [{"state": "stopped"}]


def test_a_busy_queue_is_neither_charged_as_idle_nor_stopped(box, mk_project):
    pid = mk_project(10.0)
    idle(seconds_idle=60 * 60, last_tick_ago=60, project=pid)
    vast_box.track("is-1", pid)
    assert vast_box.idle_tick(queue_empty=lambda: True) == "busy" and ledger(pid) == [] and box.puts == []
    vast_box.untrack("is-1")
    idle(seconds_idle=60 * 60, last_tick_ago=60, project=pid)
    assert vast_box.idle_tick(queue_empty=lambda: False).startswith("idle") and box.puts == []   # sunucunun kendi işi var


def test_an_exhausted_budget_stops_the_box_at_once(box, mk_project):
    pid = mk_project(0.01)                                   # bir dakikalık boşta bekleme (~0,016 $) sığmaz
    idle(seconds_idle=120, last_tick_ago=60, project=pid)
    vast_box.idle_tick(queue_empty=lambda: True)
    assert box.puts == [{"state": "stopped"}] and not [x for x in ledger(pid) if x[1] == "commit"]


def test_idle_management_is_off_in_free_mode_and_without_an_instance(box, monkeypatch):
    monkeypatch.setattr(settings, "vast_instance_id", "")
    assert vast_box.idle_tick() == "skip"
    monkeypatch.setattr(settings, "vast_instance_id", "4242")
    monkeypatch.setattr(settings, "dry_run", True)
    assert vast_box.idle_tick() == "skip" and box.requests == []
    from app.worker.tasks import h3_idle
    assert h3_idle() == "skip"


def test_a_stopped_box_is_not_charged(box, mk_project):
    pid = mk_project(10.0)
    box.status = "stopped"
    idle(seconds_idle=600, last_tick_ago=60, project=pid)
    assert vast_box.idle_tick() == "makine stopped" and ledger(pid) == []


# ---------- bağlantı sınaması ----------

def routed(vast: FakeVast | None, server_status: int):
    def handle(request):
        if request.url.host == "console.vast.ai":
            return vast.handle(request)
        return httpx.Response(server_status, json={})
    return httpx.MockTransport(handle)


def test_the_probe_reports_the_box_and_the_server(box, monkeypatch):
    r = probe.check("vast", "test-vast_api_key", routed(box, 200))
    assert r["ok"] is True and r["message"] == "Makine: running · 0,87 $/sa · Norway, NO · RTX 5090 Sunucu hazır."
    box.geolocation = "Virginia, US"
    r = probe.check("vast", "test-vast_api_key", routed(box, 200))
    assert r["ok"] is False and "lisansının dışında" in r["message"]
    assert probe.check("vast", "yanlis-anahtar", routed(box, 200))["ok"] is False


@pytest.mark.parametrize("status,ok,text", [(200, None, "hazır"), (401, False, "reddetti"), (503, None, "açılıyor"),
                                            (530, False, "tüneli kapalı"), (418, None, "Beklenmeyen")])
def test_without_an_instance_id_only_the_server_is_checked(monkeypatch, status, ok, text):
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "")
    monkeypatch.setattr(settings, "vast_instance_id", "")
    monkeypatch.setattr(settings, "h3_backend", "sglang")
    seen = []

    def health(request):
        seen.append(str(request.url))
        return httpx.Response(status, json={})
    r = probe.check("vast", "anahtar-1234", httpx.MockTransport(health))
    assert r["ok"] is ok and text in r["message"] and "Makine kimliği tanımlı değil" in r["message"]
    assert seen == ["https://h3.test/health"]


def test_the_comfyui_probe_reads_system_stats(monkeypatch):
    monkeypatch.setattr(settings, "h3_endpoint_url", "http://host.docker.internal:8188")
    monkeypatch.setattr(settings, "h3_backend", "comfyui")
    monkeypatch.setattr(settings, "vast_instance_id", "")
    seen = []
    probe.check("vast", "anahtar-1234", httpx.MockTransport(lambda r: seen.append(r.url.path) or httpx.Response(200)))
    assert seen == ["/system_stats"]
