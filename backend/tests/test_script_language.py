"""Senaryo dili ve konuşmayan karakterler: [KARAKTER: ...] işareti, projede zaten olan karakterler ve İngilizce senaryo.

Görüntü modelleri tarifte adı geçen ama kadroda olmayan kişiyi çizmez: prompt "kimse yok" der. Bu yüzden konuşmayan
karakter de kadroya girmeli ve çekime bağlanmalı.
"""
from sqlalchemy import select

from app.hermes import rules
from app.style.composer import compose_prompt


def test_a_character_marker_puts_a_silent_character_in_the_cast_and_the_shot():
    r = rules.parse("DIŞ. ÇATI - GÜN BATIMI\n\nKerem çatının kenarında durur. [KARAKTER: Kerem]\n\nŞehir susar.\n")
    assert r["cast"] == ["Kerem"]
    assert r["shots"][0]["characters"] == ["Kerem"] and r["shots"][0]["description"] == "Kerem çatının kenarında durur."
    assert r["shots"][1]["characters"] == []


def test_a_character_marker_on_its_own_line_covers_every_shot_of_the_scene():
    r = rules.parse("İÇ. ODA - GECE\n\n[KARAKTER: Ayşe]\n\nIşık söner.\n\nKapı açılır.\n\nDIŞ. YOL - GÜN\n\nYol boş.\n")
    assert [s["characters"] for s in r["shots"]] == [["Ayşe"], ["Ayşe"], []]


def test_once_in_the_cast_a_silent_character_is_linked_wherever_named():
    r = rules.parse("İÇ. ODA - GECE\n\n[KARAKTER: Kerem]\n\nIşık söner.\n\nDIŞ. YOL - GÜN\n\nKerem'in gölgesi uzar.\n")
    assert r["shots"][1]["characters"] == ["Kerem"]


def test_known_project_characters_join_the_cast_only_when_named():
    text = "DIŞ. YOL - GÜN\n\nKerem yürür.\n"
    assert rules.parse(text)["cast"] == []
    assert rules.parse(text, known=["Kerem", "Ayşe"])["cast"] == ["Kerem"]
    assert rules.parse(text, known=["Kerem"])["shots"][0]["characters"] == ["Kerem"]


def test_the_silent_character_is_no_longer_forbidden_by_the_prompt():
    shot = rules.parse("EXT. ROOFTOP - SUNSET\n\n[CHARACTER: Kerem]\n\nKerem stands on a rooftop.\n")["shots"][0]
    out = compose_prompt({"prompt": shot["visual_prompt"], "characters": shot["characters"]}, [], None).prompt
    assert "Only the listed characters appear: Kerem." in out and "No people or characters" not in out


def test_markers_are_case_insensitive_and_have_english_names():
    clean, props, chars = rules.take_marks("Kerem gets in. [prop: taxi, Suitcase] [Character: kerem]")
    assert clean == "Kerem gets in." and props == ["Taxi", "Suitcase"] and chars == ["Kerem"]
    assert rules.take_props("Kerem biner [NESNE: Taksi] [KARAKTER: Kerem].") == ("Kerem biner.", ["Taksi"])


# ---------- İngilizce senaryo ----------

ENGLISH = """# SEQUENCE: Escape

INT. CITY PARK - NIGHT

Iris runs across the park, terrified, as the lamps explode behind her.

IRIS
(whispering)
Is anyone there?

Kai smiles and laughs.
"""


def test_an_english_script_parses_with_english_names_moods_and_action():
    r = rules.parse(ENGLISH)
    assert r["cast"] == ["Iris"] and r["sequences"] == [{"code": "SEQ01", "title": "Escape"}]
    first, second = r["shots"]                                         # replik, Iris'in çekimine bağlanır
    assert first["location"] == "City Park" and first["characters"] == ["Iris"] and first["action_complexity"] >= 0.7
    assert first["dialogue"][0]["character"] == "Iris" and first["dialogue"][0]["emotion"] == "tension"
    assert second["emotion"] == "joy" and second["action_complexity"] < 0.7 and second["characters"] == []
    assert rules.emotion_of("Iris runs across the park, terrified.") == "fear"


def test_english_casing_applies_only_to_scripts_without_turkish():
    assert rules.parse("INT. KITCHEN\n\nIRIS\nHello.\n")["cast"] == ["Iris"]
    assert rules.parse("İÇ. MUTFAK\n\nIŞIL\nMerhaba.\n")["cast"] == ["Işıl"]
    assert rules.location_name("INT. CITY PARK") == "City Park"
    assert rules.location_name("İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE") == "Terk Edilmiş Dökümhane"


def test_english_moods_do_not_misread_turkish_words():
    assert rules.emotion_of("Sadece bekler.") == "neutral"                # "sad" yalnızca tam kelime
    assert rules.emotion_of("She is sad.") == "sadness" and rules.emotion_of("He cries.") == "sadness"
    assert not rules.is_action("Kristal bir fal bakar.") and rules.is_action("The roof collapses.")


def test_the_turkish_sample_is_unchanged_by_the_english_rules():
    r = rules.parse(rules.SAMPLE_SCRIPT)
    assert r["cast"] == ["Kerem", "Ayşe"] and len(r["shots"]) == 4
    assert [s["emotion"] for s in r["shots"]][:2] == ["tension", "tension"] and r["shots"][2]["action_complexity"] >= 0.7


# ---------- ingest ----------

def test_ingest_records_the_script_language_and_links_known_silent_characters(pg, mk_project):
    from app.db import session_scope
    from app.hermes.ingest import ingest
    from app.models import Character, Scene, SceneCharacter
    pid = mk_project()
    ingest(pid, "EXT. ROOFTOP - SUNSET\n\n[CHARACTER: Kerem]\n\nKerem stands on a rooftop.\n")
    ingest(pid, "EXT. ROOFTOP - SUNSET\n\nKerem stands on a rooftop.\n\n"
                "DIŞ. YOL - GÜN\n\nKerem yürür.\n")            # işaret yok: Kerem projede zaten var
    with session_scope() as s:
        scenes = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        assert {c: sc.prompt_spec["lang"] for c, sc in scenes.items()} == {"S001_SH01": "tr", "S002_SH01": "tr"}
        linked = s.scalars(select(Character.name).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                           .where(SceneCharacter.scene_id == scenes["S002_SH01"].id)).all()
        assert linked == ["Kerem"]
    other = mk_project()
    ingest(other, "EXT. ROOFTOP - SUNSET\n\nThe city sleeps.\n")
    with session_scope() as s:
        assert s.scalar(select(Scene.prompt_spec).where(Scene.project_id == other))["lang"] == "en"
