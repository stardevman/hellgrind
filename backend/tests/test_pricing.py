"""Fiyat tablosu ve tahmin fonksiyonları. Veritabanı gerektirmez."""
import pathlib

import pytest

from app.budget.pricing import estimate_image, estimate_llm, estimate_lipsync, estimate_tts, estimate_video, table

PRICING_FILE = str(pathlib.Path(__file__).resolve().parents[1] / "pricing.yaml")


@pytest.fixture(scope="module")
def prices():
    return table(PRICING_FILE)


def test_table_matches_spec(prices):
    wan = prices["video_per_second"]["fal_wan"]
    assert (wan["480p"], wan["720p"]) == (0.04, 0.08)
    assert prices["video_per_second"]["seedance"]["720p"] == 0.242
    assert prices["video_per_second"]["higgsfield"]["720p"] == 0.25
    assert prices["image_per_megapixel"]["flux_lora"] == 0.035
    assert prices["lipsync_per_minute"] == 5.0


def test_video_cost_scales_with_resolution_and_duration(prices):
    assert estimate_video("fal_wan", "480p", 5, prices) == 0.2
    assert estimate_video("fal_wan", "720p", 5, prices) == 0.4
    assert estimate_video("seedance", "720p", 5, prices) == 1.21


def test_unknown_resolution_uses_most_expensive_price(prices):
    assert estimate_video("fal_wan", "1080p", 5, prices) == estimate_video("fal_wan", "720p", 5, prices)
    # seedance yalnızca 720p fiyatlıdır; 480p istenirse 720p fiyatı uygulanır
    assert estimate_video("seedance", "480p", 5, prices) == 1.21


def test_lipsync_is_billed_per_minute(prices):
    assert estimate_lipsync(60, prices) == 5.0
    assert estimate_lipsync(30, prices) == 2.5


def test_image_is_billed_per_started_megapixel(prices):
    assert estimate_image(0.59, prices) == 0.035          # 1024x576 -> 1 MP'e yuvarlanır
    assert estimate_image(1.0, prices) == 0.035
    assert estimate_image(1.01, prices) == 0.07


def test_tts_is_billed_per_character(prices):
    assert estimate_tts("a" * 1000, prices) == 0.3
    assert estimate_tts("", prices) == 0 and estimate_tts(None, prices) == 0


def test_llm_is_billed_per_token(prices):
    assert estimate_llm(1_000_000, 0, prices) == 3.0 and estimate_llm(0, 1_000_000, prices) == 15.0
    assert estimate_llm(2000, 1000, prices) == 0.021
