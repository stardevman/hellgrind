"""Faz 16: renk aşaması ve süreklilik kontrolü.

Kabul:
  - Renk ayarı değişince VISUAL ve LIPSYNC çalışmaz.
  - Bilerek farklı renklendirilmiş iki ardışık çekim uyarı üretir.
  - Önerilen düzeltme uygulanınca uyarı kalkar.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import storage
from app.core.hashing import GRADE_IDENTITY, grade_fingerprint
from app.core.rerender import ALL, PRODUCTION, Stage, plan_rerender, stage_names
from app.db import session_scope
from app.engine.procedural import render_clip
from app.engine.providers import LocalProvider
from app.hermes import rules
from app.models import Project, Scene
from app.post import grade
from app.quality import consistency
from tests.media_utils import frame, probe
from tests.scene_utils import plans, scene_row

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)
IDENTITY_CUBE = "LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n"
WARM_CUBE = "LUT_3D_SIZE 2\n" + "\n".join(f"{min(1, r * 1.0 + 0.15)} {g} {b * 0.7}" for b in (0, 1) for g in (0, 1)
                                          for r in (0, 1)) + "\n"


# ---------- özet ve planlayıcı (veritabanı gerekmez) ----------

def _scene(**kw):
    base = dict(prompt_spec={"prompt": "p"}, camera={}, engine="fal_wan", duration_s=5.0, grade={}, grade_hash=None,
                graded_video_key=None, visual_hash=None, dialogue_hash=None, bed_hash=None, lipsync_video_key=None,
                final_video_key=None, board_key=None, board_hash=None)
    return SimpleNamespace(**{**base, **kw})


def _done(**kw):
    sc = _scene(**kw)
    _, fp = plan_rerender(sc, [], [])
    return _scene(**{**kw, "visual_hash": fp.visual, "dialogue_hash": fp.dialogue, "bed_hash": fp.bed,
                     "lipsync_video_key": "l.mp4", "final_video_key": "f.mp4", "board_key": "b", "board_hash": fp.visual})


def test_a_neutral_grade_without_a_lut_needs_no_grade_stage():
    assert grade_fingerprint(_scene(), None) is None
    assert grade_fingerprint(_scene(grade=dict(GRADE_IDENTITY)), None) is None
    assert grade_fingerprint(_scene(grade={"exposure": 0.0000001}), None) is None             # yuvarlama gürültüsü
    assert grade_fingerprint(_scene(), "lut1") != grade_fingerprint(_scene(), "lut2")
    assert grade_fingerprint(_scene(grade={"temperature": 4000}), None) is not None


def test_only_a_colour_change_plans_grade_and_mux():
    done = _done()
    assert plan_rerender(done, [], [])[0] == Stage.NONE
    warm = _done(grade={"temperature": 4000})
    assert plan_rerender(warm, [], [])[0] == Stage.GRADE | Stage.MUX                           # VISUAL ve LIPSYNC yok
    applied = SimpleNamespace(**{**vars(warm), "grade_hash": grade_fingerprint(warm, None), "graded_video_key": "g.mp4"})
    assert plan_rerender(applied, [], [])[0] == Stage.NONE
    back = SimpleNamespace(**{**vars(applied), "grade": {}})
    assert plan_rerender(back, [], [])[0] == Stage.GRADE | Stage.MUX                           # nötre dönüş de yeniden renk
    assert plan_rerender(done, [], [], lut_hash="lut")[0] == Stage.GRADE | Stage.MUX            # film LUT'u


def test_upstream_changes_carry_the_grade_along():
    assert plan_rerender(_scene(grade={"exposure": 0.5}), [], [])[0] == ALL | Stage.GRADE
    assert plan_rerender(_scene(), [], [])[0] == ALL                                            # nötr: GRADE yok
    relipsync = SimpleNamespace(**{**vars(_done(grade={"exposure": 0.5})), "lipsync_video_key": None})
    assert plan_rerender(relipsync, [], [])[0] == Stage.LIPSYNC | Stage.GRADE | Stage.MUX
    assert stage_names(PRODUCTION | Stage.GRADE) == ["VISUAL", "LIPSYNC", "GRADE", "MUX"]


# ---------- filtre ve LUT (ffmpeg, veritabanı gerekmez) ----------

def test_cube_files_are_validated_in_turkish():
    assert grade.validate_cube(IDENTITY_CUBE) == 2
    for bad, needle in (("LUT_1D_SIZE 4\n", "1B"), ("0 0 0\n", "LUT_3D_SIZE satırı yok"),
                        ("LUT_3D_SIZE 2\n0 0 0\n", "8 renk satırı"), ("LUT_3D_SIZE 99\n", "2 ile 65"),
                        ("LUT_3D_SIZE 2\n" + "a b c\n" * 8, "Sayı olmayan")):
        with pytest.raises(grade.InvalidLut, match=needle):
            grade.validate_cube(bad)


def test_the_filter_chain_skips_neutral_parts():
    assert grade.filter_chain({}, None) == "null"
    chain = grade.filter_chain({"exposure": 0.5, "temperature": 4000}, "/tmp/f.cube")
    assert chain == "lut3d=file='/tmp/f.cube',exposure=exposure=0.5,colortemperature=temperature=4000"
    assert grade.clamp({"exposure": 9, "temperature": 100}) == {**GRADE_IDENTITY, "exposure": 3.0, "temperature": 2000}


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.work_dir", str(tmp_path))
    out = {}
    for name, seed, shot in (("a", 1, "wide"), ("b", 2, "medium")):
        out[name] = render_clip(str(tmp_path / f"{name}.mp4"), seed, 320, 180, 2.0, 8, emotion="tension",
                                camera={"shot": shot, "movement": "pan"}, characters=["Kerem"])
    return out


def test_grading_warms_the_picture_and_keeps_the_timing(clips, tmp_path):
    src = clips["a"]
    out = grade.grade_video(src, str(tmp_path / "g.mp4"), {"temperature": 3500}, None)
    warmth = lambda img: img[..., 0].mean() - img[..., 2].mean()                               # noqa: E731
    assert warmth(frame(out, 1.0).astype(float)) - warmth(frame(src, 1.0).astype(float)) > 8
    assert abs(probe(out)["duration"] - probe(src)["duration"]) < 0.15


def test_two_shots_of_one_palette_flow_but_a_deliberate_grade_jumps_and_the_suggestion_fixes_it(clips):
    a = consistency.frame_stats(grade.mid_frame(clips["a"], 1.0))
    b = consistency.frame_stats(grade.mid_frame(clips["a"], 1.25))
    assert consistency.compare(a, a)["score"] == 0
    assert consistency.compare(a, b)["score"] <= consistency.JUMP_SCORE                       # aynı çekim, akan an
    off = {"exposure": 1.2, "temperature": 3500}
    jumped = consistency.compare(a, consistency.frame_stats(grade.mid_frame(clips["a"], 1.25, off)))
    assert jumped["score"] > consistency.JUMP_SCORE and jumped["delta"]["brightness"] > 10        # LAB ΔL
    assert 0 < jumped["histogram_distance"] <= 1
    suggestion, score = consistency.suggest(a, clips["a"], 1.25, grade.clamp(off), None)
    assert score <= consistency.JUMP_SCORE and abs(suggestion["exposure"]) < 1.2
    measured = consistency.compare(a, consistency.frame_stats(grade.mid_frame(clips["a"], 1.25, suggestion)))["score"]
    assert measured == score                                                                   # öneri ölçülmüş bir sonuçtur


# ---------- API ve uçtan uca ----------

@pytest.fixture
def produced(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    for code in ("S001_SH01", "S001_SH02"):
        assert "VISUAL" in api.post(f"/api/scenes/{ids[code]}/render").json()["stages"]
    return pid, ids


def spent(pid):
    with session_scope() as s:
        return s.get(Project, pid).spent_usd


def test_a_new_scene_seam_tolerates_a_different_look_but_still_flags_a_big_jump():
    """Yeni sahne (mekân/zaman değişir) ΔE 5,3'lük farkla sıçrama sayılmaz; aynı fark kesmede sıçramadır; ΔE 9 yine sıçrar."""
    base = {"L": 50.0, "a": 0.0, "b": 0.0, "sdL": 10.0}
    a = {"lab": base}
    near = {"lab": {**base, "a": 5.3}}                      # ΔE ≈ 5,3 > 4 (eşik)
    far = {"lab": {**base, "a": 9.0}}
    assert consistency.compare(a, near)["jump"] is True
    r = consistency.compare(a, near, new_scene=True)
    assert r["jump"] is False and r["slack"] == consistency.NEW_SCENE_SLACK
    assert consistency.compare(a, far, new_scene=True)["jump"] is True


