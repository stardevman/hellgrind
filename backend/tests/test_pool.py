"""Rafine Havuz: sıralama, emekli etme ve karantina. DB ve nesne deposu gerektirir (yoksa atlanır)."""
import uuid

import pytest
from sqlalchemy import select

from app import storage
from app.config import settings
from app.db import session_scope
from app.models import Scene, Variation
from app.quality import gate

RUN = "run-1"


@pytest.fixture
def scene(mk_project, mk_scene, s3):
    pid = mk_project()
    return pid, mk_scene(pid, "generating")


def add(sid, score, *, tier="full", status="passed", run=RUN, rank=None, seed=None, file=True):
    """Bir varyasyon satırı ve (istenirse) deposunda küçük bir video dosyası."""
    vid = uuid.uuid4()
    key = f"variations/{sid}/{vid}.mp4" if file else None
    if key:
        storage.client().put_object(Bucket=settings.s3_bucket, Key=key, Body=b"video")
    with session_scope() as s:
        s.add(Variation(id=vid, scene_id=sid, tier=tier, engine="fal_wan", seed=seed if seed is not None else vid.int % 10**9,
                        prompt_revision=0, status=status, composite_score=score, clip_score=score, video_key=key,
                        refined_rank=rank, anomalies=[], params={"run": run, "resolution": "720p"}))
    return str(vid), key


def row(vid):
    with session_scope() as s:
        v = s.get(Variation, vid)
        s.expunge(v)
        return v


def finalize(sid, k=3, run=RUN, **scene_update):
    return gate.finalize_refined_pool(sid, run, k, scene_update)


# ---------- sıralama ----------

def test_best_k_get_ranks_one_to_k_by_composite_score(scene):
    _, sid = scene
    ids = {score: add(sid, score)[0] for score in (0.90, 0.95, 0.88, 0.93)}
    winners = finalize(sid, k=3)
    assert winners == [ids[0.95], ids[0.93], ids[0.90]]
    assert [(row(ids[s]).refined_rank, row(ids[s]).status) for s in (0.95, 0.93, 0.90)] == [(1, "refined"), (2, "refined"), (3, "refined")]


def test_equal_scores_are_ordered_by_seed_so_the_pool_is_deterministic(scene):
    _, sid = scene
    low, high = add(sid, 0.9, seed=5)[0], add(sid, 0.9, seed=9)[0]
    assert finalize(sid, k=2) == [low, high]


def test_pool_is_smaller_than_k_when_few_candidates_passed(scene):
    _, sid = scene
    only = add(sid, 0.9)[0]
    assert finalize(sid, k=3) == [only] and row(only).refined_rank == 1


def test_probe_tier_never_enters_the_pool_even_with_the_best_score(scene):
    _, sid = scene
    probe, _ = add(sid, 0.99, tier="probe")
    full, _ = add(sid, 0.90)
    assert finalize(sid, k=3) == [full]
    assert row(probe).status == "purged" and row(probe).params["purge_reason"] == "not_top_k"


def test_only_variations_that_passed_are_candidates(scene):
    _, sid = scene
    failed, _ = add(sid, 0.99, status="failed")
    purged, _ = add(sid, 0.99, status="purged", file=False)
    ok, _ = add(sid, 0.90)
    assert finalize(sid) == [ok]
    assert row(failed).status == "failed" and row(purged).refined_rank is None


# ---------- elenenler ----------

def test_candidates_outside_the_top_k_move_to_quarantine_with_a_reason(scene):
    _, sid = scene
    best, _ = add(sid, 0.95)
    loser, key = add(sid, 0.86)
    finalize(sid, k=1)
    v = row(loser)
    assert v.status == "purged" and v.refined_rank is None and v.video_key is None
    assert v.params["purge_reason"] == "not_top_k" and v.params["quarantine_key"] == f"quarantine/{key}"
    assert storage.exists(f"quarantine/{key}") and not storage.exists(key)
    assert row(best).video_key and storage.exists(row(best).video_key)


def test_hard_mode_deletes_instead_of_quarantining(scene, monkeypatch):
    _, sid = scene
    monkeypatch.setattr(settings, "purge_mode", "hard")
    add(sid, 0.95)
    loser, key = add(sid, 0.86)
    finalize(sid, k=1)
    assert row(loser).params["quarantine_key"] is None
    assert not storage.exists(key) and not storage.exists(f"quarantine/{key}")


# ---------- önceki havuz ve başka çalıştırmalar ----------

def test_the_previous_pool_is_retired_as_superseded_and_new_ranks_do_not_collide(scene):
    _, sid = scene
    old1, k1 = add(sid, 0.99, status="refined", run="old", rank=1)
    old2, _ = add(sid, 0.98, status="refined", run="old", rank=2)
    new, _ = add(sid, 0.90)
    assert finalize(sid, k=2) == [new]
    for old in (old1, old2):
        v = row(old)
        assert (v.status, v.refined_rank, v.params["purge_reason"]) == ("purged", None, "superseded")
    assert storage.exists(f"quarantine/{k1}")
    assert row(new).refined_rank == 1


def test_leftover_candidates_of_an_earlier_run_never_compete_and_are_retired(scene):
    _, sid = scene
    stray, _ = add(sid, 0.99, run="earlier")
    mine, _ = add(sid, 0.90)
    assert finalize(sid) == [mine]
    assert row(stray).params["purge_reason"] == "superseded" and row(stray).status == "purged"


def test_without_a_winner_nothing_changes_and_the_old_pool_stays(scene):
    _, sid = scene
    old, key = add(sid, 0.99, status="refined", run="old", rank=1)
    stray, _ = add(sid, 0.95, tier="probe")
    assert finalize(sid, status="refined", visual_hash="x") == []
    assert (row(old).status, row(old).refined_rank) == ("refined", 1) and storage.exists(key)
    assert row(stray).status == "passed"
    with session_scope() as s:
        assert s.get(Scene, sid).status == "generating"                 # sahne güncellemesi de uygulanmadı


# ---------- atomiklik ----------

def test_scene_update_is_applied_together_with_the_new_ranks(scene):
    _, sid = scene
    with session_scope() as s:
        sc = s.get(Scene, sid)
        sc.lipsync_video_key, sc.final_video_key = "lipsync/old.mp4", "final/old.mp4"
    add(sid, 0.9)
    finalize(sid, status="refined", visual_hash="abc", lipsync_video_key=None, final_video_key=None)
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert (sc.status, sc.visual_hash, sc.lipsync_video_key, sc.final_video_key) == ("refined", "abc", None, None)


def test_exactly_one_variation_holds_each_rank(scene):
    _, sid = scene
    for score in (0.91, 0.92, 0.93, 0.94, 0.95):
        add(sid, score)
    finalize(sid, k=3)
    with session_scope() as s:
        ranks = sorted(v.refined_rank for v in s.scalars(select(Variation).where(
            Variation.scene_id == sid, Variation.refined_rank.is_not(None))))
    assert ranks == [1, 2, 3]


# ---------- mükemmel aday sayısı ----------

def test_excellent_count_only_counts_this_runs_passed_full_candidates_above_the_threshold(scene):
    _, sid = scene
    add(sid, 0.95)
    add(sid, 0.93)
    add(sid, 0.90)                                   # eşik altı
    add(sid, 0.99, tier="probe")                     # test turu sayılmaz
    add(sid, 0.99, run="earlier")                    # başka çalıştırma sayılmaz
    add(sid, 0.99, status="purged", file=False)      # elenen sayılmaz
    assert gate.excellent_count(sid, RUN, 0.92) == 2
    assert gate.excellent_count(sid, RUN, 0.99) == 0
