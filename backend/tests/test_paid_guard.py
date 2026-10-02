"""Ücretli kilit (değişmez kural 1 ve 2). Hiçbir ağ isteği yapılmaz, ücretli modül içe aktarılmaz.

`guard` yalnızca ayarları okuyan saf bir işlevdir; testler ayarı bellekte değiştirir, ortam değişkenine ya da
.env'e dokunmaz ve DRY_RUN=0 ile hiçbir şey çalıştırmaz.
"""
import sys

import pytest

from app.config import settings
from app.engine.providers import LocalProvider, get_provider
from app.live import PaidCallBlocked, guard

KEYS = ("fal_key", "elevenlabs_api_key", "anthropic_api_key")


def test_free_mode_is_the_default():
    assert settings.dry_run is True


@pytest.mark.parametrize("key", KEYS)
def test_guard_blocks_every_paid_key_in_free_mode(key):
    with pytest.raises(PaidCallBlocked):
        guard(key)


@pytest.mark.parametrize("key", KEYS)
def test_guard_blocks_even_when_the_key_is_set(monkeypatch, key):
    """DRY_RUN=1 iken anahtar tanımlı olsa bile ücretli çağrı yapılmaz."""
    monkeypatch.setattr(settings, key, "gizli-anahtar")
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        guard(key)


@pytest.mark.parametrize("key", KEYS)
def test_guard_blocks_when_the_key_is_empty_even_if_paid_mode_is_on(monkeypatch, key):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, key, "")
    with pytest.raises(PaidCallBlocked, match=key.upper()):
        guard(key)


def test_guard_hands_out_the_key_only_when_paid_mode_is_on_and_the_key_exists(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "fal_key", "gizli-anahtar")
    assert guard("fal_key") == "gizli-anahtar"


def test_unknown_key_name_is_blocked_not_crashed(monkeypatch):
    monkeypatch.setattr(settings, "dry_run", False)
    with pytest.raises(PaidCallBlocked):
        guard("olmayan_anahtar")


@pytest.mark.parametrize("engine", ["fal_wan", "seedance", "higgsfield"])
def test_provider_factory_returns_the_free_provider(engine):
    assert isinstance(get_provider(engine), LocalProvider)


def test_free_mode_never_loads_paid_modules_or_their_dependencies():
    """Önce/sonra karşılaştırması: başka testler ücretli modülleri (ağsız) içe aktarmış olabilir; burada yeni yükleme olmamalı."""
    watched = {"app.live.fal_video", "app.live.elevenlabs", "app.live.hermes_llm",
               "fal_client", "langgraph", "langchain_anthropic", "anthropic"}
    before = watched & set(sys.modules)
    for engine in ("fal_wan", "seedance"):
        get_provider(engine)
    assert watched & set(sys.modules) == before
