"""Ücretli modüllerin güvenlik sözleşmesi: engellenen çağrı, ücretli pakete, ağa ya da LLM'e hiç dokunmadan durur.

Tuzaklar: sahte `fal_client` modülü, `httpx.post` ve `make_llm` bir şeye dokunulursa testi patlatır.
"""
import pathlib
import subprocess
import sys
import types

import pytest

from app.config import settings
from app.engine.providers import GenRequest, get_provider
from app.live import PaidCallBlocked, elevenlabs, fal_video, hermes_llm

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


ENTRY_POINTS = {
    "get_live_provider": lambda: fal_video.get_live_provider("fal_wan"),
    "get_provider": lambda: get_provider("seedance"),
    "FalProvider.generate": lambda: fal_video.FalProvider("fal_wan").generate(REQ),
    "lipsync": lambda: fal_video.lipsync("v.mp4", "a.wav", "o.mp4"),
    "tts": lambda: elevenlabs.tts("merhaba", "voice-1", "joy", "o.mp3"),
    "parse_with_llm": lambda: hermes_llm.parse_with_llm("İÇ. ODA\n\nKerem bekler.", "proje"),
}
KEY_OF = {"get_live_provider": "fal_key", "get_provider": "fal_key", "FalProvider.generate": "fal_key",
          "lipsync": "fal_key", "tts": "elevenlabs_api_key", "parse_with_llm": "anthropic_api_key"}


@pytest.mark.parametrize("name", [n for n in ENTRY_POINTS if n != "get_provider"])   # get_provider ücretsiz modda yerel döner: aşağıda
def test_free_mode_blocks_every_entry_point_even_with_keys_set(booby_trapped, monkeypatch, name):
    monkeypatch.setattr(settings, "dry_run", True)
    for key in ("fal_key", "anthropic_api_key", "elevenlabs_api_key"):
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
    assert isinstance(get_provider("seedance"), LocalProvider)


def test_the_live_modules_import_without_loading_any_paid_package():
    code = ("import sys\nimport app.live, app.live.fal_video, app.live.elevenlabs, app.live.hermes_llm\n"
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
    assert set(table["video_per_second"]) >= {"fal_wan", "seedance"} and table["image_per_megapixel"]["flux_lora"] > 0
    assert table["lipsync_per_minute"] > 0 and table["tts_per_1k_chars"] > 0 and {"input", "output"} <= set(table["llm_per_million_tokens"])


def test_nothing_in_the_paid_modules_reads_the_environment_directly():
    """Anahtarlar yalnızca guard() ile okunur; ortam değişkenine doğrudan bakılmaz."""
    for path in (pathlib.Path(fal_video.__file__), pathlib.Path(elevenlabs.__file__), pathlib.Path(hermes_llm.__file__)):
        source = path.read_text()
        assert "os.environ" not in source and "getenv" not in source, path.name
