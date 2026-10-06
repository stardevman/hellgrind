"""Sağlayıcı soyutlaması ve motor yönlendirmesi. Veritabanı gerektirmez."""
from types import SimpleNamespace as NS

import pytest

from app.budget import pricing
from app.config import settings
from app.engine import providers
from app.engine.providers import GenRequest, LocalProvider, TransientError, get_provider, route_engine
from tests.media_utils import anomalies, probe


@pytest.fixture
def small(monkeypatch, tmp_path):
    """Gerçek render hattı, küçük kareler; varsayılan: kusur ve geçici hata yok."""
    monkeypatch.setattr(providers, "RESOLUTIONS", {"480p": (160, 90), "720p": (192, 108)})
    for name, value in dict(work_dir=str(tmp_path), local_render_fps=12, dry_run_defect_rate=0.0,
                            dry_run_fail_rate=0.0).items():
        monkeypatch.setattr(settings, name, value)


def request(seed=1, resolution="480p", duration=3.0, **extra):
    return GenRequest(prompt="köprü", seed=seed, resolution=resolution, duration_s=duration, extra=extra)


# ---------- yönlendirme ----------

@pytest.mark.parametrize("complexity", [0.0, 0.3, 0.69, 0.7, 0.8, 1.0, None])
def test_every_new_shot_goes_to_minimax_h3_whatever_its_action(complexity):
    assert route_engine(NS(action_complexity=complexity)) == "h3"


@pytest.mark.parametrize("stored,routed", [(None, "h3"), ("h3", "h3"), ("seedance", "seedance"), ("fal_wan", "h3"),
                                           ("higgsfield", "h3")])
def test_only_a_seedance_chosen_by_the_director_survives_rerouting(stored, routed):
    assert route_engine(NS(action_complexity=0.9, engine=stored)) == routed


# ---------- get_provider ----------

@pytest.mark.parametrize("engine", ["h3", "fal_wan", "seedance", "higgsfield"])
def test_free_mode_always_returns_the_local_provider(monkeypatch, engine):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "fal_key", "gerçek-görünümlü-anahtar")     # anahtar tanımlı olsa bile
    provider = get_provider(engine)
    assert isinstance(provider, LocalProvider) and provider.engine == engine


# ---------- sanal maliyet ----------

@pytest.mark.parametrize("engine,resolution,seconds,expected", [
    ("fal_wan", "480p", 5.0, 0.2), ("fal_wan", "720p", 5.0, 0.4), ("seedance", "720p", 4.0, 0.968),
])
def test_cost_is_the_real_providers_price_even_though_nothing_is_paid(engine, resolution, seconds, expected):
    assert LocalProvider(engine).estimate(request(resolution=resolution, duration=seconds)) == pytest.approx(expected)


def test_unknown_resolution_is_priced_at_the_engines_most_expensive_rate():
    assert LocalProvider("fal_wan").estimate(request(resolution="4k", duration=5.0)) \
        == pricing.estimate_video("fal_wan", "720p", 5.0)


def test_generate_reports_the_estimate_as_its_cost(small):
    result = LocalProvider("seedance").generate(request(resolution="720p", duration=3.0))
    assert result.cost_usd == pytest.approx(0.968) and result.engine == "seedance"     # Seedance en az 4 sn üretir


@pytest.mark.parametrize("engine,seconds,billed", [("h3", 3.0, 4.0), ("h3", 4.2, 5.0), ("h3", 20.0, 20.0),
                                                   ("seedance", 2.0, 4.0), ("fal_wan", 3.0, 3.0)])
def test_engines_with_a_minimum_length_are_billed_for_what_they_render(engine, seconds, billed):
    assert providers.billed_seconds(engine, seconds) == billed


def test_h3_is_priced_by_gpu_time_on_the_rented_machine():
    hosted = pricing.self_hosted("h3")
    per_video_second = hosted["gpu_hourly_usd"] / 3600 * hosted["render_seconds"]["720p"]
    assert LocalProvider("h3").estimate(request(resolution="720p", duration=6.0)) == pytest.approx(round(per_video_second * 6, 4))
    assert pricing.estimate_gpu("h3", 3600) == hosted["gpu_hourly_usd"]


# ---------- üretim ----------

def test_generate_renders_a_real_clip_at_the_requested_resolution_and_duration(small):
    result = LocalProvider("fal_wan").generate(request(resolution="720p", duration=3.0, emotion="anger",
                                                       camera={"shot": "wide", "movement": "tracking"},
                                                       characters=["Kerem"], action=0.8, label="S001_SH01"))
    info = probe(result.local_path)
    assert (int(info["video"][0]["width"]), int(info["video"][0]["height"])) == (192, 108)
    assert info["duration"] == pytest.approx(3.0, abs=0.15)
    assert result.request_id.startswith("local-")
    assert anomalies(result.local_path) == {"black": False, "freeze": False}
    assert result.meta == {"planted_defect": None}


def test_every_request_gets_a_new_file_and_request_id(small):
    a, b = LocalProvider("fal_wan").generate(request(seed=1)), LocalProvider("fal_wan").generate(request(seed=1))
    assert a.local_path != b.local_path and a.request_id != b.request_id


def test_unknown_resolution_falls_back_to_720p_size(small, monkeypatch):
    monkeypatch.setattr(providers, "DEFAULT_SIZE", (128, 72))
    result = LocalProvider("fal_wan").generate(request(resolution="8k"))
    assert int(probe(result.local_path)["video"][0]["width"]) == 128


# ---------- kusurlu klipler ----------

def test_defect_rate_one_plants_a_defect_in_every_clip_and_the_ffmpeg_filters_see_it(small, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
    for seed in range(6):
        result = LocalProvider("fal_wan").generate(request(seed=seed))
        found = anomalies(result.local_path)
        assert result.meta["planted_defect"] in ("black", "freeze")
        assert found["black"] if result.meta["planted_defect"] == "black" else found["freeze"]


def test_defect_rate_zero_never_plants_one(small):
    assert {LocalProvider("fal_wan").generate(request(seed=s)).meta["planted_defect"] for s in range(5)} == {None}


def test_defect_decision_depends_on_the_seed_and_is_repeatable(small, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.5)
    run = lambda: [LocalProvider("fal_wan").generate(request(seed=s)).meta["planted_defect"] for s in range(10)]  # noqa: E731
    first = run()
    assert first == run()
    assert len(set(first)) > 1                            # hem bozuk hem sağlam çıkar


# ---------- yapay geçici hata ----------

def test_transient_failures_follow_the_configured_rate(small, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_fail_rate", 1.0)
    with pytest.raises(TransientError):
        LocalProvider("fal_wan").generate(request())


def test_a_transient_failure_does_not_depend_on_the_seed(small, monkeypatch):
    """Aynı isteği yeniden denemek gerçekte olduğu gibi kurtarabilmeli: seed'e bağlı olsaydı hep aynı hatayı alırdı."""
    outcomes = set()
    monkeypatch.setattr(settings, "dry_run_fail_rate", 0.5)
    for _ in range(40):
        try:
            LocalProvider("fal_wan").generate(request(seed=7, duration=1.0))
            outcomes.add("ok")
        except TransientError:
            outcomes.add("fail")
    assert outcomes == {"ok", "fail"}
