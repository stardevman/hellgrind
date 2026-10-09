"""Kimlik kapısı ve H3 koşulunun otomatik seçimi (ilk gerçek RunPod testlerinden):
- yüzü hiç görünmeyen aday, yüzü kötü benzeyen adaydan avantajlı olmasın (faces_seen 0 + karakterli çekim = başarısız);
- kimlik eşiğinin (identity_threshold, varsayılan 0.60) altındaki aday rafine havuza giremez;
- sırt çekimi / silüet / aşırı geniş plan: face_expected false, ceza yok;
- karakterli çekim varsayılan olarak FL2VA'dır; ilk kare Qwen-Image-Edit ile karakterin referansından çizilir."""
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
                prompt_spec={"prompt": "Elif stands on a rooftop"}, description="", board_source=None)
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


# ---------- H3 koşulu ve ilk kare modeli ----------

def test_character_shots_default_to_fl2va_with_a_qwen_edit_first_frame(monkeypatch):
    """İlk gerçek testlerden ikinci tur: Ref2VA varsayılan değil. Karakterli çekim FL2VA'dır; ilk kare Qwen-Image-Edit ile
    karakterin onaylı referansından çizilir. Ref2VA yalnızca elle seçilir."""
    from app.config import settings
    locked = SimpleNamespace(id="a1", status="locked")
    draft = SimpleNamespace(id="a2", status="draft")
    assert h3mode.effective_mode(scene(), [char(asset_id="a1")], [locked]) == "fl2va"
    assert h3mode.effective_mode(scene(h3_mode="ref2va"), [char(asset_id="a1")], [locked]) == "ref2va"   # elle
    assert h3mode.keyframe_model(scene(), [char(asset_id="a1")], [locked]) == "qwen_edit"
    assert h3mode.keyframe_model(scene(), [char(asset_id="a2")], [draft]) == "qwen"         # referans onaylı değil
    assert h3mode.keyframe_model(scene(), [char(refs=())], []) == "qwen"                    # referans yok
    assert h3mode.keyframe_model(scene(), [], []) == "qwen"                                 # karaktersiz
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen")
    assert h3mode.keyframe_model(scene(), [char(asset_id="a1")], [locked]) == "qwen"


def test_a_draft_reference_counts_when_the_project_skips_approval():
    """Deneme ön ayarı (lint_strictness warn) onay akışını atlar: yüklenmiş referans yeterlidir."""
    draft = SimpleNamespace(id="a2", status="draft")
    assert h3mode.strict_refs({"lint_strictness": "warn"}) is False and h3mode.strict_refs({}) is True
    assert h3mode.keyframe_model(scene(), [char(asset_id="a2")], [draft], strict=False) == "qwen_edit"
    assert h3mode.keyframe_model(scene(), [char(asset_id="a2")], [draft], strict=True) == "qwen"


def test_edit_references_put_the_main_character_first_then_the_location():
    kerem = SimpleNamespace(id="c2", name="Kerem", reference_images=["assets/k/still/k.png"], asset_id=None)
    roof = SimpleNamespace(id="l1", type="location", name="Çatı", status="locked",
                           references={"still": {"key": "assets/l1/still/r.png"}})
    shot = scene(prompt_spec={"prompt": "Kerem watches Elif on the rooftop"})
    refs = h3mode.edit_references(shot, [char(), kerem], [roof])
    assert [(r["name"], r["kind"]) for r in refs] == [("Kerem", "character"), ("Elif", "character"), ("Çatı", "location")]
    assert h3mode.edit_references(scene(), [], [roof]) == []                        # karaktersiz çekimde kare metinden


def test_the_identity_warning_is_only_for_a_character_frame_drawn_from_text(monkeypatch):
    from app.config import settings
    warning = "Storyboard karesi karakter referansı kullanmıyor; kimlik kayabilir."
    assert h3mode.fl2va_identity_warning(scene(board_source="comfyui"), [char()], []) is None        # referanstan yeniden çizilecek
    assert h3mode.fl2va_identity_warning(scene(board_source="comfyui"), [char(refs=())], []) == warning   # referans yok: metinden
    assert h3mode.fl2va_identity_warning(scene(board_source="qwen_edit"), [char()], []) is None
    assert h3mode.fl2va_identity_warning(scene(board_source=None), [char()], []) is None            # qwen_edit çizilecek
    assert h3mode.fl2va_identity_warning(scene(board_source="comfyui"), [], []) is None             # karaktersiz
    assert h3mode.fl2va_identity_warning(scene(board_source="comfyui", h3_mode="ref2va"), [char()], []) is None
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen")
    assert h3mode.fl2va_identity_warning(scene(board_source=None), [char()], []) == warning
    assert h3mode.fl2va_identity_warning(scene(board_source="comfyui"), [char()], []) == warning      # düz Qwen karesi


def test_the_effective_mode_is_part_of_the_visual_fingerprint():
    auto = visual_fingerprint(scene(duration_s=3.0), [char()], [])
    fl2va = visual_fingerprint(scene(duration_s=3.0, h3_mode="fl2va"), [char()], [])
    ref2va = visual_fingerprint(scene(duration_s=3.0, h3_mode="ref2va"), [char()], [])
    assert auto == fl2va != ref2va


# ---------- "Kıvılcım" testinden: robot, küçük yüz, ham kosinüs ----------

BOLT = scorer.Target(style="", content="Bolt the robot", looks=(("Bolt", ("assets/b/still/b.png",)),))


