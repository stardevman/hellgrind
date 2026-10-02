"""Kademeli iterasyon: gerçek PostgreSQL, Redis, MinIO ve FFmpeg üzerinde uçtan uca.

LocalProvider gerçek klip üretir; yalnızca çözünürlük küçültülür ve test için ayrı bir veritabanı/kova kullanılır
(bkz. conftest). Servisler yoksa bu dosyadaki testler atlanır.
"""
import threading
import time

import pytest
from sqlalchemy import text

from app import storage
from app.budget import killswitch as ks
from app.config import settings
from app.core.hashing import seed_for, visual_fingerprint
from app.core.rerender import Stage, plan_rerender
from app.db import session_scope
from app.engine import iteration, providers
from app.engine.iteration import load_characters, run_tiered_generation
from app.engine.providers import LocalProvider, TransientError
from app.models import Scene
from app.quality import gate

PROBE_COST, FULL_COST = 0.104, 0.208          # fal_wan: 480p 0.04/sn, 720p 0.08/sn; sahne 2.6 sn


def start(mk_project, mk_scene, limit=100, **cfg):
    base = dict(probe_n=3, full_n=4, wave_size=2, refined_k=2, max_correction_rounds=1)
    pid = mk_project(limit=limit, **{**base, **cfg})
    return pid, mk_scene(pid, "pending", duration_s=2.6)


def variations(sid):
    with session_scope() as s:
        return s.execute(text("""SELECT id::text AS id, tier::text AS tier, wave_no, seed, status::text AS status,
                                        video_key, keyframe_key, refined_rank, params, anomalies, cost_usd,
                                        composite_score
                                 FROM variations WHERE scene_id = :s ORDER BY created_at, seed"""), {"s": sid}).all()


def scene(sid):
    with session_scope() as s:
        return s.execute(text("""SELECT status::text AS status, visual_hash, last_error, correction_count, engine::text AS engine,
                                        spent_usd FROM scenes WHERE id = :s"""), {"s": sid}).one()


def project(pid):
    with session_scope() as s:
        return s.execute(text("SELECT status::text AS status, spent_usd, reserved_usd FROM projects WHERE id = :p"),
                         {"p": pid}).one()


def ledger(pid):
    with session_scope() as s:
        return dict(s.execute(text("SELECT kind::text, count(*) FROM cost_ledger WHERE project_id = :p GROUP BY kind"),
                              {"p": pid}).all())


def event_kinds(pid):
    import json
    return [json.loads(e)["kind"] for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]


def tiers(sid):
    vs = variations(sid)
    return [v for v in vs if v.tier == "probe"], [v for v in vs if v.tier == "full"]


# ---------- mutlu yol ----------

