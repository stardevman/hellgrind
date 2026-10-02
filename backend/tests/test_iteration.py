"""Kademeli iterasyon: gerçek yerel render, nesne deposu, bütçe defteri ve Kill-Switch ile uçtan uca.

Sahne kimlikleri her çalıştırmada rastgele olduğundan, seed'e bağlı sonuçlar (hangi klip bozuk çıkar) değişir;
bu yüzden olasılıksal testler ya eşik değerlerini uçlara çeker (0 ya da 1) ya da yalnızca değişmezleri sınar.
"""
import threading
import time

import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.budget import killswitch as ks
from app.config import settings
from app.core.hashing import seed_for, visual_fingerprint
from app.core.state import advance_scene, load_characters
from app.db import session_scope
from app.engine import iteration
from app.engine.iteration import run_tiered_generation
from app.engine.providers import LocalProvider, TransientError
from app.models import Scene, Variation
from tests.media_utils import anomalies

BRIDGE = "S002_SH01"     # seedance'e yönlenen yüksek aksiyonlu sahne


def variations(sid):
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.scene_id == sid).order_by(Variation.created_at)).all()
        s.expunge_all()
        return rows


def scene_row(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        s.expunge(sc)
        return sc


def project_money(pid):
    with session_scope() as s:
        return s.execute(text("SELECT spent_usd, reserved_usd FROM projects WHERE id = :p"), {"p": pid}).one()


def ledger(pid):
    with session_scope() as s:
        rows = s.execute(text("SELECT kind, amount_usd FROM cost_ledger WHERE project_id = :p"), {"p": pid}).all()
    counts = {k: sum(1 for kind, _ in rows if kind == k) for k in ("reserve", "commit", "release")}
    return counts, {k: sum(float(a) for kind, a in rows if kind == k) for k in ("reserve", "commit", "release")}


def by(rows, **match):
    return [v for v in rows if all(getattr(v, k) == val for k, val in match.items())]


def events(pid):
    import json
    return [json.loads(e) for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]


@pytest.fixture
def scene(sample_scene):
    pid, codes = sample_scene()
    return pid, codes[BRIDGE]


# ---------- mutlu yol ----------

def test_a_scene_goes_through_probe_and_full_round_and_ends_refined(scene):
    pid, sid = scene
    assert run_tiered_generation(pid, sid) == "refined"

    sc, rows = scene_row(sid), variations(sid)
    with session_scope() as s:
        assert sc.visual_hash == visual_fingerprint(s.get(Scene, sid), load_characters(s, sid))
    assert (sc.status, sc.last_error, sc.correction_count) == ("refined", None, 0)
    assert len(by(rows, tier="probe")) == 3                          # probe_n
    assert len(by(rows, tier="full")) == 3                           # CLIP 0.95 > 0.92: ilk dalgada refined_k bulundu, durdu
    assert sorted(v.refined_rank for v in rows if v.refined_rank) == [1, 2]


def test_variation_rows_carry_everything_the_panel_and_audit_need(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    pool = sorted((v for v in variations(sid) if v.refined_rank), key=lambda v: v.refined_rank)
    for v in pool:
        assert v.status == "refined" and v.video_key and storage.exists(v.video_key)
        assert v.keyframe_key == f"thumbs/{sid}/{v.id}.jpg" and storage.exists(v.keyframe_key)
        assert v.external_request_id.startswith("local-") and v.engine == "seedance"
        assert v.clip_score == v.composite_score == pytest.approx(0.95) and v.anomalies == []
        assert float(v.cost_usd) > 0 and v.params["resolution"] == "720p" and v.params["provider_meta"] == {"planted_defect": None}
    assert pool[0].composite_score >= pool[1].composite_score
    assert all(v.status == "purged" for v in variations(sid) if not v.refined_rank)


def test_thumbnails_are_320px_wide_jpegs(scene, tmp_path):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    v = next(v for v in variations(sid) if v.refined_rank == 1)
    storage.download(v.keyframe_key, str(tmp_path / "t.jpg"))
    with Image.open(tmp_path / "t.jpg") as img:
        assert img.format == "JPEG" and img.width == 320


def test_probe_candidates_and_surplus_full_candidates_are_retired_to_quarantine(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    retired = [v for v in variations(sid) if not v.refined_rank]
    assert len(retired) == 4 and {v.params["purge_reason"] for v in retired} == {"not_top_k"}
    for v in retired:
        assert v.video_key is None and storage.exists(v.params["quarantine_key"])
        assert not storage.exists(v.params["quarantine_key"].removeprefix("quarantine/"))


def test_budget_ledger_balances_every_reservation(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    counts, sums = ledger(pid)
    spent, reserved = project_money(pid)
    assert counts["reserve"] == counts["commit"] == 6 and counts["release"] == 0
    assert float(spent) == pytest.approx(sums["commit"]) == pytest.approx(float(scene_row(sid).spent_usd))
    assert float(reserved) == 0 and float(spent) > 0                  # ücretsiz modda da gerçek fiyatla sanal harcama


def test_progress_is_published_as_events(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    kinds = [e["kind"] for e in events(pid)]
    assert kinds.count("probe_wave") == 1 and kinds.count("full_wave") == 1 and kinds.count("refined") == 1


def test_project_leaves_draft_when_production_starts(mk_project, fast):
    from app.hermes import rules
    from app.hermes.ingest import ingest
    pid = mk_project(status="draft", probe_n=2, full_n=2, wave_size=2, refined_k=1)
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        sid = str(s.scalars(select(Scene).where(Scene.project_id == pid)).first().id)
    run_tiered_generation(pid, sid)
    with session_scope() as s:
        assert s.execute(text("SELECT status FROM projects WHERE id = :p"), {"p": pid}).scalar() == "producing"


# ---------- seed'ler ----------

def test_seeds_are_derived_from_scene_visual_hash_tier_wave_and_index(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    fp = scene_row(sid).visual_hash
    for tier, wave in (("probe", 0), ("full", 0)):
        got = sorted(v.seed for v in by(variations(sid), tier=tier, wave_no=wave))
        assert got == sorted(seed_for(sid, fp, tier, wave, i) for i in range(3))


def test_regenerating_with_a_new_revision_uses_new_seeds_and_replaces_the_pool(scene):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    fp_before = scene_row(sid).visual_hash
    first = {v.id: v for v in variations(sid)}
    first_pool = {v.id for v in first.values() if v.refined_rank}
    with session_scope() as s:
        sc = s.get(Scene, sid)
        sc.prompt_revision = 1
        sc.lipsync_video_key, sc.final_video_key = "lipsync/old.mp4", "final/old.mp4"

    assert run_tiered_generation(pid, sid) == "refined"

    rows = variations(sid)
    new = [v for v in rows if v.id not in first]
    assert len(new) == 6 and not {v.seed for v in new} & {v.seed for v in first.values()}
    assert sorted(v.refined_rank for v in rows if v.refined_rank) == [1, 2]
    assert not {v.id for v in rows if v.refined_rank} & first_pool
    assert {first_v.params["purge_reason"] for first_v in variations(sid) if first_v.id in first_pool} == {"superseded"}
    sc = scene_row(sid)
    assert (sc.lipsync_video_key, sc.final_video_key) == (None, None)              # eski montaj artık geçersiz
    assert sc.visual_hash == fp_before                                             # revizyon görsel özeti değiştirmez


# ---------- test turu ----------

def test_failed_probe_rounds_retry_with_new_seeds_then_the_scene_needs_review(scene, monkeypatch):
    pid, sid = scene
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
    assert run_tiered_generation(pid, sid) == "needs_review"

    sc, rows = scene_row(sid), variations(sid)
    assert sc.status == "needs_review" and sc.last_error == "test turu eşiği geçilemedi" and sc.correction_count == 2
    assert len(rows) == 6 and {v.wave_no for v in rows} == {0, 1} and len({v.seed for v in rows}) == 6   # max_correction_rounds=1
    assert all(v.tier == "probe" and v.status == "purged" and v.params["purge_reason"] == "quality_gate" for v in rows)
    assert not [v for v in rows if v.refined_rank] and by(rows, tier="full") == []


def test_low_scores_fail_the_probe_even_when_every_clip_is_technically_fine(scene, monkeypatch):
    pid, sid = scene
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.80)
    assert run_tiered_generation(pid, sid) == "needs_review"
    rows = variations(sid)
    assert all([a["type"] for a in v.anomalies] == ["low_clip"] for v in rows)


def test_probe_needs_both_a_good_mean_and_a_sufficient_pass_rate(scene, monkeypatch):
    """Ortalama yüksek ama geçme oranı düşükse (çoğu klip bozuk) test turu kabul edilmez."""
    pid, sid = scene
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)         # CLIP skoru 0.95: ortalama eşik üstünde
    assert run_tiered_generation(pid, sid) == "needs_review"
    assert all(v.composite_score == pytest.approx(0.95) for v in variations(sid))


def test_a_scene_recovers_in_the_second_probe_round(scene, monkeypatch):
    """Birinci tur tamamen bozuk, ikinci tur sağlam: sahne yine de tamamlanır ve düzeltme sayısı 1 olur."""
    pid, sid = scene
    real = LocalProvider.generate
    calls = []

    def generate(self, req):
        calls.append(req)
        if len(calls) <= 3:
            monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
        else:
            monkeypatch.setattr(settings, "dry_run_defect_rate", 0.0)
        return real(self, req)

    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", generate)
    assert run_tiered_generation(pid, sid) == "refined"
    assert scene_row(sid).correction_count == 1


# ---------- kusurlu klipler ----------

def test_every_planted_defect_is_caught_and_never_reaches_the_pool(sample_scene, monkeypatch):
    pid, codes = sample_scene(probe_n=6, full_n=12, wave_size=6, max_correction_rounds=3, refined_k=3)
    sid = codes[BRIDGE]
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.4)
    assert run_tiered_generation(pid, sid) == "refined"

    rows = variations(sid)
    planted = [v for v in rows if v.params["provider_meta"]["planted_defect"]]
    assert planted, "bu olasılıkta kusurlu klip çıkmaması beklenmez"
    for v in planted:
        found = {a["type"] for a in v.anomalies}
        assert v.status == "purged" and v.params["purge_reason"] == "quality_gate" and v.refined_rank is None
        assert v.params["provider_meta"]["planted_defect"] in found      # kapı, kusuru kendisi buldu
    pool = [v for v in rows if v.refined_rank]
    assert 1 <= len(pool) <= 3
    for v in pool:                                                       # bağımsız ölçüm: havuzdaki videolar gerçekten sağlam
        assert not v.params["provider_meta"]["planted_defect"]


def test_pool_videos_pass_an_independent_ffmpeg_check(sample_scene, monkeypatch, tmp_path):
    pid, codes = sample_scene(probe_n=6, full_n=12, wave_size=6, max_correction_rounds=3, refined_k=2)
    sid = codes[BRIDGE]
    monkeypatch.setattr(settings, "dry_run_defect_rate", 0.4)
    run_tiered_generation(pid, sid)
    for v in (v for v in variations(sid) if v.refined_rank):
        path = storage.download(v.video_key, str(tmp_path / f"{v.id}.mp4"))
        assert anomalies(path) == {"black": False, "freeze": False}


def test_hard_purge_mode_deletes_rejected_clips(scene, monkeypatch):
    pid, sid = scene
    monkeypatch.setattr(settings, "purge_mode", "hard")
    monkeypatch.setattr(settings, "dry_run_defect_rate", 1.0)
    run_tiered_generation(pid, sid)
    for v in variations(sid):
        assert v.params["quarantine_key"] is None and v.video_key is None
        assert not storage.exists(f"variations/{sid}/{v.id}.mp4")


# ---------- tam tur ----------

def test_full_round_stops_early_once_refined_k_excellent_candidates_exist(sample_scene):
    pid, codes = sample_scene(full_n=12, wave_size=3, refined_k=3)
    sid = codes[BRIDGE]
    run_tiered_generation(pid, sid)
    assert len(by(variations(sid), tier="full")) == 3


def test_full_round_runs_to_full_n_when_nothing_is_excellent(sample_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)         # eşik 0.85 üstü, "mükemmel" 0.92 altı
    pid, codes = sample_scene(full_n=7, wave_size=3, refined_k=2)
    sid = codes[BRIDGE]
    run_tiered_generation(pid, sid)
    full = by(variations(sid), tier="full")
    assert len(full) == 7 and sorted(v.wave_no for v in full) == [0, 0, 0, 1, 1, 1, 2]     # son dalga kalanı kadar
    assert sorted(v.refined_rank for v in full if v.refined_rank) == [1, 2]


def test_soft_limit_stops_new_waves_but_the_first_wave_still_runs(sample_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)
    pid, codes = sample_scene(full_n=12, wave_size=3, refined_k=2)
    sid = codes[BRIDGE]
    ks.r().set(ks.SOFT.format(pid), "1")
    assert run_tiered_generation(pid, sid) == "refined"                # ilk dalga her zaman çalışır: havuz oluşur
    assert len(by(variations(sid), tier="full")) == 3
    assert "soft_limit_stop" in [e["kind"] for e in events(pid)]


def test_soft_limit_reached_during_the_round_stops_the_next_wave(sample_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)
    pid, codes = sample_scene(limit=10.0, full_n=12, wave_size=3, refined_k=2, soft_budget_ratio=0.2)
    sid = codes[BRIDGE]
    run_tiered_generation(pid, sid)                                    # test turu ~2,5 USD harcar: %20 = 2 USD aşıldı
    assert ks.is_soft_limited(pid)
    assert len(by(variations(sid), tier="full")) == 3                  # yalnızca ilk dalga


def test_a_nonsense_wave_size_is_clamped_to_one_and_cannot_loop_forever(sample_scene, monkeypatch):
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.90)         # mükemmel aday yok: erken durma devreye girmez
    pid, codes = sample_scene(wave_size=0, full_n=2, refined_k=1)
    assert run_tiered_generation(pid, codes[BRIDGE]) == "refined"
    assert sorted(v.wave_no for v in by(variations(codes[BRIDGE]), tier="full")) == [0, 1]


def test_full_round_with_no_passing_candidate_needs_review_and_keeps_the_old_pool(scene, monkeypatch):
    pid, sid = scene
    run_tiered_generation(pid, sid)
    old_pool = {v.id for v in variations(sid) if v.refined_rank}
    with session_scope() as s:
        s.get(Scene, sid).prompt_revision = 1

    real = LocalProvider.generate

    def only_probe_is_healthy(self, req):
        monkeypatch.setattr(settings, "dry_run_defect_rate", 0.0 if req.resolution == "480p" else 1.0)
        return real(self, req)

    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", only_probe_is_healthy)
    assert run_tiered_generation(pid, sid) == "needs_review"

    sc = scene_row(sid)
    assert sc.status == "needs_review" and sc.last_error == "tam turda eşiği geçen varyasyon yok"
    kept = [v for v in variations(sid) if v.refined_rank]
    assert {v.id for v in kept} == old_pool and all(storage.exists(v.video_key) for v in kept)


# ---------- eşzamanlılık ----------

def test_a_wave_runs_with_the_configured_number_of_threads(sample_scene, monkeypatch):
    monkeypatch.setattr(settings, "gen_concurrency", 3)
    pid, codes = sample_scene(probe_n=6, full_n=6, wave_size=6, refined_k=1)
    real, lock = LocalProvider.generate, threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(self, req):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.25)
        try:
            return real(self, req)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(LocalProvider, "generate", slow)
    run_tiered_generation(pid, codes[BRIDGE])
    assert state["peak"] == 3


# ---------- geçici hatalar ----------

def test_transient_errors_are_retried_and_each_attempt_has_its_own_reservation(scene, monkeypatch):
    pid, sid = scene
    real, calls = LocalProvider.generate, []

    def flaky(self, req):
        calls.append(req.seed)
        if len(calls) <= 2:
            raise TransientError("429")
        return real(self, req)

    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", flaky)
    assert run_tiered_generation(pid, sid) == "refined"
    counts, _ = ledger(pid)
    assert calls[0] == calls[1] == calls[2]                           # aynı varyasyon, aynı seed ile yeniden denendi
    assert counts == {"reserve": 8, "commit": 6, "release": 2}
    assert float(project_money(pid)[1]) == 0                          # iade edilen rezervasyonlar bütçede asılı kalmadı


def test_a_variation_that_keeps_failing_is_marked_failed_after_three_attempts(scene, monkeypatch):
    pid, sid = scene
    attempts = []

    def always(self, req):
        attempts.append(req.seed)
        raise TransientError("503")

    monkeypatch.setattr(LocalProvider, "generate", always)
    assert run_tiered_generation(pid, sid) == "needs_review"
    rows = variations(sid)
    assert all(v.status == "failed" for v in rows) and len(rows) == 6
    assert len(attempts) == 18                                        # 6 varyasyon x 3 deneme
    counts, sums = ledger(pid)
    assert counts["commit"] == 0 and counts["reserve"] == counts["release"] == 18
    assert (float(project_money(pid)[0]), float(project_money(pid)[1])) == (0.0, 0.0)


def test_non_transient_errors_fail_the_variation_and_propagate(scene, monkeypatch):
    pid, sid = scene

    def broken(self, req):
        raise ValueError("model çöktü")

    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", broken)
    with pytest.raises(ValueError):
        run_tiered_generation(pid, sid)
    assert {v.status for v in variations(sid)} == {"failed"}
    assert float(project_money(pid)[1]) == 0


# ---------- Kill-Switch ve bütçe ----------

def test_budget_exhaustion_freezes_the_project_and_never_overspends(sample_scene):
    pid, codes = sample_scene(limit=1.0)
    sid = codes[BRIDGE]
    with pytest.raises(ks.ProjectFrozen):                              # BudgetExceeded ya da onun ardından gelen donma
        run_tiered_generation(pid, sid)
    spent, reserved = project_money(pid)
    assert float(spent) + float(reserved) <= 1.0
    with session_scope() as s:
        assert s.execute(text("SELECT status FROM projects WHERE id = :p"), {"p": pid}).scalar() == "paused"
    assert not [v for v in variations(sid) if v.status in ("queued", "running", "generated")]
    assert scene_row(sid).status == "frozen"


def test_a_frozen_project_stops_the_run_and_leaves_no_dangling_rows(scene, monkeypatch):
    pid, sid = scene
    real, calls = LocalProvider.generate, []

    def freeze_on_second(self, req):
        calls.append(req.seed)
        if len(calls) == 2:
            ks.freeze(pid, "test")
        return real(self, req)

    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", freeze_on_second)
    with pytest.raises(ks.ProjectFrozen):
        run_tiered_generation(pid, sid)

    rows = variations(sid)
    assert not [v for v in rows if v.status in ("queued", "running", "generated")]
    assert len(calls) == 2 and scene_row(sid).status == "frozen"
    assert float(project_money(pid)[1]) == 0


def test_resuming_after_a_freeze_does_not_pay_again_for_finished_variations(scene, monkeypatch):
    pid, sid = scene
    real, calls = LocalProvider.generate, []

    def counting(self, req):
        calls.append(req.seed)
        if len(calls) == 2 and not resumed:
            ks.freeze(pid, "test")
        return real(self, req)

    resumed = False
    monkeypatch.setattr(settings, "gen_concurrency", 1)
    monkeypatch.setattr(LocalProvider, "generate", counting)
    with pytest.raises(ks.ProjectFrozen):
        run_tiered_generation(pid, sid)
    spent_before = float(project_money(pid)[0])
    assert len(calls) == 2

    ks.resume(pid)
    resumed = True
    assert run_tiered_generation(pid, sid) == "refined"

    assert len(calls) == 2 + 4                                        # 6 üretim toplam: ilk ikisi yeniden üretilmedi
    assert len(set(calls)) == 6
    assert float(project_money(pid)[0]) > spent_before
    counts, _ = ledger(pid)
    assert counts["commit"] == 6 and counts["reserve"] == 6
    assert sorted(v.refined_rank for v in variations(sid) if v.refined_rank) == [1, 2]


def test_clips_retired_by_an_earlier_pool_are_not_reused(scene, monkeypatch):
    """Havuzdan emekli edilmiş (dosyası karantinada) varyasyon, girdiler aynı olsa bile yeniden üretilir."""
    pid, sid = scene
    run_tiered_generation(pid, sid)
    with session_scope() as s:                                        # görsel özeti sil: plan bu sahneyi yeniden üretsin
        s.get(Scene, sid).visual_hash = None
    calls = []
    real = LocalProvider.generate
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req.seed) or real(self, req))
    run_tiered_generation(pid, sid)
    assert len(calls) == 6


