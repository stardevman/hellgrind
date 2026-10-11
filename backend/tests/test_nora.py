"""Nora — Gün Batımı (açı LoRA'sının ilk gerçek koşusu) bulguları: videoda mükerrer karakter, boy boy karakterden kırp +
iyileştir, açı karesi yolunda son kare kadrajı. Ağ yok; değerler Nora kliplerinden (0,5 sn'de bir yüz sayımı)."""
import pytest
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.db import session_scope
from app.engine import storyboard as sb
from app.models import Scene
from app.quality import gate, scorer
from tests.test_qwen_edit import edit, edit_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

CFG = dict(DEFAULT_PROJECT_SETTINGS)
NORA = scorer.Target(faces=(("Nora", ("assets/n/still/n.png",)),))


# ---------- (1) videoda mükerrer karakter ----------

@pytest.fixture
def video_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_video", lambda path, target: scorer.Result(
        clip=0.98, face=0.99, composite=0.98, detail={"faces_seen": 5, "faces_measured": 1, "identity": {"Nora": 0.99},
                                                      "face_instances": {"Nora": [[], [0.9992], [], []]}}))
    return str(tmp_path / "clip.mp4")


def test_a_clip_where_a_second_nora_appears_is_eliminated(video_gate, monkeypatch):
    # da3782c5: 0,75 ve 1,25 sn'de 2 yüz (88 px ve 42 px); dört örnek karede ikinci yüz ölçülmemişti
    monkeypatch.setattr(scorer, "face_counts", lambda path, step=0.5: [(0.25, 1), (0.75, 2), (1.25, 2), (1.75, 1),
                                                                       (2.25, 1), (2.75, 1)])
    score = gate.score_video(video_gate, 1, CFG, NORA)
    dup = [a for a in score.anomalies if a["type"] == "duplicate_character"]
    assert not score.passed and dup and dup[0]["seconds"] == [0.75, 1.25] and dup[0]["characters"] == ["Nora"]
    assert score.detail["face_counts"] == [1, 2, 2, 1, 1, 1]


def test_a_single_frame_with_an_extra_face_is_only_a_review(video_gate, monkeypatch):
    monkeypatch.setattr(scorer, "face_counts", lambda path, step=0.5: [(0.25, 1), (0.75, 2), (1.25, 1)])
    score = gate.score_video(video_gate, 1, CFG, NORA)
    assert score.passed and any("beklenenden fazla yüz (2 > 1)" in n for n in score.detail["review"])


def test_clean_clips_and_shots_without_humans_pass(video_gate, monkeypatch):
    calls = []
    monkeypatch.setattr(scorer, "face_counts", lambda path, step=0.5: calls.append(path) or [(0.25, 1), (0.75, 1)])
    assert gate.score_video(video_gate, 1, CFG, NORA).passed
    gate.score_video(video_gate, 1, CFG, scorer.Target(looks=(("Bolt", ("k",)),)))
    assert len(calls) == 1                                                       # insan yoksa sayım yapılmaz


# ---------- (2) boy boy karakterden kırp + iyileştir ----------

def _kivilcim_cut_in(edit, mk_project, tmp_path, monkeypatch, angles: bool):  # noqa: F811
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9, "Bolt": 0.9}}))
    monkeypatch.setattr(settings, "qwen_edit_angles", "on" if angles else "off")
    pid, codes = _kivilcim(mk_project, chain_end_frame=False)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).cut_method = "cut_in"
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    return pid, codes


def _prompts(fake):
    return [r["input"]["workflow"]["170:151"]["inputs"]["prompt"] for r in edit_runs(fake)]


@pytest.mark.parametrize("angles,expected", [(True, "<sks>"), (False, "must clearly differ from image 1")])
def test_a_crop_that_needs_too_much_zoom_falls_back_to_the_angle_lora(edit, mk_project, tmp_path, monkeypatch,  # noqa: F811
                                                                      angles, expected):
    pid, codes = _kivilcim_cut_in(edit, mk_project, tmp_path, monkeypatch, angles)

    def tall(image, target, names=()):                                           # geniş planda ayakta, ince uzun kutu
        w, h = image.size
        return {"name": names[0], "kind": "face", "box": (int(w * .47), int(h * .02), int(w * .53), int(h * .3)),
                "head": (int(w * .47), int(h * .02), int(w * .53), int(h * .08)), "fill": 0.6, "coverage": 0.02}
    monkeypatch.setattr(scorer, "character_box", tall)
    sb.make_board(pid, codes["S001_SH03"])
    sh03 = [p for p in _prompts(edit) if "Bolt" in p and not p.startswith("Image 1 is an enlarged close crop")]
    assert sh03 and (sh03[-1].startswith("<sks>") if angles else expected in sh03[-1])
    assert not [p for p in _prompts(edit) if p.startswith("Image 1 is an enlarged close crop") and "Bolt" in p]
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert "kırpım çok küçük" in (sc.board_note or "") and sc.board_method == ("angles" if angles else "anchor")


def test_a_refine_that_loses_the_composition_switches_to_the_angle_lora(edit, mk_project, tmp_path,  # noqa: F811
                                                                         monkeypatch):
    pid, codes = _kivilcim_cut_in(edit, mk_project, tmp_path, monkeypatch, angles=True)
    monkeypatch.setattr(scorer, "cut_in_view", lambda previous, path, rect=None: {"kept": False, "ratio": 0.0})
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.18)           # yakın plan tutmadı: yeniden deneme
    sb.make_board(pid, codes["S001_SH03"])
    bolt = [p for p in _prompts(edit) if "Bolt" in p and "Add Bolt" not in p]
    assert bolt[0].startswith("Image 1 is an enlarged close crop")              # 1. deneme kırp + iyileştir
    assert all(p.startswith("<sks>") for p in bolt[1:]) and len(bolt) == 3      # sonra açı LoRA'sı (aynı kırpım değil)


# ---------- (3) açı karesi yolunda son kare kadrajı ----------

def test_the_end_frame_of_an_angle_frame_keeps_its_shot_size(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9, "Bolt": 0.9}}))
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.6 if path.endswith("previous.jpg") else 0.3)
    monkeypatch.setattr(settings, "qwen_edit_angles", "on")
    pid, codes = _kivilcim(mk_project, chain_end_frame=True)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).cut_method = "angles"
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    sb.make_board(pid, codes["S001_SH03"])
    ends = [p for p in _prompts(edit) if p.startswith("Image 1 is the first frame of this shot") and sb.SAME_SIZE_END in p]
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.board_method == "angles"
        note = sc.end_board_note
    assert len(ends) == 3 and note.startswith(sb.END_FAIL) and "son kare başlangıç kadrajından geniş" in note
