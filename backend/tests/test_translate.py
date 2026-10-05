"""Prompt çevirisi: Türkçe tarifler sağlayıcıya gitmeden İngilizceye çevrilir, karakter adları korunur.

Birim testleri sahte bir çevirmenle koşar (model gerekmez). Gerçek modelli testler model imajda yoksa (CI) atlanır.
"""
from types import SimpleNamespace as N

import pytest

from app.config import settings
from app.hermes import translate
from app.style.composer import compose_prompt


@pytest.fixture
def fake(monkeypatch):
    """Sözlükten çeviren sahte model; gelen her girdiyi kaydeder. Bilinmeyen girdi "EN(...)" olur."""
    table: dict[str, str] = {}
    seen: list[str] = []

    def run(segments):
        seen.extend(segments)
        return [table.get(s, f"EN({s})") for s in segments]
    monkeypatch.setattr(settings, "prompt_translation", "on")
    monkeypatch.setattr(translate, "_translate", run)
    monkeypatch.setattr(translate, "unavailable", lambda: None)
    return N(table=table, seen=seen)


# ---------- dil sezgisi ----------

@pytest.mark.parametrize("text,turkish", [
    ("Kerem çatının kenarında durup sakin şehre bakıyor.", True),
    ("Köprü sarsılır.", True),
    ("Kerem stands on a rooftop at sunset, looking over a calm city.", False),
    ("Ayşe looks at the sky.", False),                       # Türkçe ad, İngilizce cümle
    ("Camera: wide shot, pan, eye-level, 24 mm lens.", False),
])
def test_turkish_sentences_are_told_apart_from_english_ones(text, turkish):
    assert translate.looks_turkish(text) is turkish


def test_a_known_turkish_script_translates_short_sentences_without_turkish_letters():
    assert translate.needs_translation("Taksi gelir.", "tr") and not translate.needs_translation("Taksi gelir.", None)
    assert not translate.needs_translation("Kerem looks at the city.", "tr")
    assert not translate.needs_translation("— 24 —", "tr")


# ---------- açma kapama ----------

def test_translation_is_off_in_free_mode_by_default(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "auto")
    monkeypatch.setattr(settings, "dry_run", True)
    assert translate.to_english("Köprü sarsılır.", lang="tr") == "Köprü sarsılır."
    monkeypatch.setattr(settings, "dry_run", False)
    assert translate.enabled()
    monkeypatch.setattr(settings, "prompt_translation", "off")
    assert not translate.enabled()


def test_turkish_prompt_without_a_model_stops_before_any_spend(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "on")
    monkeypatch.setattr(translate, "unavailable", lambda: "çeviri modeli yok: /models/mt")
    with pytest.raises(translate.TranslationUnavailable, match="İngilizce"):
        translate.to_english("Köprü sarsılır.", lang="tr")
    assert translate.to_english("The bridge shakes.", lang="en") == "The bridge shakes."   # İngilizce senaryo çalışır


# ---------- çeviri ----------

def test_only_turkish_sentences_are_sent_to_the_model(fake):
    fake.table["Köprü sarsılır."] = "The bridge shakes."
    out = translate.to_english("Köprü sarsılır. The city sleeps in the fog.", lang="tr")
    assert out == "The bridge shakes. The city sleeps in the fog." and set(fake.seen) == {"Köprü sarsılır."}


def test_all_caps_headings_are_lowered_before_translation(fake):
    fake.table["Çöken köprü - şafak."] = "Collapsing bridge - dawn."
    assert translate.to_english("ÇÖKEN KÖPRÜ - ŞAFAK.", lang="tr") == "Collapsing bridge - dawn."


def test_the_glossary_fixes_a_phrase_the_model_misreads(fake):
    fake.table["Kerem yakın çekimde konuşuyor."] = "Kerem is talking in close-up."
    assert translate.to_english("Kerem yakın planda konuşuyor.", ["Kerem"], "tr") == "Kerem is talking in close-up."


def test_a_name_lost_in_translation_is_retried_with_a_harmonious_stand_in(fake):
    fake.table["Gül sevinçle güler."] = "The rose laughs with joy."          # ad bir kelime olarak çevrildi
    fake.table["Mert sevinçle güler."] = "Mert laughs with joy."             # "Gül": ince ünlü, ünsüzle biter
    assert translate.to_english("Gül sevinçle güler.", ["Gül"], "tr") == "Gül laughs with joy."


