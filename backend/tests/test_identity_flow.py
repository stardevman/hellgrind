"""Kimlik kapısının uçtan uca etkisi ve panel düzeltmeleri: yüzü görünmeyen aday havuza giremez; hepsi kimlikte kalırsa
çekim "Kimlik düşük" olur; tek çekim için donma geçici kaldırılıp iş bitince geri gelir; üst bandın durumu türetilir."""
import itertools

import pytest
from sqlalchemy import select

from app.budget import killswitch
from app.db import session_scope
from app.models import Character, Scene, SceneCharacter, Variation
from app.quality import gate, scorer


def character_shot(pid) -> str:
    """Örnek senaryoda karakteri olan ilk çekim; karaktere referans görseli verilir (kimlik ölçülebilsin)."""
    with session_scope() as s:
        sid, cid = s.execute(select(SceneCharacter.scene_id, SceneCharacter.character_id)
                             .join(Scene, Scene.id == SceneCharacter.scene_id).where(Scene.project_id == pid)).first()
        s.get(Character, cid).reference_images = ["assets/x/still/a.png"]
        return str(sid)


@pytest.fixture
def real_scores(monkeypatch):
    """Gerçek skorlayıcı yerine: adaylar sırayla yüzsüz (0.9926) ve yüzlü (0.9924; benzerlik verilen değer)."""
    def install(identity: float):
        calls = itertools.count()

        def score(path, target):
            if next(calls) % 2 == 0:
                return scorer.Result(clip=0.9926, face=None, composite=0.9926, detail={"faces_seen": 0, "identity": {}})
            name = target.faces[0][0]
            return scorer.Result(clip=0.99, face=identity, composite=0.9924,
                                 detail={"faces_seen": 1, "identity": {name: identity}})
        monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
        monkeypatch.setattr(gate, "ensure_scorer", lambda: None)
        monkeypatch.setattr(scorer, "score_video", score)
    return install


def test_a_faceless_candidate_never_wins_a_character_shot(sample_scene, real_scores):
    from app.engine.iteration import run_tiered_generation
    real_scores(0.65)
    pid, _ = sample_scene()
    sid = character_shot(pid)
    assert run_tiered_generation(pid, sid) == "refined"
    with session_scope() as s:
        pool = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank.is_not(None))).all()
        faceless = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.tier == "full")).all()
    assert pool and all(v.anomalies == [] for v in pool)
    assert any(a["type"] == "no_face" for v in faceless for a in (v.anomalies or []))


def test_all_candidates_below_the_identity_threshold_mark_the_shot_low_identity(sample_scene, real_scores):
    from app.engine.iteration import run_tiered_generation
    real_scores(0.47)
    pid, _ = sample_scene()
    sid = character_shot(pid)
    assert run_tiered_generation(pid, sid) == "needs_review"
    with session_scope() as s:
        assert s.get(Scene, sid).last_error.startswith("Kimlik düşük")


def test_the_identity_threshold_is_a_project_setting(api, api_project):
    pid = api_project(5.0)
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"identity_threshold": 0.7}}).status_code == 200
    assert api.get(f"/api/projects/{pid}/overview").json()["project"]["settings"]["identity_threshold"] == 0.7
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"identity_threshold": 1.5}}).status_code == 422


def test_scene_mode_and_face_expectation_can_return_to_auto(api, sample_scene):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    api.patch(f"/api/scenes/{sid}", json={"h3_mode": "fl2va", "face_expected": "false"})
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert (sc.h3_mode, sc.face_expected) == ("fl2va", False)
    api.patch(f"/api/scenes/{sid}", json={"h3_mode": "auto", "face_expected": "auto"})
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert (sc.h3_mode, sc.face_expected) == (None, None)
    shot = next(x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"] if x["id"] == sid)
    assert shot["h3_mode"] in ("fl2va", "ref2va") and shot["h3_mode_explicit"] is None and "face_expected" in shot


# ---------- donma: tek çekim ----------

def test_one_shot_can_be_produced_while_frozen_and_the_project_refreezes(api, sample_scene, dispatched):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    killswitch.freeze(pid, "manual")
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").status_code == 423
    r = api.post(f"/api/scenes/{sid}/render?force_visual=true&thaw=true")
    assert r.status_code == 200 and dispatched and not killswitch.is_frozen(pid)
    assert killswitch.refreeze_after(pid, codes["S001_SH02"]) is False      # başka çekim bitince donmaz
    assert killswitch.refreeze_after(pid, sid) is True and killswitch.is_frozen(pid)


# ---------- üst bant ----------

@pytest.mark.parametrize("status,scenes,shown", [
    ("producing", ["done", "generating"], "producing"), ("producing", ["done", "done"], "completed"),
    ("producing", ["done", "needs_review"], "idle"), ("paused", ["generating"], "paused"), ("draft", [], "draft")])
def test_the_displayed_project_status_follows_the_shots(status, scenes, shown):
    from app.api.projects import display_status
    assert display_status(status, scenes) == shown
