"""ÜCRETLİ fal.ai modülü: sahte fal_client ve sahte indirme ile. Hiçbir test gerçek servise istek göndermez.

`paid` fixture'ı kilidi yalnızca bellekteki ayar nesnesinde açar ve genel internete bağlantıyı yasaklar.
"""
import sys
import types

import httpx
import pytest

from app.config import settings
from app.engine.providers import GenRequest, TransientError
from app.live import PaidCallBlocked, fal_video
from app.live.fal_video import FalProvider, get_live_provider, is_transient, lipsync

pytestmark = pytest.mark.usefixtures("paid")


class HTTPError(Exception):
    """fal-client'ın FalClientHTTPError'u gibi: status_code taşır."""

    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeFal:
    """Sahte fal_client modülü: çağrıları kaydeder, uygulamaya göre yanıt verir ya da hata fırlatır."""

    def __init__(self):
        self.calls, self.uploads, self.keys, self.behaviour = [], [], [], {}
        self.counter = 0

    def responder(self, application, arguments):
        if application in self.behaviour:
            return self.behaviour[application](arguments)
        if application == fal_video.FLUX:
            return {"images": [{"url": "https://fal.test/frame.jpg"}]}
        if application == fal_video.LIPSYNC:
            return {"video": {"url": "https://fal.test/synced.mp4"}}
        return {"video": {"url": "https://fal.test/clip.mp4"}, "seed": arguments.get("seed")}

    def module(self):
        fake = self

        class Handle:
            def __init__(self, request_id, result):
                self.request_id, self._result = request_id, result

            def get(self):
                if isinstance(self._result, BaseException):
                    raise self._result
                return self._result

        class SyncClient:
            def __init__(self, key=None, default_timeout=120.0):
                fake.keys.append(key)

            def submit(self, application, arguments, **kw):
                fake.counter += 1
                fake.calls.append((application, arguments, kw))
                try:
                    result = fake.responder(application, arguments)
                except BaseException as e:                       # noqa: BLE001 — hata, handle.get()'te fırlatılır
                    result = e
                return Handle(f"req-{fake.counter}", result)

            def upload_file(self, path):
                fake.uploads.append(str(path))
                return f"https://fal.test/upload/{len(fake.uploads)}"

        mod = types.ModuleType("fal_client")
        mod.SyncClient = SyncClient
        return mod

    def apps(self):
        return [a for a, _, _ in self.calls]


@pytest.fixture
def fal(monkeypatch, tmp_path):
    fake = FakeFal()
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    downloads = []

    def fake_download(url):
        downloads.append(url)
        path = tmp_path / f"dl{len(downloads)}.mp4"
        path.write_bytes(b"mp4:" + url.encode())
        return str(path)

    monkeypatch.setattr(fal_video, "_download", fake_download)
    fake.downloads = downloads
    return fake


def request(engine_extra=None, **kw):
    base = dict(prompt="köprü çöküyor", seed=1234, resolution="720p", duration_s=3.0, negative="bulanık")
    return GenRequest(**{**base, **kw}, extra=engine_extra or {})


# ---------- sağlayıcı seçimi ----------

def test_get_live_provider_serves_wan_and_seedance_but_not_higgsfield(fal):
    assert get_live_provider("fal_wan").engine == "fal_wan" and get_live_provider("seedance").engine == "seedance"
    with pytest.raises(NotImplementedError, match="higgsfield"):
        get_live_provider("higgsfield")


def test_the_engine_router_hands_back_the_live_provider_only_in_paid_mode(fal):
    from app.engine.providers import LocalProvider, get_provider
    assert isinstance(get_provider("seedance"), FalProvider)
    settings.dry_run = True
    assert isinstance(get_provider("seedance"), LocalProvider)


# ---------- süre, çözünürlük, maliyet ----------

@pytest.mark.parametrize("seconds,frames", [(0.5, 17), (3.0, 48), (5.0, 80), (10.0, 160), (11.0, 161), (60.0, 161)])
def test_wan_frame_count_is_clamped_to_the_documented_range(seconds, frames):
    assert FalProvider("fal_wan").frames(seconds) == frames


