"""Yönetmen kontrolü ve yönetmenlik kurallarının istemlere eklenmesi (veritabanı gerektirmez)."""
from types import SimpleNamespace

from app.quality import directing


def shots(spec):
    return [{"code": f"S{i + 1:03d}", "duration_s": d, "shot": sh, "movement": mv}
            for i, (d, sh, mv) in enumerate(spec)]


def codes(result):
    return {f["code"] for f in result["findings"]}


def test_varied_film_has_no_findings():
    r = directing.review(shots([(3, "wide", "pan"), (5, "medium", "dolly_in"), (2.5, "close_up", "static"),
                                (8, "wide", "crane"), (4, "medium", "tracking"), (3, "close_up", "static")]))
    assert r["findings"] == []
    assert r["summary"]["total_s"] == 25.5


def test_monotone_film_is_flagged():
    r = directing.review(shots([(5, "medium", "static")] * 6))
    assert {"camera_variety", "framing_run", "movement_run", "tempo_flat"} <= codes(r)
    run = next(f for f in r["findings"] if f["code"] == "framing_run")
    assert run["shots"] == [f"S{i:03d}" for i in range(1, 7)]
    assert "orta" in run["message"]


def test_slow_open_and_no_breath():
    spec = [(8, "wide", "static")] + [(4 + (i % 2), sh, mv) for i, (sh, mv) in enumerate(
        [("medium", "pan"), ("close_up", "dolly_in"), ("wide", "crane"), ("medium", "tracking"),
         ("close_up", "orbit"), ("wide", "pan"), ("medium", "tilt")])]
    r = directing.review(shots(spec))
    assert "slow_open" in codes(r)
    # 8 sn açılış ortancanın (5 sn) 1,5 katı: nefes çekimi sayılır
    assert "no_breath" not in codes(r)
    flat = shots([(4, "wide", "pan"), (5, "medium", "dolly_in"), (4, "close_up", "crane"), (5, "wide", "tracking"),
                  (4, "medium", "orbit"), (5, "close_up", "pan"), (4, "wide", "tilt"), (5, "medium", "dolly_in")])
    assert "no_breath" in codes(directing.review(flat))


def test_too_few_shots_skips_checks():
    r = directing.review(shots([(5, "medium", "static")] * 3))
    assert r["findings"] == [] and "en az" in r["note"]


def test_shot_rows_skips_removed_shots_and_prefers_camera_plan():
    sc = [SimpleNamespace(scene_code="S002", seq_order=2, in_cut=True, duration_s=4, camera={"shot": "wide"},
                          camera_plan={"shot": "close_up", "movement": "pan"}),
          SimpleNamespace(scene_code="S001", seq_order=1, in_cut=True, duration_s=3,
                          camera={"shot": "wide", "movement": "static"}, camera_plan=None),
          SimpleNamespace(scene_code="S003", seq_order=3, in_cut=False, duration_s=3, camera={}, camera_plan=None)]
    rows = directing.shot_rows(sc)
    assert [r["code"] for r in rows] == ["S001", "S002"]
    assert rows[1]["shot"] == "close_up"


def test_rules_reach_writer_and_assistant():
    from app.assistant import core, tools
    from app.engine import prompt_writer
    with_audio = prompt_writer.system_prompt("h3max", True)
    assert "Directing craft" in with_audio and "anticipation" in with_audio and "near-silence" in with_audio
    assert "near-silence" not in prompt_writer.system_prompt("h3max", False)
    assert "directing_review" in core.SYSTEM
    assert "directing_review" in tools.TOOL_NAMES and "directing_review" not in tools.PAID
