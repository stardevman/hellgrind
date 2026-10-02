"""Hermes kural tabanlı ayrıştırıcı. Veritabanı gerektirmez."""
import pytest

from app.hermes.rules import SAMPLE_SCRIPT, emotion_of, is_cue, parse, tr_lower, tr_title

SHOT_KEYS = {"scene_id", "heading", "description", "visual_prompt", "camera", "emotion", "characters",
             "dialogue", "duration_s", "action_complexity"}


def shots_of(text):
    return parse(text)["shots"]


# ---------- Türkçe harf dönüşümleri ----------

def test_turkish_title_case():
    assert tr_title("AYŞE") == "Ayşe"
    assert tr_title("IŞIL") == "Işıl"
    assert tr_title("İPEK") == "İpek"
    assert tr_title("MEHMET ALİ") == "Mehmet Ali"


def test_turkish_lower_case():
    assert tr_lower("KIRIK") == "kırık"
    assert tr_lower("İSTANBUL") == "istanbul"


# ---------- konuşan karakter ve başlık tespiti ----------

def test_cue_detection():
    assert is_cue("KEREM")
    assert is_cue("AYŞE (D.S.)")
    assert not is_cue("İÇ. DÖKÜMHANE - GECE")
    assert not is_cue("Kerem içeri girer.")


def test_cue_limits_are_four_words_and_forty_characters():
    assert is_cue("BİR İKİ ÜÇ DÖRT")
    assert not is_cue("BİR İKİ ÜÇ DÖRT BEŞ")
    assert not is_cue("A" * 41)
    assert is_cue("A" * 40)


def test_transitions_are_not_cues_and_produce_no_shots():
    assert not is_cue("KESME:")
    assert not is_cue("GEÇİŞ:")
    r = parse("İÇ. ODA - GECE\n\nKerem oturur.\n\nKESME:\n\nDIŞ. SOKAK - GÜN\n\nSessiz bir sokak.\n")
    assert r["cast"] == []
    assert [s["scene_id"] for s in r["shots"]] == ["S001_SH01", "S002_SH01"]


@pytest.mark.parametrize("heading", ["İÇ. ODA", "DIŞ. SOKAK", "İÇ/DIŞ. ARABA", "INT. ROOM", "EXT. STREET"])
def test_every_heading_form_opens_a_scene(heading):
    r = parse(f"İÇ. İLK - GÜN\n\nKerem oturur.\n\n{heading} - GECE\n\nAyşe bekler.\n")
    assert [s["scene_id"] for s in r["shots"]] == ["S001_SH01", "S002_SH01"]
    assert r["shots"][1]["heading"].startswith(heading)


def test_cue_extension_is_dropped_from_cast_name():
    r = parse("İÇ. ODA - GECE\n\nAYŞE (D.S.)\nBuradayım.\n")
    assert r["cast"] == ["Ayşe"]
    assert r["shots"][0]["dialogue"][0]["character"] == "Ayşe"


def test_script_without_heading_still_parses_into_a_default_scene():
    r = parse("Kerem oturur.\n")
    assert r["shots"][0]["scene_id"] == "S001_SH01" and r["shots"][0]["heading"] == "SAHNE"


def test_heading_without_shots_does_not_consume_a_scene_number():
    r = parse("İÇ. BOŞ ODA\n\nDIŞ. SOKAK - GÜN\n\nSessiz bir sokak.\n")
    assert [s["scene_id"] for s in r["shots"]] == ["S001_SH01"]


def test_crlf_and_lf_inputs_are_equivalent():
    assert parse(SAMPLE_SCRIPT.replace("\n", "\r\n")) == parse(SAMPLE_SCRIPT)


# ---------- duygu ----------

@pytest.mark.parametrize("text,emotion", [
    ("Korkuyla geri çekilir.", "fear"),
    ("Öfkeyle yumruğunu sıkar.", "anger"),
    ("Birden bağırır.", "anger"),
    ("Tedirgin bir bakış atar.", "tension"),
    ("Ortam gergin.", "tension"),
    ("Yas tutuyor.", "sadness"),
    ("Sıradan bir gün.", "neutral"),
])
def test_emotion_from_turkish_stems(text, emotion):
    assert emotion_of(text) == emotion