@pytest.mark.parametrize("seconds,billed", [(1.0, 4), (3.0, 4), (4.0, 4), (4.2, 5), (9.0, 9), (20.0, 15)])
def test_seedance_duration_is_clamped_to_4_to_15_seconds(seconds, billed):
    """Seedance yalnızca "auto" ya da 4-15 sn kabul eder; Hermes 3 sn'lik çekim üretir."""
    assert FalProvider("seedance").billed_seconds(seconds) == billed


def test_cost_is_priced_for_the_duration_that_is_actually_generated():
    wan, seed = FalProvider("fal_wan"), FalProvider("seedance")
    assert wan.estimate(request()) == pytest.approx(48 / 16 * 0.08 + 0.035)                 # 3 sn + ilk kare
    assert seed.estimate(request()) == pytest.approx(4 * 0.242 + 0.035)                      # 3 sn'lik çekim 4 sn faturalanır
    assert wan.estimate(request(resolution="480p")) == pytest.approx(3 * 0.04 + 0.035)
    assert wan.estimate(request(image_url="https://x/frame.jpg")) == pytest.approx(3 * 0.08)


@pytest.mark.parametrize("engine,asked,used", [("fal_wan", "1080p", "720p"), ("fal_wan", "580p", "580p"),
                                               ("seedance", "580p", "720p"), ("seedance", "480p", "480p")])
def test_unsupported_resolutions_fall_back_to_720p(engine, asked, used):
    assert FalProvider(engine).resolution(asked) == used


# ---------- istek parametreleri ----------

def test_wan_receives_the_documented_arguments(fal):
    FalProvider("fal_wan").generate(request())
    app, args, kw = fal.calls[-1]
    assert app == "fal-ai/wan/v2.2-a14b/image-to-video"
    assert args == {"prompt": "köprü çöküyor", "image_url": "https://fal.test/frame.jpg", "num_frames": 48,
                    "frames_per_second": 16, "resolution": "720p", "negative_prompt": "bulanık", "seed": 1234,
                    "aspect_ratio": "16:9", "enable_safety_checker": True}
    assert kw["start_timeout"] == fal_video.QUEUE_START_TIMEOUT_S


def test_seedance_receives_the_documented_arguments_and_no_seed(fal):
    FalProvider("seedance").generate(request())
    app, args, _ = fal.calls[-1]
    assert app == "bytedance/seedance-2.0/fast/image-to-video"
    assert args == {"prompt": "köprü çöküyor", "image_url": "https://fal.test/frame.jpg", "resolution": "720p",
                    "duration": "4", "aspect_ratio": "16:9", "generate_audio": False}     # tohum yok; yerleşik ses kapalı
    assert "seed" not in args and isinstance(args["duration"], str)


def test_the_first_frame_is_a_flux_lora_image_with_the_characters_loras_and_trigger_words(fal):
    extra = {"loras": [{"path": "https://x/kerem.safetensors", "scale": 0.8}], "lora_triggers": ["kerem_tok", ""]}
    FalProvider("fal_wan").generate(request(extra))
    app, args, _ = fal.calls[0]
    assert app == "fal-ai/flux-lora"
    assert args == {"prompt": "kerem_tok, köprü çöküyor", "image_size": "landscape_16_9", "num_images": 1,
                    "output_format": "jpeg", "enable_safety_checker": True, "seed": 1234,
                    "loras": [{"path": "https://x/kerem.safetensors", "scale": 0.8}]}


def test_without_loras_the_flux_request_has_no_lora_key(fal):
    FalProvider("fal_wan").generate(request())
    assert "loras" not in fal.calls[0][1]


def test_a_given_image_skips_the_first_frame_generation(fal):
    FalProvider("fal_wan").generate(request(image_url="https://x/mine.jpg"))
    assert fal.apps() == ["fal-ai/wan/v2.2-a14b/image-to-video"] and fal.calls[0][1]["image_url"] == "https://x/mine.jpg"


def test_the_client_gets_the_key_from_the_guard_not_from_the_environment(fal):
    FalProvider("fal_wan").generate(request())
    assert fal.keys == ["test-fal_key"]


# ---------- sonuç ----------