def test_dangling_rows_of_a_crashed_run_are_cleared_when_the_next_run_starts(scene):
    pid, sid = scene
    with session_scope() as s:
        for seed, status in enumerate(("queued", "running", "generated"), start=1):
            s.add(Variation(scene_id=sid, tier="probe", engine="seedance", seed=seed,
                            prompt_revision=0, status=status, params={"resolution": "480p"}))
    run_tiered_generation(pid, sid)
    old = [v for v in variations(sid) if v.params.get("purge_reason") == "interrupted"]
    assert len(old) == 3 and {v.status for v in old} == {"failed"}


def test_status_updates_are_not_written_for_a_frozen_project(scene, monkeypatch):
    """Durum güncellemesi dondurulmuş projede yazılmaz: freeze'in `frozen` damgası ezilmez."""
    pid, sid = scene
    ks.freeze(pid, "test")
    with pytest.raises(ks.ProjectFrozen):
        advance_scene(pid, sid, status="generating")
    assert scene_row(sid).status != "generating"


# ---------- motor ve ücretli mod ----------

def test_the_provider_matches_the_scenes_routed_engine(sample_scene, monkeypatch):
    pid, codes = sample_scene()
    chosen = []
    real = iteration.get_provider
    monkeypatch.setattr(iteration, "get_provider", lambda engine: chosen.append(engine) or real(engine))
    run_tiered_generation(pid, codes["S001_SH01"])
    run_tiered_generation(pid, codes[BRIDGE])
    assert chosen == ["fal_wan", "seedance"]


def test_a_scene_without_an_engine_gets_one_before_its_fingerprint_is_taken(mk_project, mk_scene, fast):
    pid = mk_project(probe_n=2, full_n=2, wave_size=2, refined_k=1)
    sid = mk_scene(pid)
    run_tiered_generation(pid, sid)
    assert scene_row(sid).engine == "fal_wan"


def test_paid_mode_stops_before_any_spend_or_variation_row(scene, monkeypatch):
    pid, sid = scene
    monkeypatch.setattr(settings, "dry_run", False)
    with pytest.raises(NotImplementedError):
        run_tiered_generation(pid, sid)
    assert variations(sid) == [] and ledger(pid)[0] == {"reserve": 0, "commit": 0, "release": 0}
