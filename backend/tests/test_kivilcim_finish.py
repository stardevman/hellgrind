"""Kıvılcım tamamlandı (460b232 sonrası); kalanlar: otomatik renk eşleştirmesinin izi, kamerası değişen kesmede renk
atlaması ölçüsü, kırp + iyileştir son karesinde kadraj kapısı. Ağ yok; değerler canlı Kıvılcım final videolarından."""
import json
from types import SimpleNamespace

from app.budget import killswitch as ks
from app.db import session_scope
from app.engine import storyboard as sb
from app.models import Scene
from app.post import color
from app.quality import consistency, gate, scorer
from tests.test_qwen_edit import edit, edit_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401


def _lab(L, a, b):
    return {"L": L, "a": a, "b": b, "sdL": 10.0}


# ---------- (2) kamerası değişen kesmede renk atlaması ----------

def test_a_cut_in_with_new_content_in_the_strips_is_not_a_colour_jump():
    # SH02 -> SH03 (yakın plan, mavi pencere üst ve sol şeritlerde): şerit ΔE 5,6, merkez dışı 4,1; gözle atlama yok
    a = {"lab": _lab(30.0, 5.0, 10.0), "lab_outer": _lab(28.0, 6.0, 12.0)}
    b = {"lab": _lab(29.7, 3.7, 4.5), "lab_outer": _lab(26.8, 5.0, 8.2)}
    held = consistency.compare(a, b)
    assert held["jump"] is True and held["region"] == "strips"                   # aynı kamera kuralı: atlama
    moved = consistency.compare(a, b, camera_change=True)
    assert moved["jump"] is False and moved["region"] == "outer" and moved["slack"] == color.CAMERA_CHANGE_SLACK


def test_a_real_grade_jump_is_still_caught_after_a_camera_change():
    a = {"lab": _lab(40.0, 5.0, 10.0), "lab_outer": _lab(38.0, 5.0, 10.0)}
    b = {"lab": _lab(34.1, 5.0, 10.0), "lab_outer": _lab(32.1, 5.0, 10.0)}         # renklendirilmemiş SH01 -> SH02 ΔL -5,9
    assert consistency.compare(a, b, camera_change=True)["jump"] is True


def test_the_camera_change_follows_the_cut_and_the_drawn_path():
    wide = {"shot": "wide", "side": 0}
    close = {"shot": "close_up", "side": 0}
    a = SimpleNamespace(transition_in=None, transition_auto="new_scene", camera=wide, camera_plan=None, board_method=None)
    cut_in = SimpleNamespace(transition_in=None, transition_auto="cut", camera=close, camera_plan=None, board_method="cut_in")
    same = SimpleNamespace(transition_in=None, transition_auto="cut", camera=wide, camera_plan=None, board_method=None)
    walk = SimpleNamespace(transition_in="continuous", transition_auto="cut", camera=close, camera_plan=None,
                           board_method=None)
    assert consistency.camera_changed(a, cut_in) and not consistency.camera_changed(a, same)
    assert not consistency.camera_changed(a, walk)                                  # devam çekimi: aynı kural


def test_the_outer_region_covers_everything_but_the_character_box():
    m = color.outer_mask(100, 100)
    assert not m[60, 50] and m[10, 50] and m[60, 10] and m[60, 90]
    assert 0.6 < m.mean() < 0.75


# ---------- (1) otomatik renk eşleştirmesinin izi ----------

def test_auto_match_always_reports_what_it_did_and_why_it_skipped(mk_project):
    from app.hermes.ingest import ingest
    from app.worker import tasks
    from tests.test_chain_consistency import KIVILCIM
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    assert tasks.auto_match_task(pid) == {"matched": []}
    events = [json.loads(e) for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]
    kinds = [e["kind"] for e in events]
    assert "color_auto_started" in kinds
    done = next(e for e in events if e["kind"] == "color_auto_matched")
    assert done["codes"] == [] and {s["reason"] for s in done["skipped"]} == {"bitmedi (final video yok)"}
    assert len(done["skipped"]) == 6


