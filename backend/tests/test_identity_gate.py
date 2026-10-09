"""Kimlik kapısı ve H3 koşulunun otomatik seçimi (ilk gerçek RunPod testlerinden):
- yüzü hiç görünmeyen aday, yüzü kötü benzeyen adaydan avantajlı olmasın (faces_seen 0 + karakterli çekim = başarısız);
- kimlik eşiğinin (identity_threshold, varsayılan 0.60) altındaki aday rafine havuza giremez;
- sırt çekimi / silüet / aşırı geniş plan: face_expected false, ceza yok;
- onaylı referansı olan karakterli çekim varsayılan olarak Ref2VA'dır (storyboard karesi referans içermez)."""
from types import SimpleNamespace

import pytest

from app.core import h3mode
from app.core.hashing import visual_fingerprint
from app.quality import gate, scorer

CFG = {"clip_threshold": 0.85, "identity_threshold": 0.60}
ELIF = scorer.Target(style="", content="Elif on a rooftop", faces=(("Elif", ("assets/e/still/a.png",)),))


def measured(composite, faces_seen, identity):
    return scorer.Result(clip=composite, face=None, composite=composite,
                         detail={"faces_seen": faces_seen, "identity": identity})


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")


def test_a_clip_without_any_face_fails_the_identity_gate_in_a_character_shot(real):
    no_face = gate._judge(measured(0.9926, 0, {}), 1, [], CFG, ELIF, check_identity=True)
    elif_ = gate._judge(measured(0.9924, 1, {"Elif": 0.65}), 2, [], CFG, ELIF, check_identity=True)
    assert not no_face.passed and no_face.anomalies[0]["type"] == "no_face"
    assert elif_.passed                                              # yüzü görünen aday kazanır


def test_a_similarity_below_the_threshold_keeps_the_candidate_out_of_the_pool(real):
    low = gate._judge(measured(0.97, 1, {"Elif": 0.47}), 1, [], CFG, ELIF, check_identity=True)
    assert not low.passed and low.anomalies == [{"type": "low_identity", "identity": {"Elif": 0.47}, "threshold": 0.6}]
    assert gate._judge(measured(0.97, 1, {"Elif": 0.47}), 1, [], {**CFG, "identity_threshold": 0.4}, ELIF,
                       check_identity=True).passed


def test_no_penalty_when_the_face_is_not_expected_or_there_is_no_reference(real):
    back = scorer.Target(faces=ELIF.faces, face_expected=False)
    assert gate._judge(measured(0.97, 0, {}), 1, [], CFG, back, check_identity=True).passed
    assert gate._judge(measured(0.97, 0, {}), 1, [], CFG, scorer.Target(), check_identity=True).passed
    # storyboard karesi (metinden çizilir) kimlik kapısına girmez
    assert gate._judge(measured(0.97, 0, {}), 1, [], CFG, ELIF, check_identity=False).passed


def test_the_review_reason_says_identity_when_most_candidates_failed_it():
    from app.engine.iteration import VarResult, review_reason
    results = [VarResult("a", False, 0.99, [{"type": "no_face"}]), VarResult("b", False, 0.97, [{"type": "low_identity"}]),
               VarResult("c", False, 0.5, [{"type": "low_clip"}])]
    assert review_reason(results, CFG, "test turu").startswith("Kimlik düşük")
    assert review_reason([VarResult("c", False, 0.5, [{"type": "low_clip"}])], CFG, "test turu") == "test turu eşiği geçilemedi"


# ---------- yüz beklentisi ----------

def scene(**kw):
    base = dict(h3_mode=None, face_expected=None, keyframe_key=None, engine="h3", camera={"shot": "wide"},
                prompt_spec={"prompt": "Elif stands on a rooftop"}, description="")
    return SimpleNamespace(**{**base, **kw})


def char(refs=("assets/e/still/a.png",), asset_id=None):
    return SimpleNamespace(id="c1", name="Elif", reference_images=list(refs), asset_id=asset_id, embedding_version=1,
                           lora_url=None, lora_scale=1.0)


@pytest.mark.parametrize("kw,expected", [
    ({}, True), ({"camera": {"shot": "extreme_wide"}}, False),
    ({"prompt_spec": {"prompt": "Elif seen from behind, silhouette against the sun"}}, False),
    ({"description": "Elif'in sırtı kameraya dönük"}, False), ({"face_expected": False}, False),
    ({"camera": {"shot": "extreme_wide"}, "face_expected": True}, True),
])
def test_face_expectation_defaults_and_overrides(kw, expected):
    assert h3mode.face_expected(scene(**kw), [char()]) is expected


def test_no_characters_no_face_expected():
    assert h3mode.face_expected(scene(), []) is False


# ---------- H3 koşulu ----------

def test_character_shots_with_approved_references_default_to_ref2va():
    locked = SimpleNamespace(id="a1", status="locked")
    draft = SimpleNamespace(id="a2", status="draft")
    assert h3mode.effective_mode(scene(), [char(asset_id="a1")], [locked]) == "ref2va"
    assert h3mode.effective_mode(scene(), [char(asset_id="a2")], [draft]) == "fl2va"      # referans onaylı değil
    assert h3mode.effective_mode(scene(), [char(refs=())], []) == "fl2va"                   # referans yok
    assert h3mode.effective_mode(scene(), [], []) == "fl2va"                                # karaktersiz
    assert h3mode.effective_mode(scene(keyframe_key="keyframes/x.jpg"), [char()], []) == "fl2va"   # kendi ilk karesi
    assert h3mode.effective_mode(scene(h3_mode="fl2va"), [char()], []) == "fl2va"           # yönetmenin seçimi


def test_choosing_fl2va_for_a_character_shot_warns_about_identity():
    assert "kimlik kayabilir" in h3mode.fl2va_identity_warning(scene(h3_mode="fl2va"), [char()], [])
    assert h3mode.fl2va_identity_warning(scene(), [char()], []) is None                     # otomatik: Ref2VA
    assert h3mode.fl2va_identity_warning(scene(h3_mode="fl2va"), [], []) is None


def test_the_effective_mode_is_part_of_the_visual_fingerprint():
    auto = visual_fingerprint(scene(duration_s=3.0), [char()], [])
    explicit = visual_fingerprint(scene(duration_s=3.0, h3_mode="ref2va"), [char()], [])
    fl2va = visual_fingerprint(scene(duration_s=3.0, h3_mode="fl2va"), [char()], [])
    assert auto == explicit != fl2va