@pytest.mark.parametrize("text", ["Türkiye haritası duvarda asılı.", "Kerem Türkçe konuşur.", "Yasak bölgeye girerler."])
def test_emotion_stems_do_not_match_inside_words(text):
    assert emotion_of(text) == "neutral"


def test_cast_names_are_not_emotion_stems():
    assert emotion_of("Gül çimende oturur.") == "joy"                   # kadro bilinmiyorsa kök sayılır
    assert emotion_of("Gül çimende oturur.", names=["Gül"]) == "neutral"
    assert emotion_of("Gül'ün yüzünde korku var.", names=["Gül"]) == "fear"


def test_character_named_gul_does_not_turn_every_shot_joyful():
    shots = shots_of("İÇ. BAHÇE - GÜN\n\nGül çimenlerin üzerinde oturur.\n\nGÜL\nHava çok durgun.\n")
    assert all(s["emotion"] == "neutral" for s in shots)


def test_parenthetical_sets_dialogue_and_shot_emotion():
    shot = shots_of("İÇ. ODA - GECE\n\nKerem kapıda bekler.\n\nKEREM\n(korkuyla)\nBurada kimse var mı?\n")[0]
    assert shot["dialogue"][0]["emotion"] == "fear"
    assert shot["emotion"] == "fear"


def test_emotion_carries_over_to_following_shots_in_the_scene():
    shots = shots_of("İÇ. ODA - GECE\n\nKerem korkuyla bekler.\n\nPencere açık duruyor.\n")
    assert [s["emotion"] for s in shots] == ["fear", "fear"]


# ---------- kamera ve aksiyon kuralları ----------

def test_action_words_give_wide_tracking_shot_with_high_complexity():
    cam = shots_of("İÇ. YOL - GÜN\n\nKerem koşar.\n")[0]
    assert cam["action_complexity"] == 0.8
    assert cam["camera"]["shot"] == "wide" and cam["camera"]["movement"] == "tracking"


def test_frightened_or_angry_action_is_handheld():
    shot = shots_of("İÇ. YOL - GÜN\n\nKerem korkuyla koşar.\n")[0]
    assert shot["camera"]["movement"] == "handheld"


@pytest.mark.parametrize("word", ["koşar", "atlar", "patlar", "düşer", "kopar", "sarsılır"])
def test_spec_action_words(word):
    assert shots_of(f"İÇ. YOL - GÜN\n\nBir şey {word}.\n")[0]["action_complexity"] >= 0.7


def test_character_named_ates_is_not_an_action():
    shot = shots_of("İÇ. ODA - GECE\n\nAteş pencerenin önünde oturur.\n\nATEŞ\nHava çok durgun.\n")[0]
    assert shot["action_complexity"] < 0.7
    assert shots_of("İÇ. YOL - GÜN\n\nAsker ateş eder.\n")[0]["action_complexity"] == 0.8


def test_first_shot_of_scene_is_wide_pan():
    shot = shots_of("İÇ. ODA - GECE\n\nSessiz bir oda.\n")[0]
    assert shot["camera"]["shot"] == "extreme_wide" and shot["camera"]["movement"] == "pan"


def test_first_shot_with_known_character_is_wide_pan():
    shot = shots_of("İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\nMerhaba.\n")[0]
    assert shot["camera"]["shot"] == "wide" and shot["camera"]["movement"] == "pan"


def test_later_non_action_paragraph_is_medium():
    shots = shots_of("İÇ. ODA - GECE\n\nSessiz bir oda.\n\nMasanın üstünde bir mektup duruyor.\n")
    assert shots[1]["camera"]["shot"] == "medium"


def test_dialogue_only_shot_is_close_up_and_dollies_in_when_intense():
    calm = shots_of("İÇ. ODA - GECE\n\nAYŞE\nBuradayım.\n")[0]
    tense = shots_of("İÇ. ODA - GECE\n\nAYŞE\n(gergin)\nBuradayım.\n")[0]
    assert (calm["camera"]["shot"], calm["camera"]["movement"]) == ("close_up", "static")
    assert (tense["camera"]["shot"], tense["camera"]["movement"]) == ("close_up", "dolly_in")


# ---------- diyaloğun çekime bağlanması ----------