def test_generate_returns_the_downloaded_clip_with_the_real_request_id_and_the_estimate_as_cost(fal):
    provider = FalProvider("seedance")
    result = provider.generate(request())
    assert result.request_id == "req-2" and result.engine == "seedance"                     # req-1: ilk kare
    assert open(result.local_path, "rb").read() == b"mp4:https://fal.test/clip.mp4"
    assert result.cost_usd == provider.estimate(request())
    assert result.meta == {"billed_seconds": 4.0, "seed_sent": False}
    assert fal.downloads == ["https://fal.test/clip.mp4"]


def test_wan_reports_that_its_seed_was_sent(fal):
    assert FalProvider("fal_wan").generate(request()).meta["seed_sent"] is True


# ---------- hata sınıflandırma ----------

@pytest.mark.parametrize("exc,transient", [
    (HTTPError(429), True), (HTTPError(500), True), (HTTPError(503), True), (HTTPError(599), True),
    (HTTPError(400), False), (HTTPError(401), False), (HTTPError(404), False), (HTTPError(422), False),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(503)), True),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(429)), True),
    (httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(403)), False),
    (TimeoutError("kuyruk"), True), (httpx.ConnectError("x"), True), (httpx.ReadTimeout("x"), True),
    (ValueError("x"), False), (KeyError("video"), False),
])
def test_only_429_5xx_and_timeouts_are_transient(exc, transient):
    assert is_transient(exc) is transient


@pytest.mark.parametrize("status", [429, 502])
def test_a_transient_provider_error_surfaces_as_transient_error(fal, status):
    fal.behaviour[fal_video.WAN] = lambda a: HTTPError(status)
    with pytest.raises(TransientError):
        FalProvider("fal_wan").generate(request())


def test_a_permanent_error_surfaces_unchanged(fal):
    fal.behaviour[fal_video.WAN] = lambda a: HTTPError(422)
    with pytest.raises(HTTPError) as e:
        FalProvider("fal_wan").generate(request())
    assert e.value.status_code == 422 and e.value.__cause__ is None


# ---------- yeniden deneme iki kez ödetmez ----------

def test_a_retry_after_a_transient_video_error_reuses_the_paid_first_frame(fal):
    provider, state = FalProvider("fal_wan"), {"n": 0}

    def flaky(args):
        state["n"] += 1
        return HTTPError(503) if state["n"] == 1 else {"video": {"url": "https://fal.test/clip.mp4"}}

    fal.behaviour[fal_video.WAN] = flaky
    with pytest.raises(TransientError):
        provider.generate(request())
    provider.generate(request())
    assert fal.apps().count("fal-ai/flux-lora") == 1 and fal.apps().count(fal_video.WAN) == 2


