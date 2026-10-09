"""Hermes kural tabanlı ayrıştırıcı. Veritabanı ve dış servis gerektirmez."""
import pytest

from app.hermes import rules
from app.hermes.rules import parse, tr_lower, tr_title, tr_upper


def shots_of(text):
    return parse(text)["shots"]


# ---------- Türkçe büyük/küçük harf ----------

@pytest.mark.parametrize("raw,title", [("AYŞE", "Ayşe"), ("IŞIL", "Işıl"), ("İBRAHİM", "İbrahim"),
                                       ("ayşe", "Ayşe"), ("ışıl", "Işıl"), ("ALİ RIZA", "Ali Rıza"),
                                       ("KEREM-CAN", "Kerem-Can")])
def test_tr_title_handles_dotted_and_dotless_i(raw, title):
    assert tr_title(raw) == title


def test_tr_lower_and_upper_follow_turkish_rules():
    assert tr_lower("IŞIL İSTANBUL") == "ışıl istanbul"
    assert tr_upper("ışıl istanbul") == "IŞIL İSTANBUL"
    assert "̇" not in tr_lower("İ")                     # birleşik nokta kalmamalı


# ---------- örnek senaryo (Faz 4 kabulü) ----------

def test_sample_script_gives_four_shots_and_the_cast_in_speaking_order():
    parsed = parse(rules.SAMPLE_SCRIPT)
    assert parsed["cast"] == ["Kerem", "Ayşe"]
    assert [s["scene_id"] for s in parsed["shots"]] == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]


def test_sample_bridge_shots_are_high_action():
    bridge = [s for s in shots_of(rules.SAMPLE_SCRIPT) if s["scene_id"].startswith("S002")]
    assert bridge and all(s["action_complexity"] >= 0.7 for s in bridge)


def test_sample_dialogue_is_attached_to_the_shot_where_the_speaker_appears():
    shots = shots_of(rules.SAMPLE_SCRIPT)
    assert [[d["character"] for d in s["dialogue"]] for s in shots] == [["Kerem"], ["Ayşe"], ["Kerem"], ["Ayşe"]]
    assert shots[0]["dialogue"][0]["line"] == "Burada kimse yok mu?"


def test_sample_emotions_come_from_stems_in_hints_and_text():
    assert [s["emotion"] for s in shots_of(rules.SAMPLE_SCRIPT)] == ["tension", "tension", "fear", "anger"]


def test_every_shot_has_the_documented_fields():
    expected = {"scene_id", "sequence", "heading", "location", "props", "description", "visual_prompt", "camera",
                "emotion", "characters", "dialogue", "duration_s", "action_complexity", "transition"}
    for shot in shots_of(rules.SAMPLE_SCRIPT):
        assert set(shot) == expected
        assert set(shot["camera"]) == {"shot", "movement", "angle", "lens_mm"}
        assert all(set(d) == {"character", "line", "emotion"} for d in shot["dialogue"])


def test_parsing_is_deterministic():
    assert parse(rules.SAMPLE_SCRIPT) == parse(rules.SAMPLE_SCRIPT)


# ---------- sahne başlıkları ----------

@pytest.mark.parametrize("heading", ["İÇ. MUTFAK - GÜN", "DIŞ. SOKAK", "İÇ/DIŞ. ARABA", "INT. KITCHEN - DAY",
                                     "EXT. STREET", "iç. mutfak"])
def test_slug_lines_start_a_new_scene(heading):
    text = f"Bir şey olur.\n\n{heading}\n\nBaşka bir şey olur."
    assert [s["scene_id"] for s in shots_of(text)] == ["S001_SH01", "S002_SH01"]
    assert shots_of(text)[1]["heading"] == heading


def test_ordinary_sentences_are_not_slug_lines():
    text = "Dışarıda yağmur var. İçeride kimse yok."
    assert len(shots_of(text)) == 1 and shots_of(text)[0]["heading"] == "SAHNE"


def test_text_before_any_heading_goes_to_a_default_scene():
    assert shots_of("Sessizlik.")[0]["scene_id"] == "S001_SH01"


def test_transitions_are_skipped():
    text = "İÇ. ODA\n\nKapı açılır.\n\nKESME:\n\nDIŞ. SOKAK\n\nYağmur yağar."
    assert [s["description"] for s in shots_of(text)] == ["Kapı açılır.", "Yağmur yağar."]


def test_windows_line_endings_parse_the_same():
    assert parse(rules.SAMPLE_SCRIPT.replace("\n", "\r\n")) == parse(rules.SAMPLE_SCRIPT)


# ---------- konuşan karakter ----------

def test_cue_extensions_are_dropped_from_the_name():
    parsed = parse("İÇ. ODA\n\nKEREM (D.S.)\nBeni duyuyor musun?\n")
    assert parsed["cast"] == ["Kerem"]


