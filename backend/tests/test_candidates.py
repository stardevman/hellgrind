"""Tek adaylı üretim (proje varsayılanı) ve yönetmen onaylı yeni adaylar.

Hat çekim başına tek aday üretir; kalite kapısından kalırsa kendiliğinden yeni aday üretmez. Panel yönetmene "yeni aday
üreteyim mi?" diye sorar ve tahmini ek maliyeti gösterir; yeni adaylar yalnızca yönetmenin onayıyla üretilir.
"""
from sqlalchemy import select, text

from app.budget import estimate as costs
from app.config import DEFAULT_PROJECT_SETTINGS, project_settings, settings
from app.db import session_scope
from app.engine import candidates
from app.engine.iteration import run_tiered_generation
from app.models import CurationEvent, Scene, Variation
from app.style import bible

SHOT = "S001_SH01"


def rows(sid):
    with session_scope() as s:
        out = s.scalars(select(Variation).where(Variation.scene_id == sid).order_by(Variation.created_at)).all()
        s.expunge_all()
        return out


def commits(pid) -> int:
    with session_scope() as s:
        return s.execute(text("SELECT COUNT(*) FROM cost_ledger WHERE project_id = :p AND kind = 'commit'"),
                         {"p": pid}).scalar()


def status(sid) -> str:
    with session_scope() as s:
        return s.get(Scene, sid).status


def offer_of(api, pid, sid):
    ov = api.get(f"/api/projects/{pid}/overview").json()
    return next(x for x in ov["scenes"] if x["id"] == sid)["more_candidates"]


# ---------- varsayılanlar ve tahminler (veritabanı yok) ----------

def test_the_project_default_is_a_single_candidate():
    cfg = project_settings({})
    assert DEFAULT_PROJECT_SETTINGS["candidate_mode"] == "single" and candidates.single(cfg)
    assert candidates.attempts(cfg) == 1                                  # fal ve referans modu: yeniden deneme yok
    assert candidates.attempts({**cfg, "candidate_mode": "multi", "fal_clip_attempts": 3}) == 3


def test_estimates_count_one_clip_per_shot_in_single_mode():
    cfg = project_settings({})
    one = costs.shot(cfg, "S1", 5.0, engine="h3", mode="ref2va")
    many = costs.shot({**cfg, "candidate_mode": "multi"}, "S1", 5.0, engine="h3", mode="ref2va")
    assert one["clips"] == 1 and one["expected_usd"] == one["max_usd"] == one["full_clip_usd"]
    assert many["clips"] == cfg["probe_n"] + cfg["full_n"] and many["max_usd"] > one["max_usd"]
    fal = costs.fal_shot(cfg, "S1", 5.0, "h3max")
    assert fal["max_usd"] == fal["expected_usd"]
    expected, worst = bible.visual_cost(cfg, "h3", 5.0)
    assert expected == worst > 0


# ---------- kademeli yol (yerel motor) ----------

def test_a_passing_single_candidate_goes_straight_to_the_cut(sample_scene):
    pid, codes = sample_scene(candidate_mode="single")
    assert run_tiered_generation(pid, codes[SHOT]) == "refined"
    vs = rows(codes[SHOT])
    assert len(vs) == 1 and vs[0].refined_rank == 1 and vs[0].tier == "full"
    assert commits(pid) == 1


def test_a_failing_single_candidate_asks_instead_of_generating_more(sample_scene, api, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)              # kalite kapısının altında
    pid, codes = sample_scene(candidate_mode="single")
    sid = codes[SHOT]
    assert run_tiered_generation(pid, sid) == "needs_review"
    assert len(rows(sid)) == 1 and commits(pid) == 1                     # kendiliğinden ek aday yok
    offer = offer_of(api, pid, sid)
    assert offer["ask"] is True and offer["reason"] == "gate" and "Yeni aday üreteyim mi?" in offer["message"]

    est = api.get(f"/api/scenes/{sid}/candidates/estimate?n=3").json()
    assert est["n"] == 3 and est["per_candidate_usd"] > 0
    assert abs(est["total_usd"] - 3 * est["per_candidate_usd"]) < 1e-3 and est["over_budget"] is False
    assert est["virtual"] is True and len(rows(sid)) == 1                 # tahmin hiçbir şey üretmez

    monkeypatch.setattr(settings, "dry_run_score_mean", 0.95)
    assert candidates.run_more(pid, sid, 2) == "refined"                 # yönetmen onayladı: 2 yeni aday
    vs = rows(sid)
    assert len(vs) == 3 and commits(pid) == 3
    assert len({v.seed for v in vs}) == 3                                 # yeni tohumlar
    winner = next(v for v in vs if v.refined_rank == 1)
    assert winner.id != vs[0].id and status(sid) == "refined"
    assert offer_of(api, pid, sid)["ask"] is False                       # skor iyi: soru kalktı