def test_suffixes_keep_vowel_harmony_with_the_stand_in(fake):
    fake.table["Ayşe kenara atlar ve Kerem'in elini yakalar."] = "He jumps and grabs Kerem's hand."
    fake.table["Hande kenara atlar ve Mert'in elini yakalar."] = "Hande jumps and grabs Mert's hand."
    out = translate.to_english("Ayşe kenara atlar ve Kerem'in elini yakalar.", ["Kerem", "Ayşe"], "tr")
    assert out == "Ayşe jumps and grabs Kerem's hand."
    assert "Hande kenara atlar ve Mert'in elini yakalar." in fake.seen          # Kerem'in -> Mert'in (ince, ünsüz)


def test_if_no_stand_in_survives_the_plain_translation_is_kept(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "on")
    monkeypatch.setattr(translate, "unavailable", lambda: None)
    monkeypatch.setattr(translate, "_translate",
                        lambda segs: ["The light comes in." if s == "Işıl içeri girer." else "Someone comes in." for s in segs])
    assert translate.to_english("Işıl içeri girer.", ["Işıl"], "tr") == "The light comes in."


def test_old_encoding_artifacts_are_repaired():
    assert "Ayþe".translate(translate.LEGACY) == "Ayşe" and "Ýstanbul".translate(translate.LEGACY) == "İstanbul"


# ---------- besteci ----------

def test_the_composer_translates_descriptions_and_places_but_never_character_names(fake):
    fake.table.update({"Terk edilmiş dökümhane.": "Abandoned foundry.", "Gül yavaşça içeri girer.": "Gül walks in slowly.",
                       "Kızıl saçlı genç bir kadın": "A young woman with red hair", "Taksi.": "Taxi."})
    loc = N(type="location", name="Terk Edilmiş Dökümhane", description=None, render_style="painterly")
    ch = N(type="character", name="Gül", description="Kızıl saçlı genç bir kadın", render_style="clean_3d")
    prop = N(type="prop", name="Taksi", description=None, render_style="clean_3d")
    out = compose_prompt({"prompt": "Gül yavaşça içeri girer.", "characters": ["Gül"], "lang": "tr",
                          "camera": {"shot": "wide"}}, [loc, ch, prop], None).prompt
    assert out.startswith("Gül walks in slowly. Camera: wide shot.")
    assert "Location Abandoned foundry:" in out and "Character Gül: A young woman with red hair" in out
    assert "Prop Taxi:" in out and out.endswith("Only the listed characters appear: Gül. No other people or figures.")


def test_an_english_script_reaches_the_provider_untouched(fake):
    loc = N(type="location", name="Rooftop", description=None, render_style=None)
    shot = {"prompt": "ROOFTOP - SUNSET. Kerem stands on a rooftop at sunset.", "characters": ["Kerem"], "lang": "en"}
    out = compose_prompt(shot, [loc], None).prompt
    assert out.startswith("ROOFTOP - SUNSET. Kerem stands on a rooftop at sunset.") and fake.seen == []


def test_free_mode_prompts_are_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "auto")
    monkeypatch.setattr(settings, "dry_run", True)
    loc = N(type="location", name="Çatı", description=None, render_style=None)
    out = compose_prompt({"prompt": "Kerem şehre bakıyor.", "characters": ["Kerem"], "lang": "tr"}, [loc], None).prompt
    assert out.startswith("Kerem şehre bakıyor.") and "Location Çatı:" in out


# ---------- gerçek model (imajda; CI'da atlanır) ----------

real = pytest.mark.skipif(translate.unavailable() is not None, reason="çeviri modeli kurulu değil")


@real
def test_the_real_model_translates_a_shot_and_keeps_names(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "on")
    out = translate.to_english("Kerem çatının kenarında durup sakin şehre bakıyor.", ["Kerem"], "tr")
    assert "Kerem" in out and "roof" in out.lower() and "city" in out.lower()
    out = translate.to_english("Ayşe karşı kenara atlar ve Kerem'in elini yakalar.", ["Kerem", "Ayşe"], "tr")
    assert "Ayşe" in out and "Kerem" in out
    assert translate.to_english("Gül sevinçle güler.", ["Gül"], "tr").startswith("Gül")
    assert translate.to_english_name("Terk Edilmiş Dökümhane", "tr").lower() == "abandoned foundry"


@real
def test_the_real_model_is_deterministic(monkeypatch):
    monkeypatch.setattr(settings, "prompt_translation", "on")
    text = "Kızıl bir ışık paslı kazanların arasından süzülüyor."
    first = translate.to_english(text, lang="tr")
    translate._cache.clear()
    assert translate.to_english(text, lang="tr") == first
