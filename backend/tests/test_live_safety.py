"""Ücretli modüllerin güvenlik sözleşmesi: engellenen çağrı, ücretli pakete, ağa ya da LLM'e hiç dokunmadan durur.

Tuzaklar: sahte `fal_client` modülü, `httpx.post`, H3 sunucusunun httpx taşıyıcısı ve `make_llm` bir şeye dokunulursa
testi patlatır.
"""
import pathlib
import subprocess
import sys
import types

import pytest

from app.config import settings
from app.engine.providers import GenRequest, get_provider
from app.live import PaidCallBlocked, elevenlabs, fal_video, hermes_llm, runpod_h3, vast_api, vast_h3

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REQ = GenRequest(prompt="x", seed=1, resolution="720p", duration_s=3.0)


@pytest.fixture
def booby_trapped(monkeypatch, no_network):
    """Ücretli bir şeye dokunulursa test patlar."""
    def boom(*a, **k):
        raise AssertionError("engellenmesi gereken çağrı ücretli yola ulaştı")

    trap = types.ModuleType("fal_client")
    trap.SyncClient = boom
    monkeypatch.setitem(sys.modules, "fal_client", trap)
    monkeypatch.setattr(elevenlabs.httpx, "post", boom)
    monkeypatch.setattr(fal_video.httpx, "stream", boom)
    monkeypatch.setattr(hermes_llm, "make_llm", boom)
    monkeypatch.setattr(hermes_llm, "build_graph", boom)
    monkeypatch.setattr(vast_h3, "TRANSPORT", __import__("httpx").MockTransport(boom))
    monkeypatch.setattr(vast_api, "TRANSPORT", __import__("httpx").MockTransport(boom))
    monkeypatch.setattr(runpod_h3, "TRANSPORT", __import__("httpx").MockTransport(boom))
    monkeypatch.setattr(settings, "runpod_endpoint_id", "ep123")
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "vast_instance_id", "4242")


ENTRY_POINTS = {
    "h3_provider": lambda: vast_h3.get_live_provider(),
    "H3Provider.generate": lambda: vast_h3.H3Provider().generate(REQ),
    "get_provider_h3": lambda: get_provider("h3"),
    "runpod_provider": lambda: runpod_h3.get_live_provider(),
    "RunpodProvider.generate": lambda: runpod_h3.RunpodProvider().generate(REQ),
    "runpod_keyframe": lambda: runpod_h3.keyframe_image("kare", 1),
    "runpod_apply": lambda: runpod_h3.apply_config(),
    "runpod_retrieve": lambda: runpod_h3.retrieve_row({"job_id": "j", "kind": "video", "id": 1, "server": "runpod://ep123"}),
    "keyframe_image": lambda: vast_h3.keyframe_image("kare", 1),
    "vast_start": lambda: vast_api.set_state("running"),
    "vast_instance": lambda: vast_api.instance(),
    "get_live_provider": lambda: fal_video.get_live_provider("fal_wan"),
    "get_provider": lambda: get_provider("seedance"),
    "FalProvider.generate": lambda: fal_video.FalProvider("fal_wan").generate(REQ),
    "lipsync": lambda: fal_video.lipsync("v.mp4", "a.wav", "o.mp4"),
    "FalProvider.first_frame": lambda: fal_video.FalProvider("fal_wan").first_frame(REQ),
    "image": lambda: fal_video.image("storyboard", 1),
    "upload": lambda: fal_video.upload("kare.png"),
    "restyle": lambda: fal_video.restyle("previs.mp4", "p", "", 1, "720p", 3.0),
    "tts": lambda: elevenlabs.tts("merhaba", "voice-1", "joy", "o.mp3"),
    "parse_with_llm": lambda: hermes_llm.parse_with_llm("İÇ. ODA\n\nKerem bekler.", "proje"),
}
KEY_OF = {"h3_provider": "vast_api_key", "H3Provider.generate": "vast_api_key", "get_provider_h3": "runpod_api_key",
          "runpod_provider": "runpod_api_key", "RunpodProvider.generate": "runpod_api_key", "runpod_keyframe": "runpod_api_key",
          "runpod_apply": "runpod_api_key", "runpod_retrieve": "runpod_api_key",
          "keyframe_image": "vast_api_key", "vast_start": "vast_api_key", "vast_instance": "vast_api_key",
          "get_live_provider": "fal_key", "get_provider": "fal_key", "FalProvider.generate": "fal_key",
          "lipsync": "fal_key", "tts": "elevenlabs_api_key", "parse_with_llm": "anthropic_api_key",
          "FalProvider.first_frame": "fal_key", "image": "fal_key", "upload": "fal_key", "restyle": "fal_key"}


