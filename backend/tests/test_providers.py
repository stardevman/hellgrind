"""Sağlayıcı soyutlaması: yönlendirme, ücretsiz LocalProvider, sanal maliyet, bozuk klip ve geçici hata.

Render işlevleri taklit edilir; FFmpeg ve ağ gerekmez.
"""
import os
import sys
from types import SimpleNamespace as NS

import pytest

from app.config import settings
from app.engine import providers
from app.engine.providers import GenRequest, LocalProvider, TransientError, get_provider, route_engine


@pytest.fixture
def renders(monkeypatch, tmp_path):
    calls = {"clip": [], "defect": []}

    def fake_clip(out, **kw):
        calls["clip"].append(kw)
        open(out, "wb").write(b"clip")
        return out

    def fake_defect(out, **kw):
        calls["defect"].append(kw)
        open(out, "wb").write(b"defect")
        return out

    monkeypatch.setattr(providers, "render_clip", fake_clip)
    monkeypatch.setattr(providers, "render_defect", fake_defect)
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(settings, "dry_run_fail_rate", 0.0)
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.0)
    return calls


def request(seed=1, resolution="480p", duration=5.0, **extra):
    return GenRequest(prompt="p", seed=seed, resolution=resolution, duration_s=duration, extra=extra)


# ---------- yönlendirme ve fabrika ----------

@pytest.mark.parametrize("complexity,engine", [(None, "fal_wan"), (0.0, "fal_wan"), (0.69, "fal_wan"),
                                               (0.7, "seedance"), (0.8, "seedance"), (1.0, "seedance")])
def test_route_engine_by_action_complexity(complexity, engine):
    assert route_engine(NS(engine=None, action_complexity=complexity)) == engine


def test_route_engine_ignores_a_previously_stored_engine():
    """Regresyon: run_tiered_generation motoru sahneye yazar; eski karar yeni aksiyon yoğunluğunu ezmemeli."""
    assert route_engine(NS(engine="fal_wan", action_complexity=0.8)) == "seedance"
    assert route_engine(NS(engine="seedance", action_complexity=0.2)) == "fal_wan"


@pytest.mark.parametrize("engine", ["fal_wan", "seedance", "higgsfield"])
def test_dry_run_always_returns_the_free_provider(engine):
    assert settings.dry_run is True
    provider = get_provider(engine)
    assert isinstance(provider, LocalProvider) and provider.engine == engine


def test_free_mode_never_imports_the_paid_module():
    for engine in ("fal_wan", "seedance"):
        get_provider(engine)
    assert "app.live.fal_video" not in sys.modules


# ---------- sanal maliyet ----------

@pytest.mark.parametrize("engine,resolution,duration,cost", [
    ("fal_wan", "480p", 5.0, 0.2), ("fal_wan", "720p", 5.0, 0.4), ("seedance", "720p", 4.0, 0.968),
    ("fal_wan", "1080p", 5.0, 0.4),                       # bilinmeyen çözünürlük: motorun en pahalı fiyatı
])
def test_cost_is_the_real_provider_price(engine, resolution, duration, cost):
    assert LocalProvider(engine).estimate(request(resolution=resolution, duration=duration)) == cost


def test_generate_reports_virtual_cost_and_local_request_id(renders):
    result = LocalProvider("seedance").generate(request(resolution="720p", duration=4.0))
    assert result.cost_usd == 0.968 and result.engine == "seedance"
    assert result.request_id.startswith("local-") and os.path.exists(result.local_path)


# ---------- render parametreleri ----------

def test_generate_passes_scene_parameters_to_the_renderer(renders):
    cam = {"shot": "close_up", "movement": "dolly_in"}
    LocalProvider("fal_wan").generate(request(seed=7, duration=3.0, emotion="fear", camera=cam,
                                              characters=["Kerem"], action=0.8, label="S001_SH01"))
    (kw,) = renders["clip"]
    assert (kw["width"], kw["height"], kw["duration"], kw["seed"]) == (854, 480, 3.0, 7)
    assert kw["fps"] == settings.local_render_fps
    assert (kw["emotion"], kw["camera"], kw["characters"], kw["action"], kw["label"]) == \
        ("fear", cam, ["Kerem"], 0.8, "S001_SH01")


def test_unknown_resolution_renders_720p(renders):
    LocalProvider("fal_wan").generate(request(resolution="999p"))
    assert (renders["clip"][0]["width"], renders["clip"][0]["height"]) == (1280, 720)


# ---------- bozuk klip (Quality Gate'i sınamak için) ----------

def test_defect_rate_extremes(renders, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
    LocalProvider("fal_wan").generate(request())
    assert len(renders["defect"]) == 1 and not renders["clip"]
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.0)
    LocalProvider("fal_wan").generate(request())
    assert len(renders["clip"]) == 1


def test_defect_kinds_are_black_and_freeze(renders, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
    for seed in range(40):
        LocalProvider("fal_wan").generate(request(seed=seed))
    assert {c["kind"] for c in renders["defect"]} == {"black", "freeze"}


def test_default_defect_rate_is_about_12_percent(renders, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.12)
    for seed in range(500):
        LocalProvider("fal_wan").generate(request(seed=seed))
    assert 0.07 <= len(renders["defect"]) / 500 <= 0.17


def test_defect_decision_is_reproducible_per_seed(renders, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.5)
    outcomes = []
    for _ in range(2):
        before = len(renders["defect"])
        for seed in range(30):
            LocalProvider("fal_wan").generate(request(seed=seed))
        outcomes.append(len(renders["defect"]) - before)
    assert outcomes[0] == outcomes[1]


# ---------- yapay geçici hata ----------

def test_fail_rate_one_always_raises_transient_error(renders, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_fail_rate", 1.0)
    with pytest.raises(TransientError):
        LocalProvider("fal_wan").generate(request())
    assert not renders["clip"] and not renders["defect"]


def test_retrying_the_same_request_can_recover(renders, monkeypatch):
    """Regresyon: hata seed'e bağlıydı; aynı isteği yeniden denemek hep aynı hatayı veriyordu."""
    draws = iter([0.1, 0.9])                                  # önce hata (0.1 < 0.5), sonra başarı
    monkeypatch.setattr(settings, "dry_run_fail_rate", 0.5)
    monkeypatch.setattr(providers.random, "random", lambda: next(draws))
    req = request(seed=42)
    with pytest.raises(TransientError):
        LocalProvider("fal_wan").generate(req)
    assert LocalProvider("fal_wan").generate(req).request_id.startswith("local-")
