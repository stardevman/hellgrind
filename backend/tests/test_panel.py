"""Panel (tek statik HTML) için spesifikasyon bekçileri. Tarayıcı gerekmez; gerçek tarayıcı doğrulaması ayrıca yapılır."""
import pathlib
import re

import pytest

HTML = (pathlib.Path(__file__).resolve().parents[1] / "app" / "panel" / "index.html").read_text()

PALETTE = {"slag": "#2A2D31", "plate": "#33373C", "ash": "#E6E1D8", "dust": "#A39E95",
           "ember": "#E4A23E", "verdigris": "#62AE8E", "oxide": "#C2573F", "steel": "#86A5B8"}

UI_TEXT = ["Ücretsiz mod: hiçbir ücretli servis çağrılmaz", "Yeni proje", "Üretimi durdur", "Üretime devam et",
           "Bunu kullan", "Müziği değiştir", "Bu sahneyi baştan üret", "Senaryoyu işle ve üret", "sahneyi birleştir",
           "Yalnızca montaj; görüntü motoru çağrılmadı", "Yalnızca ses ve montaj; görüntü motoru çağrılmadı"]


def test_it_is_one_self_contained_page_without_a_build_step():
    assert HTML.lstrip().startswith("<!doctype html>") and '<html lang="tr">' in HTML
    assert '<meta name="viewport"' in HTML
    assert not re.search(r"<script[^>]+src=", HTML)                                   # dış betik yok
    stylesheets = re.findall(r'<link[^>]+rel="stylesheet"[^>]*>', HTML)
    assert all("fonts.googleapis.com" in s for s in stylesheets)                       # tek dış kaynak: yazı tipi
    assert "/api" in HTML and "fetch(" in HTML


@pytest.mark.parametrize("name,hex_", PALETTE.items())
def test_palette_colours_are_defined(name, hex_):
    assert re.search(rf"--{name}:\s*{hex_}", HTML, re.IGNORECASE)


def test_font_is_archivo_with_the_width_axis():
    assert "family=Archivo:wdth" in HTML and "font-stretch" in HTML and "Archivo" in HTML


def test_no_uppercase_labels():
    assert "text-transform" not in HTML.replace("text-transform:none", "")


def test_accessibility_and_responsive_rules_exist():
    assert ":focus-visible" in HTML
    assert "@media (prefers-reduced-motion:reduce)" in HTML
    assert re.search(r"@media \(max-width:\d+px\)", HTML)


def test_overview_is_polled_every_two_seconds():
    assert re.search(r"setInterval\([^;]*2000\)", HTML)


@pytest.mark.parametrize("text", UI_TEXT)
def test_required_turkish_texts(text):
    assert text in HTML


def test_user_supplied_text_is_escaped_before_it_reaches_innerhtml():
    """Senaryodan gelen ad/açıklama/replik metinleri esc() ile geçmeden şablona girmemeli."""
    for expr in ("esc(s.scene_code)", "esc(s.description)", "esc(t.text)", "esc(s.heading", "esc(p.title)", "esc(c.description)"):
        assert expr in HTML, expr
    assert re.search(r"\$\{(?!esc\()s\.description\}", HTML) is None


def test_film_button_counts_only_finished_scenes():
    """Montaj yalnızca status=done sahneleri birleştirir; düğme de aynı sayıyı göstermeli."""
    assert 's.status === "done" && s.final_url' in HTML