@pytest.mark.parametrize("name", [n for n in ENTRY_POINTS if not n.startswith("get_provider")])   # yerel döner: aşağıda
def test_free_mode_blocks_every_entry_point_even_with_keys_set(booby_trapped, monkeypatch, name):
    monkeypatch.setattr(settings, "dry_run", True)
    for key in ("runpod_api_key", "vast_api_key", "h3_api_key", "fal_key", "anthropic_api_key", "elevenlabs_api_key"):
        monkeypatch.setattr(settings, key, "gerçek-görünümlü-anahtar")
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        ENTRY_POINTS[name]()


@pytest.mark.parametrize("name", ENTRY_POINTS)
def test_paid_mode_blocks_every_entry_point_without_its_key(booby_trapped, monkeypatch, name):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, KEY_OF[name], "")
    with pytest.raises(PaidCallBlocked, match=KEY_OF[name].upper()):
        ENTRY_POINTS[name]()


def test_the_router_returns_the_local_provider_in_free_mode_and_never_builds_a_live_one(booby_trapped, monkeypatch):
    from app.engine.providers import LocalProvider
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "fal_key", "anahtar")
    monkeypatch.setattr(settings, "h3_api_key", "anahtar")
    assert isinstance(get_provider("seedance"), LocalProvider) and isinstance(get_provider("h3"), LocalProvider)


def test_the_live_modules_import_without_loading_any_paid_package():
    code = ("import sys\nimport app.live, app.live.fal_video, app.live.elevenlabs, app.live.hermes_llm, app.live.vast_h3, "
            "app.live.vast_api, app.live.vast_box\n"
            "paid = {'fal_client', 'langgraph', 'langchain', 'langchain_anthropic', 'langchain_core', 'anthropic'}\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in paid))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BACKEND,
                         env={"PATH": "", "PYTHONPATH": str(BACKEND), "DRY_RUN": "1"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_paid_packages_are_in_the_live_requirements_and_not_in_the_free_ones():
    free = (BACKEND / "requirements.txt").read_text().lower()
    live = (BACKEND / "requirements-live.txt").read_text().lower()
    for package in ("fal-client", "langgraph", "langchain-anthropic"):
        assert package in live and package not in free
    assert "-r requirements.txt" in live


def test_pricing_has_a_rate_for_every_paid_call_the_code_reserves():
    from app.budget import pricing
    table = pricing.table()
    assert set(table["video_per_second"]) >= {"fal_wan", "seedance", "wan_v2v"} and table["image_per_megapixel"]["flux_lora"] > 0
    h3 = table["self_hosted"]["h3"]
    assert h3["gpu_hourly_usd"] > 0 and {"480p", "720p"} <= set(h3["render_seconds"])
    assert table["lipsync_per_minute"] > 0 and table["tts_per_1k_chars"] > 0 and {"input", "output"} <= set(table["llm_per_million_tokens"])


def test_nothing_in_the_paid_modules_reads_the_environment_directly():
    """Anahtarlar yalnızca guard() ile okunur; ortam değişkenine doğrudan bakılmaz."""
    for path in (pathlib.Path(fal_video.__file__), pathlib.Path(elevenlabs.__file__), pathlib.Path(hermes_llm.__file__)):
        source = path.read_text()
        assert "os.environ" not in source and "getenv" not in source, path.name
