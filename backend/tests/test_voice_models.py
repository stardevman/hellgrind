"""Yerel ses modelleri: geçici ses Okur ile, ses denetimi Duyu-small ile (ücretsiz, CPU).

Kabul:
  - Okur'un ön yüzü sayıları, saati, yüzdeyi ve sıra sayılarını kelimeyle okur (normalizer-tr yoksa yedek okuyucu).
  - Karakterin perde kaydırması tohumdan gelir (hep aynı); duygu hızı değiştirir.
  - "okur" seçiliyken model dosyaları yoksa espeak kullanılır; dosya anahtarı gerçekten kullanılan motoru gösterir.
  - Denetim iki metni aynı biçime getirir: rakamla yazılan sayı kelimeyle yazılanla uyuşur; başka bir cümle uyuşmaz.
  - Sesi olan replikler denetlenir; sonuç Ses sayfasında görünür, ses ya da metin değişince eskir. Yüklenen kayıt
    kendiliğinden denetlenir. Okur sesi filme girdiyse teslim paketine kaynak bildirimi eklenir.
  - Model dosyaları varsa (imaj): Okur'un sesi Duyu-small ile yazıya dökülünce metinle uyuşur (CI'da atlanır).
"""
import os
import subprocess

import numpy as np
import pytest

from app import storage
from app.config import settings
from app.post import audio, okur, pipeline, voice
from app.quality import asr
from tests.test_voice_first import voiced  # noqa: F401 (fikstür)

MODELS = settings.quality_model_dir
needs_okur = pytest.mark.skipif(not okur.available(MODELS), reason="Okur model dosyaları yok (CI)")
needs_duyu = pytest.mark.skipif(not asr.available(MODELS), reason="Duyu-small model dosyaları yok (CI)")


# ---------- saf hesaplar ----------

@pytest.mark.parametrize("n,words", [(0, "sıfır"), (7, "yedi"), (10, "on"), (21, "yirmi bir"), (100, "yüz"),
                                     (115, "yüz on beş"), (1000, "bin"), (1250, "bin iki yüz elli"),
                                     (2026, "iki bin yirmi altı"), (1_250_000, "bir milyon iki yüz elli bin")])
def test_numbers_are_read_in_turkish(n, words):
    assert okur.number_words(n) == words


@pytest.mark.parametrize("n,words", [(1, "birinci"), (2, "ikinci"), (3, "üçüncü"), (4, "dördüncü"), (6, "altıncı"),
                                     (9, "dokuzuncu"), (10, "onuncu"), (21, "yirmi birinci"), (40, "kırkıncı"),
                                     (100, "yüzüncü")])
def test_ordinals_follow_vowel_harmony(n, words):
    assert okur.ordinal_words(n) == words


def test_the_fallback_reader_spells_times_percentages_decimals_and_ordinals():
    fe = okur.Frontend()
    fe._normalizer = None                                            # normalizer-tr kurulu değilmiş gibi
    assert fe("Saat 09:30'da 3. kattaki toplantıya") == "saat dokuz otuz'da üçüncü kattaki toplantıya"
    assert fe("%15 indirim, 1.250.000 lira, 3,5 metre") == "yüzde on beş indirim, bir milyon iki yüz elli bin lira, üç virgül beş metre"
    assert fe("KÖPRÜ ÇÖKÜYOR, IŞIL!") == "köprü çöküyor, ışıl!"                     # Türkçe küçük harf (I -> ı)
    assert fe("Dükkanın kârı hala artıyor") == "dükkânın kârı hâlâ artıyor"           # şapka geri gelir
    assert fe("   ") == ""


def test_the_timeline_gives_each_frame_its_word_and_place():
    cw, wstart, n = okur.words("ab cd")
    assert cw.tolist() == [0, 0, 0, 1, 1] and wstart.tolist() == [0, 0, 0, 3, 3] and n == 2
    counts = okur.word_frames(np.array([1.2, 1.3, 0.4, 2.0, 1.1]), cw, n)
    assert counts.tolist() == [3, 3] and counts.sum() == round(1.2 + 1.3 + 0.4 + 2.0 + 1.1)
    fw, fp = okur.frame_timeline(counts)
    assert fw.tolist() == [0, 0, 0, 1, 1, 1] and np.allclose(fp, [0, 1 / 3, 2 / 3] * 2)


def test_the_noise_is_seeded_and_standard_normal():
    a, b = okur.gaussian_noise(7, 4000), okur.gaussian_noise(7, 4000)
    assert np.array_equal(a, b) and not np.array_equal(a, okur.gaussian_noise(8, 4000))
    assert abs(a.mean()) < 0.1 and abs(a.std() - 1) < 0.1


