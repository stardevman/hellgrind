"""ÜCRETLİ MiniMax H3 modülü (kendi GPU sunucusu, ör. vast.ai): sahte SGLang sunucusuyla. Hiçbir test gerçek sunucuya
istek göndermez; `h3` fixture'ı kilidi yalnızca bellekte açar ve httpx'e sahte taşıyıcı bağlar.
"""
import base64
import io

import httpx
import pytest
from PIL import Image

from app.budget import history, pricing
from app.config import settings
from app.engine.providers import GenRequest, LocalProvider, TransientError, get_provider, route_engine
from app.live import PaidCallBlocked, probe, vast_h3
from app.live.vast_h3 import H3Provider, JobFailed, build_job, get_live_provider, seconds_for
from tests.media_utils import probe as media_probe


def request(extra=None, **kw):
    base = dict(prompt="Kerem köprüde koşuyor", seed=4321, resolution="720p", duration_s=4.0, negative="bulanık")
    return GenRequest(**{**base, **kw}, extra=extra or {})


def png(tmp_path, size=(2400, 1200), name="kare.png"):
    path = tmp_path / name
    Image.new("RGB", size, (200, 120, 40)).save(path)
    return str(path)


# ---------- sağlayıcı seçimi ve kilit ----------

def test_new_shots_go_to_h3_and_only_a_chosen_seedance_is_kept():
    from types import SimpleNamespace as NS
    assert route_engine(NS(engine=None, action_complexity=0.95)) == "h3"          # yoğun aksiyon da H3
    assert route_engine(NS(engine="fal_wan", action_complexity=0.1)) == "h3"      # eski Wan çekimi H3'e geçer
    assert route_engine(NS(engine="seedance", action_complexity=0.1)) == "seedance"   # yönetmenin seçimi korunur


def test_free_mode_never_reaches_the_h3_module(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    assert isinstance(get_provider("h3"), LocalProvider)
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        get_live_provider()


def test_paid_mode_without_a_key_or_a_server_address_is_blocked(paid, monkeypatch):
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "h3_api_key", "")
    with pytest.raises(PaidCallBlocked, match="H3_API_KEY"):
        get_live_provider()
    monkeypatch.setattr(settings, "h3_api_key", "belirtec-1234")
    monkeypatch.setattr(settings, "h3_endpoint_url", "")
    with pytest.raises(PaidCallBlocked, match="sunucu adresi tanımlı değil"):
        get_live_provider()


def test_paid_mode_hands_back_the_h3_provider(h3):
    provider = get_provider("h3")
    assert isinstance(provider, H3Provider) and provider.engine == "h3" and provider.key_name == "h3_api_key"
    assert provider.image_first is False                                         # önce görsel yok: video doğrudan


# ---------- istek gövdesi ----------

@pytest.mark.parametrize("asked,sent", [(0.5, 4), (3.0, 4), (4.0, 4), (4.2, 5), (7.5, 8), (15.0, 15), (40.0, 15)])
def test_duration_is_clamped_to_the_models_4_to_15_seconds(asked, sent):
    assert seconds_for(asked) == sent