def test_a_colour_change_runs_only_grade_and_mux(api, produced, monkeypatch):
    pid, ids = produced
    sid = ids["S001_SH02"]
    before, cost = scene_row(sid), spent(pid)
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)
    r = api.post(f"/api/scenes/{sid}/grade", json={"exposure": 0.4, "temperature": 5000})
    assert r.status_code == 200 and r.json()["stages"] == ["GRADE", "MUX"], r.text
    after = scene_row(sid)
    assert calls == [] and spent(pid) == cost                                                 # görüntü motoru yok
    assert after.lipsync_video_key == before.lipsync_video_key                                # dudak senkronu yok
    assert after.graded_video_key and storage.exists(after.graded_video_key) and after.final_video_key != before.final_video_key
    assert plans(pid)["S001_SH02"] == Stage.NONE
    r = api.post(f"/api/scenes/{sid}/grade", json={})                                          # nötre dönüş
    assert r.json()["stages"] == ["GRADE", "MUX"] and scene_row(sid).graded_video_key is None
    assert scene_row(sid).final_video_key == before.final_video_key                           # içerik-adresli: aynı montaj


def test_finish_pending_restores_a_finished_shot_without_rendering(api, produced, monkeypatch):
    """Kare hazırlama gibi bir adım biten çekimi "pending"e düşürmüşse: görüntü üretilmez, klip korunur, çekim yine done."""
    pid, ids = produced
    sid = ids["S001_SH02"]
    before, cost = scene_row(sid), spent(pid)
    with session_scope() as s:
        s.get(Scene, sid).status = "pending"
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)
    r = api.post(f"/api/projects/{pid}/finish-pending")
    assert r.status_code == 200 and r.json()["planned"]["S001_SH02"] == [], r.text
    after = scene_row(sid)
    assert after.status == "done" and after.final_video_key == before.final_video_key and calls == [] and spent(pid) == cost
    assert "status_restored" in api.get(f"/api/projects/{pid}/overview").json()["events"][0]["kind"]


