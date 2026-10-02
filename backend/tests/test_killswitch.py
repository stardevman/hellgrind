"""Kill-Switch'in gerçek PostgreSQL ve Redis üzerindeki davranışı (ayrı `hellgrind_test` veritabanı).

PostgreSQL ya da Redis erişilemiyorsa (ör. CI) bu dosyadaki testler atlanır.
Konteynerde çalıştırmak için:
  docker compose run --rm -v "$PWD/backend:/app" -v "$PWD/db:/db:ro" api python -m pytest -q
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.budget import killswitch as ks
from app.db import session_scope


def project_row(pid):
    with session_scope() as s:
        status, spent, reserved = s.execute(
            text("SELECT status, spent_usd, reserved_usd FROM projects WHERE id = :p"), {"p": pid}).one()
    return status, float(spent), float(reserved)


def ledger_kinds(pid):
    with session_scope() as s:
        return [k for (k,) in s.execute(text("SELECT kind FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid})]


def event_kinds(pid):
    return [json.loads(e)["kind"] for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]


def add_ledger(pid, kind, amount, rid=None, age="0 minutes", provider="fal_wan:720p"):
    rid = rid or uuid.uuid4()
    with session_scope() as s:
        s.execute(text("""INSERT INTO cost_ledger (project_id, provider, kind, amount_usd, reservation, created_at)
                          VALUES (:p, :prov, CAST(:k AS ledger_kind), :a, :r, now() - CAST(:age AS interval))"""),
                  {"p": pid, "prov": provider, "k": kind, "a": amount, "r": rid, "age": age})
    return rid


# ---------- reserve / commit / release ----------

def test_reserve_holds_amount_and_writes_ledger(mk_project):
    pid = mk_project(limit=10)
    ks.reserve(pid, 2.5, "fal_wan:720p")
    assert project_row(pid) == ("producing", 0.0, 2.5)
    assert ledger_kinds(pid) == ["reserve"]


def test_reserve_over_limit_freezes_project_and_raises(mk_project):
    pid = mk_project(limit=10)
    ks.reserve(pid, 6, "seedance:720p")
    with pytest.raises(ks.BudgetExceeded):
        ks.reserve(pid, 5, "seedance:720p")
    status, spent, reserved = project_row(pid)
    assert (status, spent, reserved) == ("paused", 0.0, 6.0)   # reddedilen tutar ayrılmadı
    assert ks.r().exists(ks.FROZEN.format(pid))
    assert "frozen" in event_kinds(pid)
    assert ledger_kinds(pid) == ["reserve"]


def test_reserve_refuses_when_already_frozen(mk_project):
    pid = mk_project(limit=10)
    ks.freeze(pid, "manual")
    with pytest.raises(ks.ProjectFrozen):
        ks.reserve(pid, 0.01, "fal_wan:480p")
    assert project_row(pid)[2] == 0.0


def test_commit_moves_estimate_to_actual_and_tracks_scene_spend(mk_project, mk_scene):
    pid = mk_project(limit=100)
    sid = mk_scene(pid)
    rid = ks.reserve(pid, 10, "fal_wan:720p", scene_id=sid)
    ks.commit(pid, rid, estimate=10, actual=7.5, provider="fal_wan:720p", scene_id=sid)
    assert project_row(pid) == ("producing", 7.5, 0.0)
    assert ledger_kinds(pid) == ["reserve", "commit"]
    with session_scope() as s:
        assert float(s.execute(text("SELECT spent_usd FROM scenes WHERE id = :s"), {"s": sid}).scalar()) == 7.5


def test_soft_limit_flag_is_set_at_80_percent_without_freezing(mk_project):
    pid = mk_project(limit=10)
    rid = ks.reserve(pid, 7, "fal_wan:720p")
    ks.commit(pid, rid, 7, 7, "fal_wan:720p")
    assert not ks.is_soft_limited(pid)
    rid = ks.reserve(pid, 1.5, "fal_wan:720p")
    ks.commit(pid, rid, 1.5, 1.5, "fal_wan:720p")
    assert ks.is_soft_limited(pid)
    assert not ks.is_frozen(pid)
    assert "soft_limit" in event_kinds(pid)


def test_soft_limit_ratio_comes_from_project_settings(mk_project):
    pid = mk_project(limit=10, soft_budget_ratio=0.5)
    rid = ks.reserve(pid, 5, "fal_wan:720p")
    ks.commit(pid, rid, 5, 5, "fal_wan:720p")
    assert ks.is_soft_limited(pid)


def test_commit_reaching_limit_freezes_project(mk_project):
    pid = mk_project(limit=10)
    rid = ks.reserve(pid, 10, "fal_wan:720p")
    ks.commit(pid, rid, 10, 10, "fal_wan:720p")
    assert project_row(pid)[0] == "paused"
    assert ks.is_frozen(pid)


def test_actual_cost_above_estimate_still_trips_the_limit(mk_project):
    pid = mk_project(limit=10)
    rid = ks.reserve(pid, 5, "fal_wan:720p")
    ks.commit(pid, rid, 5, 12, "fal_wan:720p")   # sağlayıcı tahminden pahalıya çıktı
    status, spent, reserved = project_row(pid)
    assert (status, spent, reserved) == ("paused", 12.0, 0.0)


def test_release_returns_reservation(mk_project):
    pid = mk_project(limit=10)
    rid = ks.reserve(pid, 4, "fal_wan:720p")
    ks.release(pid, rid, 4, "fal_wan:720p")
    assert project_row(pid) == ("producing", 0.0, 0.0)
    assert ledger_kinds(pid) == ["reserve", "release"]


def test_spend_commits_actual_cost(mk_project):
    pid = mk_project(limit=10)
    with ks.spend(pid, 3, "fal_wan:720p") as charge:
        charge.actual = 2.0
    assert project_row(pid) == ("producing", 2.0, 0.0)
    assert ledger_kinds(pid) == ["reserve", "commit"]


def test_spend_commits_estimate_when_actual_not_set(mk_project):
    pid = mk_project(limit=10)
    with ks.spend(pid, 3, "fal_wan:720p"):
        pass
    assert project_row(pid) == ("producing", 3.0, 0.0)


def test_spend_releases_reservation_on_error(mk_project):
    pid = mk_project(limit=10)
    with pytest.raises(RuntimeError):
        with ks.spend(pid, 3, "fal_wan:720p"):
            raise RuntimeError("sağlayıcı hatası")
    assert project_row(pid) == ("producing", 0.0, 0.0)
    assert ledger_kinds(pid) == ["reserve", "release"]


def test_concurrent_reserves_never_exceed_limit(mk_project):
    pid = mk_project(limit=10)

    def attempt(_):
        try:
            ks.reserve(pid, 1.0, "fal_wan:720p")
            return True
        except ks.ProjectFrozen:
            return False

    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(attempt, range(20)))
    status, spent, reserved = project_row(pid)
    assert sum(results) == 10
    assert spent + reserved == 10.0
    assert status == "paused"


# ---------- freeze / resume ----------

def test_freeze_pauses_project_freezes_active_scenes_and_revokes_tasks(mk_project, mk_scene, monkeypatch):
    from app.worker.celery_app import celery
    revoked = []
    monkeypatch.setattr(celery.control, "revoke", lambda ids, *a, **kw: revoked.extend(ids))

    pid = mk_project()
    active = [mk_scene(pid, st) for st in ("pending", "probing", "generating", "correcting", "audio", "lipsync", "muxing")]
    untouched = {st: mk_scene(pid, st) for st in ("done", "refined", "needs_review", "parsed", "failed")}
    ks.register_task(pid, "task-a")
    ks.register_task(pid, "task-b")

    ks.freeze(pid, "manual test")

    assert project_row(pid)[0] == "paused"
    assert ks.r().get(ks.FROZEN.format(pid)) == "manual test"
    assert set(revoked) == {"task-a", "task-b"}
    with session_scope() as s:
        states = dict(s.execute(text("SELECT id::text, status FROM scenes WHERE project_id = :p"), {"p": pid}).all())
    assert all(states[i] == "frozen" for i in active)
    assert {st: states[i] for st, i in untouched.items()} == {st: st for st in untouched}
    assert event_kinds(pid)[0] == "frozen"


def test_freeze_does_not_reopen_killed_or_completed_project(mk_project):
    killed, completed = mk_project(status="killed"), mk_project(status="completed")
    ks.freeze(killed, "x")
    ks.freeze(completed, "x")
    assert project_row(killed)[0] == "killed"
    assert project_row(completed)[0] == "completed"


def test_resume_reverses_freeze(mk_project, mk_scene):
    pid = mk_project()
    sid = mk_scene(pid, "generating")
    ks.freeze(pid, "test")
    ks.r().set(ks.SOFT.format(pid), "1")
    assert ks.is_frozen(pid)

    ks.resume(pid)

    assert not ks.is_frozen(pid)
    assert not ks.is_soft_limited(pid)
    assert project_row(pid)[0] == "producing"
    with session_scope() as s:
        assert s.execute(text("SELECT status FROM scenes WHERE id = :s"), {"s": sid}).scalar() == "pending"
    assert event_kinds(pid)[0] == "resumed"
    ks.reserve(pid, 1, "fal_wan:480p")   # devam sonrası rezervasyon yine alınabilir


@pytest.mark.parametrize("status,frozen", [("paused", True), ("killed", True), ("producing", False), ("draft", False)])
def test_is_frozen_falls_back_to_database_status(mk_project, status, frozen):
    pid = mk_project(status=status)
    assert not ks.r().exists(ks.FROZEN.format(pid))
    assert ks.is_frozen(pid) is frozen


def test_event_log_keeps_last_200(mk_project):
    pid = mk_project()
    for i in range(250):
        ks.event(pid, "tick", i=i)
    assert ks.r().llen(ks.EVENTS.format(pid)) == 200
    newest = json.loads(ks.r().lindex(ks.EVENTS.format(pid), 0))
    assert newest["i"] == 249


# ---------- Celery beat bekçileri ----------

def test_beat_schedule_wires_guards_to_registered_tasks():
    """Veritabanı gerektirmez: zamanlama 30 sn / 5 dk ve görev adları gerçekten kayıtlı."""
    from app.worker import tasks  # noqa: F401  (görevleri kaydeder)
    from app.worker.celery_app import celery

    schedule = celery.conf.beat_schedule
    assert schedule["budget-watchdog"] == {"task": "hg.watchdog", "schedule": 30.0}
    assert schedule["reap-stale-reservations"] == {"task": "hg.reap_stale", "schedule": 300.0}
    assert {"hg.watchdog", "hg.reap_stale"} <= set(celery.tasks)


def test_watchdog_freezes_projects_that_spend_too_fast(mk_project):
    fast = mk_project(max_spend_per_10min=5)
    slow = mk_project(max_spend_per_10min=5)
    old = mk_project(max_spend_per_10min=5)
    idle = mk_project(status="paused", max_spend_per_10min=5)
    add_ledger(fast, "commit", 6)
    add_ledger(slow, "commit", 4)
    add_ledger(old, "commit", 6, age="20 minutes")   # 10 dakika penceresinin dışında
    add_ledger(idle, "commit", 6)                     # zaten duraklatılmış

    tripped = ks.watchdog()

    assert fast in tripped
    assert not {slow, old, idle} & set(tripped)
    assert project_row(fast)[0] == "paused"
    assert project_row(slow)[0] == project_row(old)[0] == "producing"


def test_watchdog_ignores_reserve_and_release_rows(mk_project):
    pid = mk_project(max_spend_per_10min=5)
    add_ledger(pid, "reserve", 50)
    add_ledger(pid, "release", 50)
    assert pid not in ks.watchdog()


def test_reaper_releases_only_stale_unmatched_reservations(mk_project):
    pid = mk_project(limit=100)
    with session_scope() as s:
        s.execute(text("UPDATE projects SET reserved_usd = 6 WHERE id = :p"), {"p": pid})
    orphan = add_ledger(pid, "reserve", 2, age="2 hours")                 # worker çökmüş, eşi yok
    settled = add_ledger(pid, "reserve", 3, age="2 hours")                # commit ile kapanmış
    add_ledger(pid, "commit", 3, rid=settled, age="2 hours")
    add_ledger(pid, "reserve", 1, age="5 minutes")                        # henüz bayat değil

    ks.reap_stale_reservations()

    assert project_row(pid)[2] == 4.0
    with session_scope() as s:
        released = {str(r) for (r,) in s.execute(
            text("SELECT reservation FROM cost_ledger WHERE project_id = :p AND kind = 'release'"), {"p": pid})}
    assert released == {str(orphan)}

    ks.reap_stale_reservations()                                           # ikinci çalışma aynı rezervasyonu tekrar iade etmez
    assert project_row(pid)[2] == 4.0