def test_auto_match_names_manual_and_reference_shots(monkeypatch):
    shots = [SimpleNamespace(id=f"i{n}", scene_code=f"S{n}", status="done", final_video_key=f"f{n}",
                             lipsync_video_key=f"l{n}", sequence_id="q", grade={"exposure": 0.2} if n == 1 else None,
                             grade_auto=None) for n in (1, 2, 3)]

    class Rows:
        def all(self):
            return shots

    class S:
        def scalars(self, q):
            return Rows()

        def get(self, model, pid):
            return SimpleNamespace(lut_key=None)

        def expunge_all(self):
            pass
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield S()
    monkeypatch.setattr(consistency, "session_scope", scope)
    monkeypatch.setattr(consistency.storage, "download", lambda key, path: path)
    monkeypatch.setattr(consistency, "probe_duration", lambda path: 2.0)
    monkeypatch.setattr(consistency, "mid_frame", lambda path, at, *a: path)
    looks = {"f1": _lab(30, 0, 0), "f2": _lab(31, 0, 0), "f3": _lab(31.5, 0.5, 0.5)}
    monkeypatch.setattr(consistency, "frame_stats", lambda img: {"lab": looks[img.split("/")[-1].split("_", 1)[1]
                                                                             .replace(".mp4", "").replace("i", "f")]})
    report = consistency.auto_match_report("p")
    reasons = {s["code"]: s["reason"] for s in report["skipped"]}
    assert report["changed"] == [] and reasons["S1"] == "renk elle ayarlı"
    assert report["references"] == {"q": "S2"} and reasons["S2"] == "referans çekim"
    assert reasons["S3"].startswith("referansa zaten yakın (ΔE")


# ---------- (3) kırp + iyileştir son karesinde kadraj ----------

def test_the_end_frame_gate_needs_eighty_percent_of_the_start_area(monkeypatch, tmp_path):
    from PIL import Image
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(clip=0.95, face=None, composite=0.95,
                                                                                  detail={}))
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.226)              # SH03 geriye açılan son kare
    frame = tmp_path / "f.jpg"
    Image.new("RGB", (64, 36)).save(frame)
    from app.config import DEFAULT_PROJECT_SETTINGS
    wide = gate.score_frame(str(frame), 1, dict(DEFAULT_PROJECT_SETTINGS), min_area=0.297 * gate.END_AREA_RATIO)
    assert wide.detail["end_framing"]["ok"] is False and wide.detail["framing_mismatch"] is True
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.29)
    tight = gate.score_frame(str(frame), 1, dict(DEFAULT_PROJECT_SETTINGS), min_area=0.297 * gate.END_AREA_RATIO)
    assert tight.detail["end_framing"]["ok"] is True and "framing_mismatch" not in tight.detail


def test_a_cut_in_end_frame_that_widens_is_redrawn_and_finally_not_used(edit, mk_project, tmp_path,  # noqa: F811
                                                                         monkeypatch):
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9, "Bolt": 0.9}}))
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.297 if path.endswith("previous.jpg") else 0.226)
    pid, codes = _kivilcim(mk_project, chain_end_frame=True)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).cut_method = "cut_in"
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    sb.make_board(pid, codes["S001_SH03"])
    ends = [r for r in edit_runs(edit)
            if r["input"]["workflow"]["170:151"]["inputs"]["prompt"].startswith("Image 1 is the first frame of this shot")
            and sb.TIGHT_END in r["input"]["workflow"]["170:151"]["inputs"]["prompt"]]
    assert len(ends) == 6                                     # SH02 ve SH03 cut-in: her son kare üç deneme
    with session_scope() as s:
        note = s.get(Scene, codes["S001_SH03"]).end_board_note
        assert s.get(Scene, codes["S001_SH02"]).board_method == "cut_in"
    assert note.startswith(sb.END_FAIL) and "son kare başlangıç kadrajından geniş (alan 0,23 < 0,24)" in note