def test_dialogue_attaches_to_last_shot_up_to_two_lines():
    script = ("İÇ. ODA - GECE\n\nKerem masaya oturur.\n\nKEREM\nBir.\n\nKEREM\nİki.\n\nKEREM\nÜç.\n\nAYŞE\nDört.\n")
    shots = shots_of(script)
    assert [[d["line"] for d in s["dialogue"]] for s in shots] == [["Bir.", "İki."], ["Üç."], ["Dört."]]
    assert [s["camera"]["shot"] for s in shots[1:]] == ["close_up", "close_up"]


def test_speaker_missing_from_last_shot_opens_new_close_up():
    shots = shots_of("İÇ. ODA - GECE\n\nKerem oturur.\n\nAYŞE\nMerhaba.\n")
    assert len(shots) == 2 and shots[1]["characters"] == ["Ayşe"] and shots[1]["camera"]["shot"] == "close_up"


def test_dialogue_longer_than_8_5_seconds_opens_new_shot():
    line = " ".join(["kelime"] * 14) + "."          # 14 / 2.5 + 0.35 = 5.95 sn; iki replik 11.9 sn
    shots = shots_of(f"İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\n{line}\n\nKEREM\n{line}\n")
    assert [len(s["dialogue"]) for s in shots] == [1, 1]


def test_multi_line_speech_is_joined_into_one_line():
    shot = shots_of("İÇ. ODA - GECE\n\nAYŞE\nBirinci satır\nikinci satır.\n")[0]
    assert shot["dialogue"][0]["line"] == "Birinci satır ikinci satır."


# ---------- süre ----------

def test_duration_is_clamped_between_3_and_10_seconds():
    short = shots_of("İÇ. ODA - GECE\n\nAYŞE\nEvet.\n")[0]
    long_line = shots_of("İÇ. ODA - GECE\n\nAYŞE\n" + " ".join(["kelime"] * 40) + ".\n")[0]
    long_action = shots_of("İÇ. ODA - GECE\n\n" + " ".join(["sessiz"] * 120) + ".\n")[0]
    assert short["duration_s"] == 3.0
    assert long_line["duration_s"] == 10.0
    assert 3.0 <= long_action["duration_s"] <= 10.0


def test_dialogue_duration_covers_the_speech_with_gaps():
    shot = shots_of("İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\n" + " ".join(["kelime"] * 10) + ".\n\nKEREM\nTamam.\n")[0]
    speech = (10 / 2.5 + 0.35) + (1 / 2.5 + 0.35)
    assert shot["duration_s"] >= speech


# ---------- çıktı biçimi ve örnek senaryo ----------

def test_output_schema():
    r = parse(SAMPLE_SCRIPT)
    assert set(r) == {"cast", "shots"}
    for s in r["shots"]:
        assert set(s) == SHOT_KEYS
        assert set(s["camera"]) == {"shot", "movement", "angle", "lens_mm"}
        assert 0 <= s["action_complexity"] <= 1


def test_sample_script_breakdown():
    r = parse(SAMPLE_SCRIPT)
    assert r["cast"] == ["Kerem", "Ayşe"]
    codes = [s["scene_id"] for s in r["shots"]]
    assert codes == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
    first, bridge = r["shots"][0], r["shots"][2]
    assert first["emotion"] == "tension"                       # (tedirgin)
    assert first["dialogue"][0]["character"] == "Kerem"
    assert bridge["action_complexity"] >= 0.7                  # koşar, kopar -> aksiyon
    assert set(bridge["characters"]) == {"Kerem", "Ayşe"}
    assert all(3 <= s["duration_s"] <= 10 for s in r["shots"])


def test_sample_script_has_two_scenes_and_expected_locations():
    r = parse(SAMPLE_SCRIPT)
    assert [s["heading"] for s in r["shots"][::2]] == ["İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE", "DIŞ. ÇÖKEN KÖPRÜ - ŞAFAK"]
    assert [s["emotion"] for s in r["shots"][2:]] == ["fear", "anger"]


def test_dialogue_without_action_gets_close_up():
    r = parse("İÇ. ODA - GECE\n\nAYŞE\nBuradayım.\n")
    assert r["shots"][0]["camera"]["shot"] == "close_up"
    assert r["shots"][0]["characters"] == ["Ayşe"]
