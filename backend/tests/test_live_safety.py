"""Ücretli kodun kilitleri (değişmez kural 1 ve 2) — ücretli bağımlılıklar kurulu olmadan da çalışır (CI dahil).

Hiçbir test gerçek bir sağlayıcıya bağlanmaz; `no_network` dış ağ bağlantısını yasaklar.
"""
import ast
import importlib
import pathlib
import sys

import pytest

from app.config import settings
from app.live import PaidCallBlocked

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
LIVE_MODULES = ("app.live.fal_video", "app.live.elevenlabs", "app.live.hermes_llm")
PAID_PACKAGES = {"fal_client", "langgraph", "langchain_anthropic", "langchain_core", "anthropic", "elevenlabs", "pinecone"}


def imports(path: pathlib.Path, top_level_only: bool):
    tree = ast.parse(path.read_text())
    nodes = tree.body if top_level_only else ast.walk(tree)
    for n in nodes:
        if isinstance(n, ast.Import):
            yield from (a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            yield n.module.split(".")[0]


@pytest.mark.parametrize("path", sorted((APP / "live").glob("*.py")), ids=lambda p: p.name)
def test_paid_packages_are_only_imported_inside_functions(path):
    """Kural 2: ücretli kod yalnızca fonksiyon içinde, lazy import ile yüklenir."""
    assert not PAID_PACKAGES & set(imports(path, top_level_only=True))


def test_paid_packages_are_never_imported_outside_app_live():
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py")
                 if "live" not in p.relative_to(APP).parts and PAID_PACKAGES & set(imports(p, top_level_only=False))]
    assert offenders == []


def test_live_modules_import_cleanly_and_load_no_paid_package(no_network):
    before = PAID_PACKAGES & set(sys.modules)
    for name in LIVE_MODULES:
        importlib.import_module(name)
    assert PAID_PACKAGES & set(sys.modules) == before          # içe aktarmak ücretli paketi yüklemez


@pytest.fixture
def blocked(no_network, monkeypatch):
    """Ücretli modül testleri için: dış ağ yasak."""


def test_every_paid_entry_point_is_blocked_in_free_mode_even_with_keys_set(blocked, monkeypatch, tmp_path):
    from app.live.elevenlabs import tts
    from app.live.fal_video import FalVideoProvider, get_live_provider, lipsync
    from app.live.hermes_llm import parse_with_llm
    for key in ("fal_key", "elevenlabs_api_key", "anthropic_api_key"):
        monkeypatch.setattr(settings, key, "gizli-anahtar")
    assert settings.dry_run is True
    before = PAID_PACKAGES & set(sys.modules)

    for call in (lambda: FalVideoProvider("fal_wan"), lambda: get_live_provider("seedance"),
                 lambda: lipsync("v.mp4", "a.wav", str(tmp_path / "o.mp4")),
                 lambda: tts("merhaba", "ses-kimligi", "joy", str(tmp_path / "o.mp3")),
                 lambda: parse_with_llm("İÇ. ODA - GECE\n\nKerem oturur.\n", "00000000-0000-0000-0000-000000000000")):
        with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
            call()
    assert PAID_PACKAGES & set(sys.modules) == before          # engellenen çağrı ücretli paketi hiç yüklemedi


@pytest.mark.parametrize("module,call", [
    ("app.live.fal_video", lambda m: m.FalVideoProvider("fal_wan")),
    ("app.live.elevenlabs", lambda m: m.tts("merhaba", "ses", "joy", "/tmp/yok.mp3")),
    ("app.live.hermes_llm", lambda m: m.parse_with_llm("İÇ. ODA\n\nx\n", "p")),
])
def test_even_in_paid_mode_an_empty_key_blocks_the_call(blocked, monkeypatch, module, call):
    """Anahtar boşsa DRY_RUN=0 olsa bile çağrı yapılmaz. (Bellekte bayrak değişir; hiçbir şey çalıştırılmaz.)"""
    monkeypatch.setattr(settings, "dry_run", False)
    for key in ("fal_key", "elevenlabs_api_key", "anthropic_api_key"):
        monkeypatch.setattr(settings, key, "")
    with pytest.raises(PaidCallBlocked):
        call(importlib.import_module(module))


def test_provider_factory_in_paid_mode_without_a_key_is_still_blocked(blocked, monkeypatch):
    from app.engine.providers import get_provider
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "fal_key", "")
    with pytest.raises(PaidCallBlocked):
        get_provider("fal_wan")


def test_the_network_guard_really_blocks_public_addresses(no_network):
    """Bekçinin kendisi: genel bir adrese bağlanma girişimi bağlantı kurulmadan reddedilir."""
    import socket
    for host in ("8.8.8.8", "api.anthropic.com"):
        with pytest.raises(AssertionError, match="genel internete"):
            socket.create_connection((host, 443), timeout=1) if host[0].isdigit() else socket.socket().connect((host, 443))
