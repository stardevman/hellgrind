"""fal.ai sağlayıcısı: SAHTE fal_client ile, ağ yasak. Parametreler fal.ai model sayfalarından doğrulanmıştır (2 Ekim 2026).

Hiçbir gerçek istek yapılmaz; `guard` yerine sahte anahtar dönen bir işlev konur (DRY_RUN bayrağına dokunulmaz).
"""
import os
import sys

import httpx
import pytest
from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.engine import iteration
from app.engine.providers import GenRequest, LocalProvider, TransientError
from app.live import fal_video
from app.live.fal_video import ENDPOINTS, FalVideoProvider, lipsync

FLUX_FEE = 0.035


class HTTPError(Exception):
    """fal_client.FalClientHTTPError benzeri: .status_code taşır."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeFal:
    def __init__(self):
        self.calls, self.uploads, self.failures = [], [], {}

    def subscribe(self, app, arguments, *, on_enqueue=None, **kw):
        self.calls.append((app, arguments))
        if on_enqueue:
            on_enqueue(f"istek-{len(self.calls)}")
        if self.failures.get(app):
            raise self.failures[app].pop(0)
        if app == ENDPOINTS["flux"]:
            return {"images": [{"url": "https://cdn.example/ilk-kare.jpg"}]}
        return {"video": {"url": "https://cdn.example/cikti.mp4"}}

    def upload_file(self, path):
        self.uploads.append(path)
        return f"https://cdn.example/yukleme/{os.path.basename(path)}"

    def apps(self):
        return [a for a, _ in self.calls]


@pytest.fixture
def fal(monkeypatch, tmp_path, no_network):
    fake = FakeFal()
    monkeypatch.setitem(sys.modules, "fal_client", fake)
    monkeypatch.setattr(fal_video, "guard", lambda key: "sahte-anahtar")
    monkeypatch.setenv("FAL_KEY", "")
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))

    def fake_download(url, ext=".mp4"):
        path = tmp_path / f"indirilen{ext}"
        path.write_bytes(b"video")
        return str(path)

    monkeypatch.setattr(fal_video, "_download", fake_download)
    return fake


def request(seed=7, resolution="480p", duration=5.0, negative="", **extra):
    return GenRequest(prompt="köprüde iki kişi", negative=negative, seed=seed, resolution=resolution, duration_s=duration, extra=extra)


# ---------- kimlikler ve süreler ----------

def test_endpoint_ids_match_the_fal_model_pages():
    assert ENDPOINTS == {"flux": "fal-ai/flux-lora", "fal_wan": "fal-ai/wan/v2.2-a14b/image-to-video",
                         "seedance": "bytedance/seedance-2.0/fast/image-to-video", "lipsync": "fal-ai/sync-lipsync/v2/pro"}


@pytest.mark.parametrize("seconds,frames", [(0.5, 17), (3.0, 48), (5.0, 80), (10.0, 160), (20.0, 161)])
def test_wan_frame_count_is_clamped_to_17_161(seconds, frames):
    assert fal_video.wan_frames(seconds) == frames


@pytest.mark.parametrize("seconds,billed", [(2.0, 4), (3.0, 4), (3.6, 4), (5.0, 5), (10.0, 10), (20.0, 15)])
def test_seedance_duration_is_clamped_to_4_15(seconds, billed):
    """Regresyon: Hermes 3 sn'lik çekim üretir; Seedance "3" kabul etmez (en az 4)."""
    assert fal_video.seedance_seconds(seconds) == billed


# ---------- tahmin (bütçe rezervasyonu) gerçekte üretilen süreye göre ----------

def test_estimate_uses_the_duration_that_is_really_generated(fal):
    wan, seedance = FalVideoProvider("fal_wan"), FalVideoProvider("seedance")
    assert wan.estimate(request(duration=5.0)) == round(0.04 * 5.0 + FLUX_FEE, 4)
    assert wan.estimate(request(duration=3.4)) == round(0.04 * (54 / 16) + FLUX_FEE, 4)          # 54 kare / 16 fps
    assert seedance.estimate(request(resolution="720p", duration=3.0)) == round(0.242 * 4 + FLUX_FEE, 4)   # 3 sn -> 4 sn faturalanır