def test_more_candidates_keep_the_better_current_winner(sample_scene, monkeypatch):
    pid, codes = sample_scene(candidate_mode="single")
    sid = codes[SHOT]
    assert run_tiered_generation(pid, sid) == "refined"
    first = rows(sid)[0]
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)              # yeni adaylar kötü
    assert candidates.run_more(pid, sid, 1) == "refined"
    vs = rows(sid)
    assert len(vs) == 2 and next(v for v in vs if v.refined_rank == 1).id == first.id


def test_declining_hides_the_question_and_is_recorded(sample_scene, api, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    pid, codes = sample_scene(candidate_mode="single")
    sid = codes[SHOT]
    run_tiered_generation(pid, sid)
    assert offer_of(api, pid, sid)["ask"] is True
    assert api.post(f"/api/scenes/{sid}/candidates/decline").status_code == 200
    assert offer_of(api, pid, sid)["ask"] is False
    with session_scope() as s:
        assert s.scalars(select(CurationEvent.action).where(CurationEvent.scene_id == sid)).all() == [
            "more_candidates_decline"]


def test_the_endpoint_dispatches_only_on_request_and_records_the_decision(sample_scene, api, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    pid, codes = sample_scene(candidate_mode="single")
    sid = codes[SHOT]
    run_tiered_generation(pid, sid)
    sent = []

    class Sig:
        def __init__(self, **kw):
            self.kw = kw

        def __or__(self, other):
            return self

        def apply_async(self):
            sent.append(self.kw)

    monkeypatch.setattr("app.worker.tasks.more_candidates.si", lambda **kw: Sig(**kw))
    monkeypatch.setattr("app.worker.dispatch.build_chain", lambda *a, **k: None)
    assert api.post(f"/api/scenes/{sid}/candidates?n=9").status_code == 422          # en çok 5
    r = api.post(f"/api/scenes/{sid}/candidates?n=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stages"][0] == "VISUAL" and body["n"] == 2 and body["total_usd_estimate"] > 0
    assert sent == [{"project_id": pid, "scene_id": sid, "n": 2}]
    with session_scope() as s:
        ev = s.scalars(select(CurationEvent).where(CurationEvent.scene_id == sid)).one()
        assert ev.action == "more_candidates" and ev.payload["request"]["n"] == 2


def test_more_candidates_are_refused_over_budget_and_when_frozen(sample_scene, api, monkeypatch):
    from app.budget import killswitch
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    pid, codes = sample_scene(limit=1000.0, candidate_mode="single")
    sid = codes[SHOT]
    run_tiered_generation(pid, sid)
    with session_scope() as s:
        s.execute(text("UPDATE projects SET budget_limit_usd = spent_usd + 0.0001 WHERE id = :p"), {"p": pid})
    r = api.post(f"/api/scenes/{sid}/candidates?n=1")
    assert r.status_code == 402 and "bütçe" in r.json()["detail"]
    assert api.get(f"/api/scenes/{sid}/candidates/estimate?n=1").json()["over_budget"] is True
    killswitch.freeze(pid, "test")
    assert api.post(f"/api/scenes/{sid}/candidates?n=1").status_code == 423
    assert len(rows(sid)) == 1


# ---------- referans modu (bir istekte bütün parça) ----------

def test_reference_mode_makes_one_attempt_and_more_only_on_request(mk_project, fast, dispatched, monkeypatch):
    from tests.test_reference_mode import nora
    for name in ("h3_platform", "keyframe_platform", "video_draft_engine", "video_final_engine", "video_input_mode",
                 "scene_render_mode"):
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    pid, ids = nora(mk_project, candidate_mode="single", fal_clip_attempts=3)
    lead = ids["S001_SH01"]
    assert run_tiered_generation(pid, lead) == "needs_review"
    assert commits(pid) == 1                                              # fal_clip_attempts=3 olsa da tek deneme
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.95)
    assert candidates.run_more(pid, lead, 1) == "refined" and commits(pid) == 2
    with session_scope() as s:
        for sid in ids.values():                                          # parçanın bütün çekimleri yeni denemeden
            w = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1)).one()
            assert w.params["attempt"] == 1