def test_a_character_keeps_its_pitch_and_emotion_sets_the_pace():
    assert audio.okur_voice("neutral", 3) == audio.okur_voice("neutral", 3)
    assert audio.okur_voice("neutral", 0) != audio.okur_voice("neutral", 1)
    assert audio.okur_voice("neutral", 0) == (0.0, 1.0)
    shift, fast = audio.okur_voice("anger", 0)
    assert fast > 1 > audio.okur_voice("sadness", 0)[1] and shift < 0
    for seed in range(12):
        for emotion in audio.EMOTION_VOICE:
            shift, speed = audio.okur_voice(emotion, seed)
            assert -7 < shift < 9 and 0.75 < speed < 1.25                      # atempo 0,5-2 aralığında kalır


def test_okur_falls_back_to_espeak_without_model_files(monkeypatch):
    monkeypatch.setattr(settings, "tts_engine", "okur")
    monkeypatch.setattr(okur, "available", lambda root: False)
    assert pipeline.effective_tts_engine() == "espeak"
    monkeypatch.setattr(okur, "available", lambda root: True)
    assert pipeline.effective_tts_engine() == "okur"
    monkeypatch.setattr(settings, "tts_engine", "elevenlabs")                 # ücretsiz modda yerel motor
    assert pipeline.effective_tts_engine() == "okur"


def test_the_check_compares_spoken_numbers_and_flags_another_sentence():
    assert asr.plain("Saat 09:30'da 3. kattaki toplantıya!") == "saat dokuz otuzda üçüncü kattaki toplantıya"
    assert asr.compare("Saat 09:30'da 3. kattaki toplantıya geç kalma.",
                       "Saat 09.30’da 3. kattaki toplantıya geç kalma.")["ok"]
    near = asr.compare("Kerem, sakın arkana bakma.", "Kerem sakın arkana bakma")
    assert near == {"cer": 0.0, "wer": 0.0, "ok": True}
    other = asr.compare("Kerem, sakın arkana bakma.", "Köprü çöküyor, koş Ayşe.")
    assert not other["ok"] and other["cer"] > asr.MAX_CER
    assert asr.compare("Kerem, sakın arkana bakma.", "") == {"cer": 1.0, "wer": 1.0, "ok": False}
    assert asr.compare("", "ne olursa")["ok"]
    assert asr.lang_for("Nereye gidiyorsun?") == "tr" and asr.lang_for("Kerem, sakın arkana bakma.") == "tr"
    assert asr.lang_for("Where are you going?") == "mix" and asr.lang_for("Don't look back, Kerem!") == "mix"
    assert asr.lang_for("") == "tr"


# ---------- API (sahte yazıya dökme) ----------

@pytest.fixture
def checking(monkeypatch):
    """Denetim açık, model varmış gibi; yazıya dökme sahte: dosyada ne konuşulduğunu HEARD söyler."""
    heard = {}
    monkeypatch.setattr(settings, "voice_check", "auto")
    monkeypatch.setattr(asr, "available", lambda root: True)
    monkeypatch.setattr(asr, "transcribe", lambda path, root, lang="mix": heard.get(open(path, "rb").read(), ""))
    return heard


