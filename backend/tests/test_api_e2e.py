"""Uçtan uca (süreç içi): API -> Celery zinciri (eager) -> gerçek yerel render, ses, montaj, film.

Duman testinin (scripts/smoke.py) aynı yedi kontrolü, ama gerçek HTTP ve worker olmadan, küçük karelerle.
"""
import pytest
from sqlalchemy import select, text

from app import storage
from app.db import session_scope
from app.hermes import rules
from app.models import Scene, Variation
from tests.media_utils import probe
from tests.scene_utils import load_scenes, plans

CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
SMALL = dict(probe_n=3, full_n=6, wave_size=3, refined_k=2, max_correction_rounds=2, max_spend_per_10min=10_000)


@pytest.fixture
def film(api, api_project, fast, eager):
    """Örnek senaryodan, hattın tamamı çalıştırılmış proje (kimlik, {kod: sahne kimliği})."""
    pid = api_project(1000.0, **SMALL)
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT})
    assert r.status_code == 200, r.text
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return pid, ids


def overview(api, pid):
    return api.get(f"/api/projects/{pid}/overview").json()


def spent(api, pid):
    return api.get(f"/api/projects/{pid}/budget").json()["spent"]


def test_the_sample_script_becomes_four_finished_scenes_with_clean_pools(api, film, tmp_path):
    pid, _ = film
    scenes = overview(api, pid)["scenes"]
    assert [s["scene_code"] for s in scenes] == CODES and {s["status"] for s in scenes} == {"done"}
    for sc in scenes:
        pool = sorted(v["rank"] for v in sc["variations"] if v["rank"])
        assert pool in ([1], [1, 2]) and sc["final_url"] and sc["last_error"] is None
        for v in sc["variations"]:
            if v["rank"]:
                assert v["anomalies"] == [] and v["thumb_url"] and v["video_url"]
    assert spent(api, pid) > 0
    final = api.get(scenes[0]["final_url"], follow_redirects=False)
    assert final.status_code == 307
    key = scenes[0]["final_url"].split("key=")[1]
    info = probe(storage.download(key, str(tmp_path / "final.mp4")))
    assert len(info["video"]) == 1 and len(info["audio"]) == 1


def test_changing_only_the_music_plans_only_the_mux_and_spends_nothing(api, film):
    pid, ids = film
    before = spent(api, pid)
    old_final = api.get(f"/api/scenes/{ids['S001_SH01']}").json()["final_video_key"]
    r = api.put(f"/api/scenes/{ids['S001_SH01']}/audio", json={"kind": "music", "emotion": "joy", "gain_db": -14})
    assert r.json()["stages"] == ["MUX"]
    scene = api.get(f"/api/scenes/{ids['S001_SH01']}").json()
    assert scene["status"] == "done" and scene["final_video_key"] != old_final
    assert spent(api, pid) == before                                    # görüntü motoru çağrılmadı: harcama aynı


def test_the_curators_choice_runs_only_lipsync_and_mux(api, film):
    pid, ids = film
    sid = ids["S001_SH01"]
    scene = next(s for s in overview(api, pid)["scenes"] if s["id"] == sid)
    second = next(v for v in scene["variations"] if v["rank"] == 2)
    before, old_lipsync = spent(api, pid), api.get(f"/api/scenes/{sid}").json()["lipsync_video_key"]
    r = api.post(f"/api/scenes/{sid}/promote/{second['id']}")
    assert r.json() == {"stages": ["LIPSYNC", "MUX"]}
    after = api.get(f"/api/scenes/{sid}").json()
    assert after["status"] == "done" and after["lipsync_video_key"] != old_lipsync
    assert spent(api, pid) == before
    with session_scope() as s:
        assert s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1)) is not None
        assert str(s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1))) == second["id"]


def test_with_nothing_changed_the_plan_is_empty_everywhere(api, film):
    pid, ids = film
    assert set(plans(pid).values()) == {plans(pid)["S001_SH01"]} and str(plans(pid)["S001_SH01"]) == "Stage.NONE"
    before = spent(api, pid)
    assert all(api.post(f"/api/scenes/{sid}/render").json() == {"stages": []} for sid in ids.values())
    body = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).json()
    assert body["planned"] == {code: [] for code in CODES} and spent(api, pid) == before


def test_the_finished_scenes_assemble_into_one_film(api, film, tmp_path):
    pid, _ = film
    assert api.post(f"/api/projects/{pid}/assemble").json() == {"queued": True, "scenes": 4}
    timeline = overview(api, pid)["timeline"]
    assert [c["scene_code"] for c in timeline["cut"]["clips"]] == CODES
    key = timeline["url"].split("key=")[1]
    info = probe(storage.download(key, str(tmp_path / "film.mp4")))
    assert info["duration"] == pytest.approx(timeline["cut"]["duration_s"], abs=0.3)
    assert (int(info["video"][0]["width"]), int(info["video"][0]["height"])) == (1280, 720)


def test_regenerating_a_scene_replaces_its_pool_and_keeps_the_ranks_unique(api, film):
    pid, ids = film
    sid = ids["S002_SH01"]
    old_pool = {v["id"] for v in next(s for s in overview(api, pid)["scenes"] if s["id"] == sid)["variations"] if v["rank"]}
    assert api.post(f"/api/scenes/{sid}/render?force_visual=true").json() == {"stages": ["VISUAL", "LIPSYNC", "MUX"]}
    scene = next(s for s in overview(api, pid)["scenes"] if s["id"] == sid)
    ranks = sorted(v["rank"] for v in scene["variations"] if v["rank"])
    assert scene["status"] == "done" and ranks in ([1], [1, 2])
    assert not {v["id"] for v in scene["variations"] if v["rank"]} & old_pool
    retired = [v for v in scene["variations"] if v["id"] in old_pool]
    assert retired and {v["reason"] for v in retired} == {"superseded"} and all(v["rank"] is None for v in retired)
    assert all(v["video_url"].startswith("/api/media?key=quarantine/") for v in retired)


def test_a_one_dollar_budget_freezes_the_project_and_answers_423(api, api_project, fast, eager):
    pid = api_project(1.0, **SMALL)
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT})
    assert r.status_code == 200 and r.json()["frozen"] is True                  # senaryo işlendi; üretim bütçede durdu
    ov = overview(api, pid)
    assert ov["project"]["frozen"] is True and ov["project"]["status"] == "paused"
    assert {"frozen"} <= {s["status"] for s in ov["scenes"]}
    assert ov["project"]["spent_usd"] + ov["project"]["reserved_usd"] <= 1.0
    sid = ov["scenes"][0]["id"]
    assert api.post(f"/api/scenes/{sid}/render").status_code == 423
    assert "frozen" in [e["kind"] for e in ov["events"]]


def test_resuming_a_frozen_project_finishes_the_film_without_paying_again_for_finished_clips(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze"})
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 423
    api.post(f"/api/projects/{pid}/killswitch", json={"action": "resume"})
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 200
    assert {s["status"] for s in overview(api, pid)["scenes"]} == {"done"}
    with session_scope() as s:
        counts = s.execute(text("SELECT COUNT(*) FILTER (WHERE kind='reserve'), COUNT(*) FILTER (WHERE kind='commit'), "
                                "COUNT(*) FILTER (WHERE kind='release') FROM cost_ledger WHERE project_id = :p"), {"p": pid}).one()
    assert counts[0] == counts[1] + counts[2] and counts[1] > 0