def test_unsupported_resolutions_fall_back_to_720p(fal):
    assert fal_video.resolution_for("fal_wan", "1080p") == "720p"
    assert fal_video.resolution_for("seedance", "580p") == "720p"
    assert fal_video.resolution_for("fal_wan", "580p") == "580p" and fal_video.resolution_for("seedance", "480p") == "480p"


# ---------- üretim akışı ----------

def test_wan_generation_sends_the_documented_arguments(fal):
    result = FalVideoProvider("fal_wan").generate(request(seed=11, duration=3.0, negative="bulanık"))

    assert fal.apps() == [ENDPOINTS["flux"], ENDPOINTS["fal_wan"]]
    assert fal.calls[1][1] == {"prompt": "köprüde iki kişi", "image_url": "https://cdn.example/ilk-kare.jpg", "seed": 11,
                               "resolution": "480p", "aspect_ratio": "16:9", "num_frames": 48, "frames_per_second": 16,
                               "negative_prompt": "bulanık"}
    assert result.engine == "fal_wan" and result.request_id == "fal-istek-2" and os.path.exists(result.local_path)
    assert result.cost_usd == round(0.04 * 3.0 + FLUX_FEE, 4)


def test_wan_omits_an_empty_negative_prompt(fal):
    FalVideoProvider("fal_wan").generate(request())
    assert "negative_prompt" not in fal.calls[1][1]


def test_seedance_generation_sends_the_documented_arguments(fal):
    FalVideoProvider("seedance").generate(request(resolution="720p", duration=3.0))
    assert fal.calls[1][1] == {"prompt": "köprüde iki kişi", "image_url": "https://cdn.example/ilk-kare.jpg", "resolution": "720p",
                               "aspect_ratio": "16:9", "duration": "4", "generate_audio": False}      # seed parametresi yok


def test_first_frame_uses_character_loras_triggers_and_the_seed(fal):
    loras = [{"path": "https://lora.example/kerem.safetensors", "scale": 0.8}]
    FalVideoProvider("fal_wan").generate(request(seed=5, loras=loras, lora_triggers=["kerem_tok", "ayse_tok"]))
    assert fal.calls[0][1] == {"prompt": "kerem_tok, ayse_tok, köprüde iki kişi", "seed": 5, "image_size": "landscape_16_9",
                               "num_images": 1, "loras": loras}


def test_first_frame_without_loras_sends_an_empty_list(fal):
    FalVideoProvider("fal_wan").generate(request())
    assert fal.calls[0][1]["loras"] == [] and fal.calls[0][1]["prompt"] == "köprüde iki kişi"


def test_retry_after_a_transient_video_error_does_not_pay_for_a_second_first_frame(fal):
    """Regresyon: yeniden denemede Flux ilk karesi tekrar üretilip tekrar ücretleniyordu."""
    fal.failures[ENDPOINTS["fal_wan"]] = [HTTPError(503)]
    provider, req = FalVideoProvider("fal_wan"), request()

    with pytest.raises(TransientError):
        provider.generate(req)
    result = provider.generate(req)                                   # iteration aynı isteği yeniden dener

    assert fal.apps() == [ENDPOINTS["flux"], ENDPOINTS["fal_wan"], ENDPOINTS["fal_wan"]]      # Flux yalnızca bir kez
    assert fal.calls[2][1]["image_url"] == req.image_url == "https://cdn.example/ilk-kare.jpg"
    assert result.cost_usd == provider.estimate(req)                  # tek kez ödenen Flux, tam tahminle bir kez yazılır


def test_unsupported_engine_is_rejected(fal):
    with pytest.raises(ValueError, match="higgsfield"):
        FalVideoProvider("higgsfield")


# ---------- hata sınıflandırması ----------

@pytest.mark.parametrize("error,transient", [
    (HTTPError(429), True), (HTTPError(500), True), (HTTPError(502), True), (HTTPError(503), True), (HTTPError(504), True),
    (HTTPError(400), False), (HTTPError(401), False), (HTTPError(422), False), (HTTPError(404), False),
    (TimeoutError("zaman aşımı"), True), (httpx.ConnectError("bağlanamadı"), True), (ValueError("başka"), False),
])
def test_transient_errors_are_only_429_5xx_timeouts_and_transport_failures(fal, error, transient):
    fal.failures[ENDPOINTS["flux"]] = [error]
    if transient:
        with pytest.raises(TransientError):
            FalVideoProvider("fal_wan").generate(request())
    else:
        with pytest.raises(type(error)):
            FalVideoProvider("fal_wan").generate(request())