def test_lines_with_a_voice_are_checked_and_a_wrong_recording_is_flagged(api, voiced, checking, tmp_path, s3):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import AudioTrack
    from tests.test_voice_first import _lines, _wav
    st, lines = _lines(api, voiced)
    assert st["checker"] and st["counts"]["unchecked"] == 0                  # henüz sesi olan replik yok
    assert api.post(f"/api/projects/{voiced}/voice/temp").json() == {"queued": True}
    with session_scope() as s:                                               # geçici seslerin içeriği = repliğin metni
        for t in s.scalars(select(AudioTrack).where(AudioTrack.kind == "dialogue", AudioTrack.is_active,
                                                    AudioTrack.file_key.is_not(None))):
            checking[storage.read_bytes(t.file_key)] = t.text
    # voice_temp, sahte yazıya dökme kurulmadan önce çalıştı: hiçbir şey duyulmadı, hepsi uyuşmuyor
    st, lines = _lines(api, voiced)
    assert all(ln["check"] and not ln["check"]["ok"] for _, _, ln in lines)
    assert st["counts"]["mismatch"] == len(lines)
    with session_scope() as s:                                               # sonuç yeniden denetlensin diye silinir
        for t in s.scalars(select(AudioTrack).where(AudioTrack.kind == "dialogue")):
            t.params = {k: v for k, v in (t.params or {}).items() if k != "check"}
    client = storage.client()                                                # önbellekteki boş dökümler de
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket, Prefix="asr/"):
        for o in page.get("Contents", []):
            client.delete_object(Bucket=settings.s3_bucket, Key=o["Key"])
    assert api.get(f"/api/projects/{voiced}/voice").json()["counts"]["unchecked"] == len(lines)
    assert api.post(f"/api/projects/{voiced}/voice/check").json() == {"queued": True}
    st, lines = _lines(api, voiced)
    assert all(ln["check"] == {"ok": True, "cer": 0.0, "heard": ln["text"]} for _, _, ln in lines)
    assert st["counts"]["mismatch"] == 0 and st["counts"]["unchecked"] == 0

    # Yanlış repliğe yüklenen kayıt kendiliğinden denetlenir ve uyuşmuyor olarak işaretlenir
    _, shot, line = lines[0]
    wav = _wav(tmp_path / "a.wav", 1.2)
    checking[wav.read_bytes()] = "Bambaşka bir cümle söylendi burada."
    up = api.post(f"/api/projects/{voiced}/voice/upload?filename=x.wav", content=wav.read_bytes()).json()
    r = api.put(f"/api/scenes/{shot['scene_id']}/audio", json={"kind": "dialogue", "source": "upload",
                                                              "file_key": up["key"], "replaces": line["track_id"]})
    assert r.status_code == 200, r.text
    st, after = _lines(api, voiced)
    final = after[0][2]
    assert final["final"] and final["check"]["ok"] is False and final["check"]["heard"] == "Bambaşka bir cümle söylendi burada."
    assert st["counts"]["mismatch"] == 1
    events = api.get(f"/api/projects/{voiced}/overview").json()["events"]
    assert any(e.get("kind") == "voice_check" and e.get("mismatch") for e in events)


def test_the_check_button_needs_the_model(api, voiced, monkeypatch):
    monkeypatch.setattr(settings, "voice_check", "auto")
    monkeypatch.setattr(asr, "available", lambda root: False)
    assert api.get(f"/api/projects/{voiced}/voice").json()["checker"] is False
    assert api.post(f"/api/projects/{voiced}/voice/check").status_code == 409


def test_okur_voices_bring_the_attribution_into_the_delivery_list(voiced, monkeypatch):
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import AudioTrack, Scene
    from app.post import delivery
    with session_scope() as s:
        assert not voice.okur_used(s, voiced)
        assert "OKUR_TTS.txt" not in [r["path"] for r in delivery.plan(s, voiced)]
        t = s.scalars(select(AudioTrack).join(Scene, Scene.id == AudioTrack.scene_id)
                      .where(Scene.project_id == voiced, AudioTrack.kind == "dialogue")).first()
        t.file_key = "audio/okur/abc.wav"
    with session_scope() as s:
        assert voice.okur_used(s, voiced)
        assert "OKUR_TTS.txt" in [r["path"] for r in delivery.plan(s, voiced)]
    assert "CC BY 4.0" in voice.OKUR_NOTICE and "Ahmet Hakan Beşel" in voice.OKUR_NOTICE


# ---------- gerçek modeller (imajda; CI'da atlanır) ----------

@needs_okur
def test_okur_speaks_turkish_and_keeps_the_length_when_the_pitch_moves(tmp_path):
    plain, shifted = tmp_path / "a.wav", tmp_path / "b.wav"
    text = "Bu dökümhanede yirmi yıl çalıştım, bir gün bile böyle bir şey görmedim."
    d0 = audio.synthesize(text, str(plain), "neutral", 0, engine="okur", model_dir=MODELS)
    d1 = audio.synthesize(text, str(shifted), "neutral", 1, engine="okur", model_dir=MODELS)
    assert 2.5 < d0 < 7 and abs(d0 - d1) < 0.1                              # perde kaydı süreyi değiştirmez
    rate = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(plain)],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert rate == "48000"
    assert audio.synthesize("...", str(tmp_path / "c.wav"), engine="okur", model_dir=MODELS) > 0   # boş: yedeğe düşer


@needs_okur
@needs_duyu
@pytest.mark.parametrize("seed,emotion", [(0, "neutral"), (1, "joy"), (2, "anger"), (3, "fear")])
def test_duyu_hears_what_okur_said(tmp_path, seed, emotion):
    text = "Köprü çöküyor, koş Ayşe! Saat 09:30'da 3. katta buluşalım."
    path = str(tmp_path / "a.wav")
    audio.synthesize(text, path, emotion, seed, engine="okur", model_dir=MODELS)
    heard = asr.transcribe(path, MODELS, asr.lang_for(text))
    result = asr.compare(text, heard)
    assert result["ok"], (heard, result)
    assert not asr.compare("Kerem, sakın arkana bakma.", heard)["ok"]
    assert os.path.getsize(path) > 10_000