def test_a_text_only_shot_is_a_t2va_request_with_the_documented_fields(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_steps", "9")                               # panelden metin olarak gelir
    job = build_job(request(duration_s=3.0))
    assert job.task == "t2va" and job.url == "https://h3.test" and job.body["conditions"] == []
    assert job.body["model"] == settings.h3_model and job.body["prompt"] == "Kerem köprüde koşuyor"
    assert job.body["seconds"] == 4 and job.body["target"] == {"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 4.0}
    assert job.body["num_inference_steps"] == 9 and job.body["seed"] == 4321
    assert (job.body["flow_shift"], job.body["audio_flow_shift"], job.body["num_outputs_per_prompt"]) == (12.0, 3.0, 1)
    assert "negative_prompt" not in job.body and "bulanık" not in str(job.body)  # CFG damıtılmış: negatif gitmez


def test_480p_asks_for_a_480_pixel_short_edge_and_everything_else_for_768(h3):
    assert build_job(request(resolution="480p")).body["target"]["short_edge"] == 480
    assert build_job(request(resolution="1080p")).body["target"]["short_edge"] == 768


def test_a_given_first_frame_becomes_an_fl2va_keyframe(h3, tmp_path):
    uri = H3Provider().upload(png(tmp_path))
    job = build_job(request(image_url=uri))
    assert job.task == "fl2va"
    assert job.body["conditions"] == [{"type": "image", "uri": uri, "role": "keyframe", "frame_index": 0}]


def test_uploaded_frames_are_sent_inline_as_downscaled_jpeg_data_uris(h3, tmp_path):
    uri = H3Provider().upload(png(tmp_path, size=(3000, 1500)))
    assert uri.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as im:
        assert max(im.size) == vast_h3.IMAGE_EDGE and im.format == "JPEG"


STILLS = {"stills": [{"name": "Kerem", "key": "assets/k/still/a.png"}, {"name": "Ayşe", "key": "assets/a/still/b.png"}]}


def test_character_stills_go_to_the_ref2va_server_with_picture_tags(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test/")
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: f"data:image/jpeg;base64,{key}")
    job = build_job(request(STILLS, image_url="data:image/jpeg;base64,ilk"))
    assert job.task == "ref2va" and job.url == "https://h3-ref.test" and job.references == ["Kerem", "Ayşe"]
    roles = [(c["role"], c["uri"].split(",")[1]) for c in job.body["conditions"]]
    assert roles == [("reference", "assets/k/still/a.png"), ("reference", "assets/a/still/b.png"), ("keyframe", "ilk")]
    assert job.body["prompt"].startswith("Character references: <Picture 1> is Kerem, <Picture 2> is Ayşe.")
    assert job.body["prompt"].endswith("Kerem köprüde koşuyor")


def test_without_a_ref2va_server_the_stills_are_not_sent(h3):
    job = build_job(request(STILLS))
    assert job.task == "t2va" and job.body["conditions"] == [] and job.references == []


def test_at_most_nine_references_are_sent(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: "data:image/jpeg;base64,eA==")
    many = {"stills": [{"name": f"K{i}", "key": f"assets/{i}/still/x.png"} for i in range(12)]}
    assert len(build_job(request(many)).body["conditions"]) == vast_h3.MAX_REFERENCES


# ---------- üretim ----------

def test_generate_submits_polls_downloads_and_charges_the_reported_gpu_time(h3):
    provider = H3Provider()
    with history.recording() as call:
        result = provider.generate(request())
    assert len(h3.submits) == 1 and result.request_id == "video_1" and result.engine == "h3"
    assert all(r.headers["Authorization"] == "Bearer test-h3_api_key" for r in h3.requests)
    assert result.cost_usd == pricing.estimate_gpu("h3", 42.0) and result.meta["gpu_seconds"] == 42.0
    assert media_probe(result.local_path)["duration"] == pytest.approx(4.0, abs=0.1)
    assert [s["model"] for s in call.steps] == [settings.h3_model] * 2 and call.steps[0]["task"] == "t2va"
    assert call.steps[0]["request_id"] == "video_1" and call.steps[0]["negative_dropped"] is True
    assert call.steps[1]["gpu_seconds"] == 42.0


def test_without_a_reported_gpu_time_the_estimate_is_charged(h3):
    h3.inference_time_s = None
    provider = H3Provider()
    assert provider.generate(request(resolution="480p")).cost_usd == provider.estimate(request(resolution="480p"))


def test_the_estimate_is_the_hourly_rate_times_gpu_seconds_per_video_second():
    hosted = pricing.self_hosted("h3")
    expected = round(hosted["gpu_hourly_usd"] / 3600 * hosted["render_seconds"]["480p"] * 4, 4)
    assert H3Provider().estimate(request(resolution="480p", duration_s=3.0)) == expected     # 3 sn çekim 4 sn üretilir
    assert LocalProvider("h3").estimate(request(resolution="480p", duration_s=3.0)) == expected  # ücretsiz modda da


def test_a_shot_shorter_than_four_seconds_is_cut_to_its_length(h3):
    result = H3Provider().generate(request(duration_s=3.0))
    info = media_probe(result.local_path)
    assert info["duration"] == pytest.approx(3.0, abs=0.1) and info["video"]


def test_overloaded_or_failing_servers_are_transient(h3):
    h3.submit_errors = [503]
    with pytest.raises(TransientError):
        H3Provider().generate(request())


def test_a_rejected_token_stops_with_a_turkish_explanation(h3):
    h3.submit_errors = [401]
    with pytest.raises(PaidCallBlocked, match="erişim anahtarını reddetti"):
        H3Provider().generate(request())


def test_an_unreachable_server_stops_instead_of_retrying(h3, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("bağlantı reddedildi", request=request)
    monkeypatch.setattr(vast_h3, "TRANSPORT", httpx.MockTransport(refuse))
    with pytest.raises(PaidCallBlocked, match="makine çalışıyor mu"):
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
    h3.content_errors = [503, 503, 503]                    # üç deneme de düşer
    provider = H3Provider()
    with pytest.raises(TransientError, match="indirilemedi"):
        provider.generate(request())
    with history.recording() as call:
        result = provider.generate(request())              # iterasyonun yeniden denemesi
    assert len(h3.submits) == 1 and result.request_id == "video_1" and call.steps[0]["reused"] is True


def test_a_job_that_outlives_the_timeout_is_awaited_again_not_resubmitted(h3, monkeypatch):
    monkeypatch.setattr(settings, "h3_timeout_s", 0)
    h3.never_finish = True
    provider = H3Provider()
    with pytest.raises(TransientError, match="bitmedi"):
        provider.generate(request())
    h3.never_finish = False
    assert provider.generate(request()).request_id == "video_1" and len(h3.submits) == 1


# ---------- bağlantı sınaması ----------

def probe_with(status):
    return httpx.MockTransport(lambda r: httpx.Response(status, json={}))


@pytest.mark.parametrize("status,ok,text", [(200, True, "hazır"), (401, False, "reddedildi"), (503, None, "açılıyor"),
                                            (530, False, "tüneli kapalı"), (418, None, "Beklenmeyen")])
def test_the_probe_reads_the_servers_health_endpoint(monkeypatch, status, ok, text):
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "")
    seen = []

    def health(request):
        seen.append((request.method, str(request.url), request.headers["Authorization"]))
        return httpx.Response(status, json={})
    result = probe.check("vast", "belirtec-1234", httpx.MockTransport(health))
    assert result["ok"] is ok and text in result["message"]
    assert seen == [("GET", "https://h3.test/health", "Bearer belirtec-1234")]


def test_the_probe_needs_an_address_and_reports_the_ref2va_server(monkeypatch):
    monkeypatch.setattr(settings, "h3_endpoint_url", "")
    assert probe.check("vast", "belirtec-1234", probe_with(200))["message"] == "Sunucu adresi tanımlı değil."
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    assert "İki sunucu da hazır" in probe.check("vast", "belirtec-1234", probe_with(200))["message"]
    transport = httpx.MockTransport(lambda r: httpx.Response(200 if r.url.host == "h3.test" else 401, json={}))
    result = probe.check("vast", "belirtec-1234", transport)
    assert result["ok"] is False and result["message"].startswith("Ana sunucu hazır; referans sunucusu")