def test_response_attached_status_code_is_understood_too(fal):
    class Wrapped(Exception):
        response = type("R", (), {"status_code": 429})()
    fal.failures[ENDPOINTS["flux"]] = [Wrapped()]
    with pytest.raises(TransientError):
        FalVideoProvider("fal_wan").generate(request())


# ---------- dudak senkronu ----------

def test_lipsync_uploads_both_files_and_writes_the_result(fal, tmp_path):
    out = lipsync("/tmp/video.mp4", "/tmp/ses.mp4", str(tmp_path / "sonuc.mp4"))

    app, args = fal.calls[0]
    assert app == ENDPOINTS["lipsync"]
    assert args == {"video_url": "https://cdn.example/yukleme/video.mp4", "audio_url": "https://cdn.example/yukleme/ses.mp4",
                    "sync_mode": "cut_off"}
    assert fal.uploads == ["/tmp/video.mp4", "/tmp/ses.mp4"] and open(out, "rb").read() == b"video"


# ---------- indirme ----------

class FakeStream:
    def __init__(self, fail):
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise httpx.ReadError("kesildi")
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield b"abc"


def test_download_retries_so_a_paid_generation_is_not_lost_to_one_network_blip(monkeypatch, tmp_path, no_network):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(fal_video.time, "sleep", lambda s: None)
    outcomes = iter([True, True, False])
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream(next(outcomes)))
    assert open(fal_video._download("https://cdn.example/x.mp4"), "rb").read() == b"abc"


def test_download_gives_up_after_five_attempts(monkeypatch, tmp_path, no_network):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    sleeps = []
    monkeypatch.setattr(fal_video.time, "sleep", sleeps.append)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: FakeStream(True))
    with pytest.raises(httpx.ReadError):
        fal_video._download("https://cdn.example/x.mp4")
    assert sleeps == [1, 2, 4, 8]


# ---------- gerçek kütüphaneyle sözleşme (kuruluysa) ----------

def test_installed_fal_client_accepts_the_arguments_we_pass():
    fc = pytest.importorskip("fal_client")
    import inspect
    assert {"on_enqueue"} <= set(inspect.signature(fc.subscribe).parameters)
    assert "path" in inspect.signature(fc.upload_file).parameters
    assert issubclass(fc.client.FalClientTimeoutError, TimeoutError) and hasattr(fc.client.FalClientHTTPError, "__dataclass_fields__")


# ---------- iteration: karakter LoRA'ları isteğe taşınır (veritabanı) ----------

def test_generation_requests_carry_the_scene_characters_loras(fast, mk_project, mk_scene, monkeypatch):
    seen = []

    class Spy(LocalProvider):
        def generate(self, req):
            seen.append(req.extra)
            return super().generate(req)

    monkeypatch.setattr(iteration, "get_provider", lambda engine: Spy(engine))
    pid = mk_project(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0)
    sid = mk_scene(pid, "pending", duration_s=2.6)
    with session_scope() as s:
        for name, url, scale, trigger in (("Kerem", "https://lora.example/kerem.safetensors", 0.8, "kerem_tok"),
                                          ("Ayşe", None, 1.0, None)):
            cid = s.execute(text("""INSERT INTO characters (project_id, name, pinecone_namespace, lora_url, lora_scale, lora_trigger)
                                    VALUES (:p, :n, :ns, :u, :sc, :t) RETURNING id"""),
                            {"p": pid, "n": name, "ns": pid, "u": url, "sc": scale, "t": trigger}).scalar()
            s.execute(text("INSERT INTO scene_characters (scene_id, character_id) VALUES (:s, :c)"), {"s": sid, "c": cid})

    iteration.run_tiered_generation(pid, sid)

    assert seen and all(x["loras"] == [{"path": "https://lora.example/kerem.safetensors", "scale": 0.8}] for x in seen)
    assert all(x["lora_triggers"] == ["kerem_tok"] for x in seen)