def test_cue_limits_four_words_and_forty_characters():
    assert rules.is_cue("ÇOK UZUN BİR KARAKTER")                       # 4 kelime
    assert not rules.is_cue("ÇOK UZUN BİR KARAKTER ADI")               # 5 kelime
    assert not rules.is_cue("A" * 41)
    assert rules.is_cue("A" * 40)
    assert not rules.is_cue("Kerem")                                    # büyük harfli değil
    assert not rules.is_cue("KOŞ!")                                     # bağırma, karakter adı değil
    assert not rules.is_cue("(tedirgin)")


def test_uppercase_line_without_dialogue_after_it_is_action():
    shots = shots_of("İÇ. ODA\n\nSESSİZLİK\n\nKerem bekler.")
    assert [s["description"] for s in shots] == ["SESSİZLİK", "Kerem bekler."]


def test_parenthesis_line_is_an_emotion_hint_not_dialogue_text():
    shot = shots_of("İÇ. ODA\n\nKEREM\n(korkuyla)\nGeliyorlar.\n")[0]
    assert shot["dialogue"] == [{"character": "Kerem", "line": "Geliyorlar.", "emotion": "fear"}]


# ---------- diyaloğun çekime bağlanması ----------

SCENE = "İÇ. ODA\n\n"


def test_speaker_present_in_last_shot_joins_it_up_to_two_lines():
    text = SCENE + "Kerem pencereye bakar.\n\nKEREM\nBir.\n\nKEREM\nİki.\n\nKEREM\nÜç.\n"
    shots = shots_of(text)
    assert [len(s["dialogue"]) for s in shots] == [2, 1]
    assert shots[1]["description"].startswith("Kerem konuşuyor")


def test_speaker_missing_from_last_shot_opens_a_close_up():
    text = SCENE + "Kerem pencereye bakar.\n\nAYŞE\nNe bakıyorsun?\n"
    shots = shots_of(text)
    assert len(shots) == 2
    assert shots[1]["camera"]["shot"] == "close_up" and shots[1]["characters"] == ["Ayşe"]


def test_dialogue_longer_than_the_time_budget_opens_a_new_shot():
    long_line = " ".join(["kelime"] * 14) + "."          # 14/2,5 + 0,35 = 5,95 sn
    text = SCENE + f"Kerem bekler.\n\nKEREM\n{long_line}\n\nKEREM\n{long_line}\n"
    shots = shots_of(text)
    assert [len(s["dialogue"]) for s in shots] == [1, 1]


def test_a_single_speech_over_the_budget_is_split_at_sentence_boundaries():
    sentence = " ".join(["kelime"] * 12) + "."
    text = SCENE + f"Kerem bekler.\n\nKEREM\n{sentence} {sentence} {sentence}\n"
    shots = shots_of(text)
    spoken = [d["line"] for s in shots for d in s["dialogue"]]
    assert len(spoken) == 3 and all(len(line.split()) == 12 for line in spoken)
    assert all(s["duration_s"] <= 10 for s in shots)


def test_each_action_paragraph_is_its_own_shot():
    shots = shots_of(SCENE + "Kapı açılır.\n\nIşık yanar.\n\nBir gölge geçer.")
    assert [s["description"] for s in shots] == ["Kapı açılır.", "Işık yanar.", "Bir gölge geçer."]


def test_lines_of_one_paragraph_are_joined():
    shots = shots_of(SCENE + "Kapı açılır.\nIşık yanar.\n")
    assert [s["description"] for s in shots] == ["Kapı açılır. Işık yanar."]


# ---------- duygu ----------

@pytest.mark.parametrize("text,emotion", [
    ("Çok korkuyor.", "fear"), ("Öfkeyle bağırır.", "anger"), ("Tedirgin bekler.", "tension"),
    ("Gergin bir sessizlik.", "tension"), ("Mutlu bir kahkaha atar.", "joy"), ("Gözyaşlarını tutamaz.", "sadness"),
    ("Şaşkınlıkla bakar.", "surprise"), ("İğrenerek geri çekilir.", "disgust"), ("Odaya girer.", "neutral"),
])
def test_emotion_from_turkish_stems(text, emotion):
    assert rules.emotion_of(text) == emotion


def test_stems_match_only_at_word_start():
    assert rules.emotion_of("Türkiye haritası duvarda asılı.") == "neutral"      # "ürk" korku sayılmaz
    assert rules.emotion_of("Yasak bölgeye girer.") == "neutral"                  # "yas" üzüntü sayılmaz


def test_cast_names_are_not_mistaken_for_emotion_or_action_stems():
    assert rules.emotion_of("Gül kapıyı açar.", names=["Gül"]) == "neutral"
    assert not rules.is_action("Ateş pencereye bakar.", names=["Ateş"])
    assert rules.emotion_of("Gül kapıyı açar.") == "joy"                          # ad bilinmiyorsa kök sayılır