def test_a_failed_download_does_not_pay_for_the_video_again(fal, monkeypatch, tmp_path):
    provider, attempts = FalProvider("fal_wan"), []
    good = fal_video._download

    def flaky_download(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise TransientError("ağ kesildi")
        return good(url)

    monkeypatch.setattr(fal_video, "_download", flaky_download)
    with pytest.raises(TransientError):
        provider.generate(request())
    result = provider.generate(request())
    assert fal.apps().count(fal_video.WAN) == 1 and fal.apps().count("fal-ai/flux-lora") == 1
    assert result.request_id == "req-2" and len(attempts) == 2


def test_caches_are_cleared_after_a_success_so_the_next_request_pays_normally(fal):
    provider = FalProvider("fal_wan")
    provider.generate(request())
    provider.generate(request())
    assert fal.apps().count("fal-ai/flux-lora") == 2 and fal.apps().count(fal_video.WAN) == 2


def test_different_requests_never_share_a_first_frame(fal):
    provider = FalProvider("fal_wan")
    for seed in (1, 2):
        provider.generate(request(seed=seed))
    assert fal.apps().count("fal-ai/flux-lora") == 2


# ---------- indirme ----------

class FakeResponse:
    def __init__(self, status, chunks=(b"abc", b"def")):
        self.status, self.chunks = status, chunks

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://fal.test"), response=httpx.Response(self.status))

    def iter_bytes(self):
        yield from self.chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def downloading(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(fal_video.time, "sleep", lambda s: None)
    return tmp_path


def test_download_retries_a_network_blip_and_writes_the_file(downloading, monkeypatch):
    seen = []

    def stream(method, url, **kw):
        seen.append(url)
        if len(seen) == 1:
            raise httpx.ReadError("kesildi")
        return FakeResponse(200)

    monkeypatch.setattr(fal_video.httpx, "stream", stream)
    path = fal_video._download("https://fal.test/a.mp4")
    assert open(path, "rb").read() == b"abcdef" and len(seen) == 2


def test_download_gives_up_with_a_transient_error_and_leaves_no_partial_file(downloading, monkeypatch):
    monkeypatch.setattr(fal_video.httpx, "stream", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("yok")))
    with pytest.raises(TransientError):
        fal_video._download("https://fal.test/a.mp4")
    assert list(downloading.iterdir()) == []


def test_download_does_not_retry_an_expired_link(downloading, monkeypatch):
    calls = []
    monkeypatch.setattr(fal_video.httpx, "stream", lambda *a, **k: calls.append(1) or FakeResponse(403))
    with pytest.raises(httpx.HTTPStatusError):
        fal_video._download("https://fal.test/a.mp4")
    assert len(calls) == 1


def test_download_retries_a_5xx(downloading, monkeypatch):
    responses = iter([FakeResponse(503), FakeResponse(200)])
    monkeypatch.setattr(fal_video.httpx, "stream", lambda *a, **k: next(responses))
    assert open(fal_video._download("https://fal.test/a.mp4"), "rb").read() == b"abcdef"


# ---------- dudak senkronu ----------

def test_lipsync_uploads_both_files_and_writes_the_synced_video(fal, tmp_path):
    out = lipsync("/work/video.mp4", "/work/voice.wav", str(tmp_path / "out.mp4"))
    assert fal.uploads == ["/work/video.mp4", "/work/voice.wav"]
    app, args, _ = fal.calls[-1]
    assert app == "fal-ai/sync-lipsync/v2/pro"
    assert args == {"video_url": "https://fal.test/upload/1", "audio_url": "https://fal.test/upload/2", "sync_mode": "cut_off"}
    assert open(out, "rb").read() == b"mp4:https://fal.test/synced.mp4"


def test_lipsync_classifies_errors_the_same_way(fal, tmp_path):
    fal.behaviour[fal_video.LIPSYNC] = lambda a: HTTPError(429)
    with pytest.raises(TransientError):
        lipsync("/w/v.mp4", "/w/a.wav", str(tmp_path / "o.mp4"))


# ---------- kilit ----------

def test_every_entry_point_is_blocked_in_free_mode_before_touching_fal(fal, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    for call in (lambda: get_live_provider("fal_wan"), lambda: lipsync("v", "a", "o"),
                 lambda: FalProvider("fal_wan").generate(request())):
        with pytest.raises(PaidCallBlocked):
            call()
    assert fal.calls == [] and fal.keys == []


def test_every_entry_point_is_blocked_without_a_key(fal, monkeypatch):
    monkeypatch.setattr(settings, "fal_key", "")
    for call in (lambda: get_live_provider("seedance"), lambda: lipsync("v", "a", "o")):
        with pytest.raises(PaidCallBlocked, match="FAL_KEY"):
            call()
    assert fal.calls == []


# ---------- gerçek paketle sözleşme ----------

def test_the_real_fal_client_exposes_what_this_module_relies_on():
    """Sahte modülün gerçeği temsil ettiğini doğrular. Paket kurulu değilse atlanır (requirements-live.txt)."""
    pytest.importorskip("fal_client")
    import dataclasses
    import inspect

    import fal_client
    from fal_client.client import FalClientHTTPError, FalClientTimeoutError
    submit = inspect.signature(fal_client.SyncClient.submit)
    assert {"application", "arguments", "start_timeout"} <= set(submit.parameters)
    assert "request_id" in {f.name for f in dataclasses.fields(fal_client.SyncRequestHandle)}
    assert "get" in dir(fal_client.SyncRequestHandle) and "upload_file" in dir(fal_client.SyncClient)
    assert "key" in inspect.signature(fal_client.SyncClient).parameters
    assert "status_code" in {f.name for f in dataclasses.fields(FalClientHTTPError)}
    assert issubclass(FalClientTimeoutError, TimeoutError)
