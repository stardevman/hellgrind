"""Fiyat tablosu ve tahmin fonksiyonları. Veritabanı gerektirmez."""
import pathlib

import pytest

from app.budget.pricing import estimate_lipsync, estimate_video, table

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