def test_emotion_carries_over_within_a_scene_and_resets_at_a_new_scene():
    text = "İÇ. ODA\n\nKerem çok korkar.\n\nKapı kapanır.\n\nİÇ. SOKAK\n\nKapı açılır."
    assert [s["emotion"] for s in shots_of(text)] == ["fear", "fear", "neutral"]


# ---------- kamera ve aksiyon ----------

@pytest.mark.parametrize("text", ["Kerem koşar.", "Köprü sarsılır.", "Halat kopar.", "Bina patlar.",
                                  "Kerem yere düşer.", "Ayşe atlar."])
def test_action_words_make_a_wide_tracking_or_handheld_shot(text):
    shot = shots_of(SCENE + "Sessizlik.\n\n" + text)[1]
    assert shot["camera"]["shot"] == "wide" and shot["camera"]["movement"] in ("handheld", "tracking")
    assert shot["action_complexity"] == 0.8


@pytest.mark.parametrize("text", ["Kerem düşünüyor.", "Düşman görünmez.", "Bu koşullar altında bekler.",
                                  "Duvarda bir atlas asılı.", "Kopya kağıdı masada."])
def test_words_that_merely_start_like_action_stems_are_not_action(text):
    assert not rules.is_action(text)


def test_handheld_when_the_emotion_is_intense_otherwise_tracking():
    calm = shots_of(SCENE + "Sessizlik.\n\nKerem koşar.")[1]
    panic = shots_of(SCENE + "Sessizlik.\n\nKerem korkuyla koşar.")[1]
    assert (calm["camera"]["movement"], panic["camera"]["movement"]) == ("tracking", "handheld")


def test_first_shot_of_a_scene_is_wide_with_a_pan_and_every_scene_restarts_that_rule():
    shots = shots_of("İÇ. ODA\n\nDuvarlar çıplak.\n\nBir masa var.\n\nİÇ. SOKAK\n\nYağmur yağar.")
    assert [(s["camera"]["shot"], s["camera"]["movement"]) for s in shots] == [("wide", "pan"), ("medium", "static"),
                                                                              ("wide", "pan")]


def test_action_wins_over_the_scene_opening_rule():
    shot = shots_of(SCENE + "Kerem koşar.")[0]
    assert shot["action_complexity"] == 0.8 and shot["camera"]["movement"] != "pan"


def test_dialogue_only_shot_is_a_close_up_and_pushes_in_when_intense():
    calm = shots_of(SCENE + "Ayşe bekler.\n\nKEREM\nMerhaba.\n")[1]
    tense = shots_of(SCENE + "Ayşe bekler.\n\nKEREM\n(öfkeyle)\nMerhaba.\n")[1]
    assert (calm["camera"]["shot"], calm["camera"]["movement"]) == ("close_up", "static")
    assert (tense["camera"]["shot"], tense["camera"]["movement"]) == ("close_up", "dolly_in")


# ---------- süre ----------

def test_duration_is_clamped_between_three_and_ten_seconds():
    assert shots_of(SCENE + "Kerem bekler.\n\nKEREM\nEvet.\n")[0]["duration_s"] == 3.0
    long_action = " ".join(["kelime"] * 100)
    assert shots_of(SCENE + long_action)[0]["duration_s"] <= 10.0


def test_dialogue_duration_is_words_per_2_5_plus_gaps_rounded_up_to_half_seconds():
    line = " ".join(["kelime"] * 10)                      # 4,0 + 0,35 + 1,0 pay = 5,35 -> 5,5
    shot = shots_of(SCENE + f"Kerem bekler.\n\nKEREM\n{line}\n")[0]
    assert shot["duration_s"] == 5.5


def test_small_dialogue_edits_do_not_change_the_duration():
    a = shots_of(SCENE + "Kerem bekler.\n\nKEREM\nBir iki üç dört beş altı.\n")[0]
    b = shots_of(SCENE + "Kerem bekler.\n\nKEREM\nBir iki üç dört beş yedi.\n")[0]
    assert a["duration_s"] == b["duration_s"]


# ---------- görsel istem ----------

def test_visual_prompt_never_contains_the_spoken_text():
    text = SCENE + "Ayşe bekler.\n\nKEREM\nGizli replik burada.\n"
    for shot in shots_of(text):
        assert "Gizli replik" not in shot["visual_prompt"]


def test_editing_a_line_leaves_the_visual_prompt_untouched():
    a = shots_of(SCENE + "Ayşe bekler.\n\nKEREM\nBirinci söz.\n")
    b = shots_of(SCENE + "Ayşe bekler.\n\nKEREM\nİkinci söz.\n")
    assert [s["visual_prompt"] for s in a] == [s["visual_prompt"] for s in b]
    assert a[1]["description"] != b[1]["description"]          # panelde gösterilen açıklama ise değişir


def test_visual_prompt_starts_with_the_location():
    assert shots_of("İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE\n\nKerem girer.")[0]["visual_prompt"].startswith(
        "TERK EDİLMİŞ DÖKÜMHANE - GECE.")