def test_excellent_candidates_stop_the_full_round_early(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "refined"

    probes, fulls = tiers(sid)
    assert len(probes) == 3 and len(fulls) == 2                  # ilk dalgada 2 mükemmel aday -> erken durma
    assert sorted(v.refined_rank for v in fulls) == [1, 2] and {v.status for v in fulls} == {"refined"}
    assert scene(sid).status == "refined" and scene(sid).engine == "fal_wan"
    assert event_kinds(pid)[::-1][:2] == ["probe_wave", "full_wave"]


def test_pool_files_live_in_storage_and_everything_else_is_quarantined(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)

    probes, fulls = tiers(sid)
    for v in fulls:
        assert v.video_key == f"variations/{sid}/{v.id}.mp4" and storage.exists(v.video_key)
    for v in probes:
        assert v.status == "purged" and v.video_key is None
        assert v.params["purge_reason"] == "not_top_k"
        assert v.params["quarantine_key"] == f"quarantine/variations/{sid}/{v.id}.mp4"
        assert storage.exists(v.params["quarantine_key"]) and not storage.exists(f"variations/{sid}/{v.id}.mp4")
    for v in probes + fulls:                                    # küçük resimler her varyasyon için
        assert v.keyframe_key == f"thumbs/{sid}/{v.id}.jpg" and storage.exists(v.keyframe_key)


def test_visual_hash_written_at_the_end_matches_the_rerender_planner(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)

    with session_scope() as s:
        sc = s.get(Scene, sid)
        stages, fp = plan_rerender(sc, load_characters(s, sid), [])
        assert sc.visual_hash == visual_fingerprint(sc, load_characters(s, sid)) == fp.visual
    assert Stage.VISUAL not in stages                            # görüntü yeniden üretilmez, yalnızca ses/montaj kalır


def test_every_variation_is_reserved_and_committed_at_the_virtual_provider_price(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)

    probes, fulls = tiers(sid)
    assert ledger(pid) == {"reserve": 5, "commit": 5}
    assert [float(v.cost_usd) for v in probes] == [PROBE_COST] * 3 and [float(v.cost_usd) for v in fulls] == [FULL_COST] * 2
    expected = 3 * PROBE_COST + 2 * FULL_COST
    assert float(project(pid).spent_usd) == pytest.approx(expected) and float(project(pid).reserved_usd) == 0
    assert float(scene(sid).spent_usd) == pytest.approx(expected)


def test_seeds_follow_the_documented_formula(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)

    vh = scene(sid).visual_hash
    probes, fulls = tiers(sid)
    assert {v.seed for v in probes} == {seed_for(sid, vh, "probe", 0, i) for i in range(3)}
    assert {v.seed for v in fulls} == {seed_for(sid, vh, "full", 0, i) for i in range(2)}


def test_pool_ranks_follow_composite_score_descending(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(gate, "score_clip_dry", lambda seed: round(0.86 + (seed % 90) / 1000, 4))   # 0.86 - 0.949, hepsi geçer
    pid, sid = start(mk_project, mk_scene, excellent_threshold=0.99, refined_k=2, full_n=6, wave_size=3)
    run_tiered_generation(pid, sid)

    _, fulls = tiers(sid)
    by_rank = {v.refined_rank: v.composite_score for v in fulls if v.refined_rank}
    losers = [v.composite_score for v in fulls if not v.refined_rank]
    assert len(fulls) == 6 and set(by_rank) == {1, 2}
    assert by_rank[1] >= by_rank[2] >= max(losers)


def test_good_but_not_excellent_scores_run_every_wave(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)       # eşiği (0.85) geçer, mükemmel (0.92) değil
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "refined"

    probes, fulls = tiers(sid)
    assert len(fulls) == 4 and {v.wave_no for v in fulls} == {0, 1}
    assert sorted(v.refined_rank for v in fulls if v.refined_rank) == [1, 2]
    losers = [v for v in fulls if not v.refined_rank]
    assert len(losers) == 2 and all(v.params["purge_reason"] == "not_top_k" for v in losers)


# ---------- test turu reddi ----------

def test_failed_probe_retries_with_new_seeds_then_needs_review(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "needs_review"

    probes, fulls = tiers(sid)
    assert fulls == [] and len(probes) == 6                      # 1 ilk deneme + max_correction_rounds(1) tekrar
    assert {v.wave_no for v in probes} == {0, 1} and len({v.seed for v in probes}) == 6
    s = scene(sid)
    assert (s.status, s.last_error, s.correction_count) == ("needs_review", "test turu eşiği geçilemedi", 2)
    for v in probes:
        assert v.status == "purged" and v.params["purge_reason"] == "quality_gate"
        assert [a["type"] for a in v.anomalies] == ["low_clip"]
        assert storage.exists(v.params["quarantine_key"]) and v.video_key is None
    assert event_kinds(pid).count("probe_wave") == 2


def test_defective_clips_are_rejected_by_the_real_ffmpeg_detector(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)       # CLIP skoru yüksek ama klipler siyah/donmuş
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "needs_review"

    probes, _ = tiers(sid)
    kinds = {a["type"] for v in probes for a in v.anomalies}
    assert kinds <= {"black", "freeze"} and kinds
    assert all(v.status == "purged" and v.params["purge_reason"] == "quality_gate" for v in probes)


def test_hard_purge_mode_deletes_instead_of_quarantining(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.5)
    monkeypatch.setattr(settings, "purge_mode", "hard")
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)

    for v in tiers(sid)[0]:
        assert v.params["quarantine_key"] is None
        assert not storage.exists(f"variations/{sid}/{v.id}.mp4")
        assert not storage.exists(f"quarantine/variations/{sid}/{v.id}.mp4")


def test_scene_needs_review_when_no_full_variation_passes(fast, mk_project, mk_scene, monkeypatch):
    state = {"tier": None}
    original = iteration.run_wave

    def tracked(c, tier, *a, **k):
        state["tier"] = tier
        return original(c, tier, *a, **k)

    monkeypatch.setattr(iteration, "run_wave", tracked)
    monkeypatch.setattr(gate, "score_clip_dry", lambda seed: 0.95 if state["tier"] == "probe" else 0.5)
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "needs_review"

    probes, fulls = tiers(sid)
    assert scene(sid).last_error == "tam turda eşiği geçen varyasyon yok"
    assert len(fulls) == 4 and all(v.params["purge_reason"] == "quality_gate" for v in fulls)
    assert all(v.refined_rank is None for v in probes + fulls)


# ---------- Kill-Switch ile etkileşim ----------

def test_soft_limit_flag_stops_new_waves_but_not_the_first(fast, mk_project, mk_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)
    pid, sid = start(mk_project, mk_scene)
    ks.r().set(ks.SOFT.format(pid), "1")

    assert run_tiered_generation(pid, sid) == "refined"

    _, fulls = tiers(sid)
    assert len(fulls) == 2                                       # yalnızca ilk dalga; ikinci dalga başlatılmadı
    assert "soft_limit_stop" in event_kinds(pid)


def test_frozen_project_starts_nothing(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    ks.freeze(pid, "test")

    with pytest.raises(ks.ProjectFrozen):
        run_tiered_generation(pid, sid)

    assert variations(sid) == [] and ledger(pid) == {}


def test_budget_exhaustion_freezes_project_and_never_overspends(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene, limit=0.3, probe_n=10)    # 0.104 x 3 = 0.312 > 0.3

    with pytest.raises(ks.ProjectFrozen):
        run_tiered_generation(pid, sid)

    p = project(pid)
    vs = variations(sid)
    assert p.status == "paused"
    assert float(p.spent_usd) + float(p.reserved_usd) <= 0.3 and float(p.reserved_usd) == 0
    assert float(p.spent_usd) == pytest.approx(sum(float(v.cost_usd) for v in vs))
    assert any(v.status == "failed" for v in vs)


# ---------- sağlayıcı hataları ve eşzamanlılık ----------

def test_transient_errors_are_retried_and_each_failed_attempt_is_refunded(fast, mk_project, mk_scene, monkeypatch):
    real, lock, left = LocalProvider.generate, threading.Lock(), {"n": 2}

    def flaky(self, req):
        with lock:
            if left["n"] > 0:
                left["n"] -= 1
                raise TransientError("test")
        return real(self, req)

    monkeypatch.setattr(LocalProvider, "generate", flaky)
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "refined"

    vs = variations(sid)
    assert not any(v.status == "failed" for v in vs)
    assert ledger(pid) == {"reserve": len(vs) + 2, "release": 2, "commit": len(vs)}
    assert float(project(pid).reserved_usd) == 0


def test_provider_that_keeps_failing_gives_up_after_three_attempts_and_refunds(fast, mk_project, mk_scene, monkeypatch):
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(1) or (_ for _ in ()).throw(TransientError("hep")))
    pid, sid = start(mk_project, mk_scene)

    assert run_tiered_generation(pid, sid) == "needs_review"

    vs = variations(sid)
    assert len(vs) == 6 and {v.status for v in vs} == {"failed"}
    assert len(calls) == 6 * iteration.MAX_PROVIDER_RETRIES == 18
    assert ledger(pid) == {"reserve": 18, "release": 18}
    p = project(pid)
    assert float(p.spent_usd) == 0 and float(p.reserved_usd) == 0


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_wave_concurrency_is_bounded_by_gen_concurrency(fast, mk_project, mk_scene, monkeypatch, workers):
    monkeypatch.setattr(settings, "gen_concurrency", workers)
    real, lock, state = LocalProvider.generate, threading.Lock(), {"now": 0, "max": 0}

    def tracked(self, req):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        try:
            return real(self, req)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(LocalProvider, "generate", tracked)
    pid, sid = start(mk_project, mk_scene, probe_n=4)
    run_tiered_generation(pid, sid)

    assert state["max"] == workers


# ---------- bilinen sorun ----------

@pytest.mark.xfail(strict=True, reason="Bilinen sorun, karar bekliyor: sahne baştan üretilince eski Rafine Havuz üyeleri "
                                       "status='refined' ama sırasız olarak depoda kalıyor (silme davranışı onay ister).")
def test_regeneration_leaves_no_orphaned_refined_variations(fast, mk_project, mk_scene):
    pid, sid = start(mk_project, mk_scene)
    run_tiered_generation(pid, sid)
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET prompt_spec = CAST(:p AS jsonb) WHERE id = :s"),
                  {"p": '{"prompt": "tamamen yeni bir istek"}', "s": sid})

    run_tiered_generation(pid, sid)

    vs = variations(sid)
    assert [v for v in vs if v.status == "refined" and v.refined_rank is None] == []
    assert sorted(v.refined_rank for v in vs if v.refined_rank) == [1, 2]


def test_non_positive_wave_size_cannot_hang_the_worker(fast, mk_project, mk_scene):
    """Regresyon: API artık reddediyor ama veritabanında eski/elle girilmiş bir proje wave_size=0 taşıyabilir;
    n=0 ile produced hiç artmaz ve dalga döngüsü sonsuza dek dönerdi."""
    pid, sid = start(mk_project, mk_scene, wave_size=0, full_n=2)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(run_tiered_generation(pid, sid)), daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "dalga döngüsü sonlanmadı"
    assert outcome == ["refined"] and len(tiers(sid)[1]) == 2