def test_a_non_human_character_is_never_rejected_for_a_missing_face(real):
    """Robotun yüzünü SFace bulamaz: insan olmayan karakterde yüz aranmaz, görsel benzerlik (OpenCLIP) ölçülür."""
    looks = lambda v: scorer.Result(clip=0.97, face=None, composite=0.97,                        # noqa: E731
                                    detail={"faces_seen": 0, "faces_measured": 0, "identity": {}, "look": {"Bolt": v}})
    assert gate._judge(looks(0.82), 1, [], {**CFG, "look_threshold": 0.75}, BOLT, check_identity=True).passed
    low = gate._judge(looks(0.61), 1, [], {**CFG, "look_threshold": 0.75}, BOLT, check_identity=True)
    assert not low.passed and low.anomalies == [{"type": "low_likeness", "look": {"Bolt": 0.61}, "threshold": 0.75}]


def test_a_small_face_is_not_measured_and_never_rejects(real):
    small = scorer.Result(clip=0.97, face=None, composite=0.97,
                          detail={"faces_seen": 1, "faces_measured": 0, "identity": {}})
    assert gate._judge(small, 1, [], CFG, ELIF, check_identity=True).passed


def test_the_gate_compares_the_identity_score_not_the_raw_cosine(monkeypatch):
    """SFace'in aynı kişi eşiği kosinüs 0,363'tür. Mila'nın 0,58 / 0,46 / 0,57'si aynı kişidir: kapı 0-1 kimlik skorunu
    (lojistik eğri) karşılaştırır. Küçük yüz ölçülmez; referanslar en yüksek çözünürlüklüden; en yüksek skor alınır."""
    import numpy as np
    from PIL import Image
    ref = np.zeros(128, dtype=np.float32)
    ref[0] = 1.0

    def at(cos):
        v = np.zeros(128, dtype=np.float32)
        v[0], v[1] = cos, (1 - cos ** 2) ** 0.5
        return v

    class Fake:
        scale = 100.0

        def __init__(self, faces):
            self._faces = faces

        def encode_images(self, images):
            return np.ones((len(images), 4), dtype=np.float32) / 2

        def encode_texts(self, texts):
            return np.ones((len(texts), 4), dtype=np.float32) / 2

        def faces(self, image):
            return self._faces

        def reference_face(self, key):
            return ref

        def reference_look(self, key):
            return np.ones(4, dtype=np.float32) / 2

    frame = [Image.new("RGB", (64, 36))]
    mila = scorer.Target(faces=(("Mila", ("assets/m/still/m.png",)),))
    for cos in (0.58, 0.46, 0.57):
        monkeypatch.setattr(scorer, "models", lambda c=cos: Fake([(1.0, at(c), 220.0)]))
        r = scorer.score_images(frame, mila)
        assert r.detail["identity_cosine"]["Mila"] == pytest.approx(cos, abs=1e-3)
        assert r.detail["identity"]["Mila"] > 0.8 and not gate.identity_anomalies(r.detail, mila, CFG)
    monkeypatch.setattr(scorer, "models", lambda: Fake([(1.0, at(0.58), 52.0)]))          # yüz küçük: ölçülmez
    r = scorer.score_images(frame, mila)
    assert (r.detail["faces_seen"], r.detail["faces_measured"], r.detail["identity"]) == (1, 0, {})
    assert gate.identity_anomalies(r.detail, mila, CFG) == []
    monkeypatch.setattr(scorer, "models", lambda: Fake([]))
    r = scorer.score_images(frame, scorer.Target(looks=(("Bolt", ("assets/b/still/b.png",)),)))
    assert r.detail["look"] == {"Bolt": 1.0} and r.detail["faces_seen"] == 0


def test_references_are_ordered_sharpest_first():
    keys = ["assets/a/turnaround/frames/0.jpg", "assets/a/expressions/e.png", "assets/a/still/s.png"]
    assert scorer.ref_order(keys) == ("assets/a/still/s.png", "assets/a/expressions/e.png", "assets/a/turnaround/frames/0.jpg")


def robot(**kw):
    return SimpleNamespace(**{**dict(id="c2", name="Bolt", reference_images=["assets/b/still/b.png"], asset_id=None,
                                     embedding_version=1, lora_url=None, lora_scale=1.0, kind="non_human"), **kw})


def test_no_face_is_expected_from_a_robot_or_in_a_wide_action_shot():
    assert h3mode.face_expected(scene(), [robot()]) is False                                  # yalnızca robot
    assert h3mode.face_expected(scene(), [robot(), char()]) is True                            # insan da var
    assert h3mode.face_expected(scene(camera={"shot": "wide"}, action_complexity=0.8), [char()]) is False
    assert h3mode.face_expected(scene(camera={"shot": "wide"}, action_complexity=0.2), [char()]) is True


def test_the_character_kind_is_suggested_from_the_description():
    assert h3mode.suggest_kind("Bolt", "A small rusty robot with one blue eye") == "non_human"
    assert h3mode.suggest_kind("Pamuk", "Beyaz bir köpek") == "non_human"
    assert h3mode.suggest_kind("Mila", "A girl with round glasses and overalls") == "human"


def test_a_robot_is_measured_by_likeness_and_a_human_by_face():
    target = scorer.target_for(scene(), [char(), robot()], None)
    assert [n for n, _ in target.faces] == ["Elif"] and [n for n, _ in target.looks] == ["Bolt"]