def test_the_preview_shows_before_and_after(api, produced):
    pid, ids = produced
    sid = ids["S001_SH01"]
    before = api.get(f"/api/scenes/{sid}/grade/preview.jpg?before=true")
    after = api.get(f"/api/scenes/{sid}/grade/preview.jpg?exposure=1&temperature=3500")
    assert before.status_code == after.status_code == 200 and before.headers["content-type"] == "image/jpeg"
    assert before.content != after.content
    assert api.get(f"/api/scenes/{ids['S002_SH01']}/grade/preview.jpg").status_code == 409     # henüz videosu yok
    assert api.get(f"/api/scenes/{sid}/grade/preview.jpg?exposure=9").status_code == 422


def test_a_film_lut_regrades_finished_shots_only(api, produced):
    pid, ids = produced
    finished = {c: scene_row(ids[c]) for c in ("S001_SH01", "S001_SH02")}
    r = api.post(f"/api/projects/{pid}/lut?filename=film.cube", content=WARM_CUBE.encode())
    assert r.status_code == 200 and r.json()["lut"]["size"] == 2, r.text
    assert sorted(r.json()["regraded"]) == ["S001_SH01", "S001_SH02"]                          # üretilmemişler beklemez
    for code, before in finished.items():
        after = scene_row(ids[code])
        assert after.graded_video_key and after.lipsync_video_key == before.lipsync_video_key
    assert {scene_row(ids[c]).status for c in ("S002_SH01", "S002_SH02")} == {"pending"}
    assert plans(pid)["S001_SH01"] == Stage.NONE
    assert api.post(f"/api/projects/{pid}/lut?filename=film.cube", content=b"LUT_3D_SIZE 2\n").status_code == 422
    assert api.post(f"/api/projects/{pid}/lut?filename=film.png", content=WARM_CUBE.encode()).status_code == 422
    r = api.delete(f"/api/projects/{pid}/lut").json()
    assert sorted(r["regraded"]) == ["S001_SH01", "S001_SH02"]
    assert all(scene_row(ids[c]).final_video_key == finished[c].final_video_key for c in finished)   # LUT'suz hâline döndü


def test_a_deliberate_colour_jump_is_flagged_and_the_suggestion_clears_it(api, produced):
    pid, ids = produced
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 200                       # birleşince denetlenir
    report = api.get(f"/api/projects/{pid}/continuity").json()
    assert any((p["from"], p["to"]) == ("S001_SH01", "S001_SH02") for p in report["pairs"])
    api.post(f"/api/scenes/{ids['S001_SH02']}/grade", json={"exposure": 1.2, "temperature": 3500})
    pair = next(p for p in api.get(f"/api/projects/{pid}/continuity").json()["pairs"] if p["to"] == "S001_SH02")
    assert pair["jump"] is True and pair["delta"]["brightness"] > 0 and pair["suggestion"], pair     # renk sıçraması
    ov = api.get(f"/api/projects/{pid}/overview").json()
    assert any(p["jump"] for p in ov["project"]["continuity"]["pairs"])
    before = pair
    r = api.post(f"/api/scenes/{ids['S001_SH02']}/grade", json=pair["suggestion"])               # tek tıkla uygula
    assert r.json()["stages"] == ["GRADE", "MUX"]
    pair = next(p for p in api.get(f"/api/projects/{pid}/continuity").json()["pairs"] if p["to"] == "S001_SH02")
    # Parlaklık ve sıcaklık düzelir (ΔL <= 3); prosedürel çekimlerde kalan fark içerikten gelen a (yeşil-macenta) farkıdır,
    # renk ayarının bunu düzelten bir parametresi yok
    assert abs(pair["lab"]["L"]) <= 3 and pair["score"] < before["score"] / 2
