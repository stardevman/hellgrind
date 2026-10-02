"""Aşama zincirleri, kuyruk yönlendirmesi ve görev koruma sarmalayıcısı.

Zincir ve kuyruk testleri veritabanı gerektirmez; `_guard` testleri PostgreSQL/Redis yoksa atlanır.
"""
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text

from app.budget import killswitch as ks
from app.core.rerender import ALL, Stage
from app.db import session_scope
from app.worker.celery_app import celery
from app.worker.dispatch import build_chain


def names(chain):
    return [t.task for t in chain.tasks]


# ---------- build_chain ----------

def test_full_chain_runs_stages_in_order():
    assert names(build_chain("p", "s", ALL)) == ["hg.generate_scene", "hg.lipsync_scene", "hg.mux_scene"]


def test_partial_plans_skip_earlier_stages():
    assert names(build_chain("p", "s", Stage.LIPSYNC | Stage.MUX)) == ["hg.lipsync_scene", "hg.mux_scene"]
    assert names(build_chain("p", "s", Stage.MUX)) == ["hg.mux_scene"]


def test_nothing_to_do_builds_no_chain():
    assert build_chain("p", "s", Stage.NONE) is None


def test_chain_links_are_immutable_so_results_are_not_passed_along():
    for sig in build_chain("p", "s", ALL).tasks:
        assert sig.immutable
        assert not sig.args and sig.kwargs == {"project_id": "p", "scene_id": "s"}


@pytest.mark.parametrize("task,queue", [
    ("hg.generate_scene", "gen"),
    ("hg.lipsync_scene", "post"),
    ("hg.mux_scene", "post"),
    ("hg.assemble_timeline", "post"),
    ("hg.watchdog", "control"),
    ("hg.reap_stale", "control"),
])
def test_tasks_are_routed_to_their_queues(task, queue):
    assert celery.amqp.router.route({}, task)["queue"].name == queue


# ---------- _guard ----------

def scene_row(sid):
    with session_scope() as s:
        return s.execute(text("SELECT status, last_error FROM scenes WHERE id = :s"), {"s": sid}).one()


def fake_task(task_id="task-123"):
    return NS(request=NS(id=task_id))


def test_guard_skips_work_for_frozen_project(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid, "pending")
    ks.freeze(pid, "test")
    calls = []

    assert _guard(fake_task(), pid, sid, lambda: calls.append(1)) == "frozen"
    assert calls == []
    assert scene_row(sid)[0] == "frozen"


def test_guard_registers_task_while_running_and_returns_result(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)
    seen = []

    def work():
        seen.append(ks.r().sismember(ks.TASKS.format(pid), "task-123"))
        return "ok"

    assert _guard(fake_task(), pid, sid, work) == "ok"
    assert seen == [True]
    assert not ks.r().sismember(ks.TASKS.format(pid), "task-123")   # iş bitince kayıt silinir


def test_guard_records_last_error_and_reraises(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)

    def boom():
        raise ValueError("ffmpeg çöktü")

    with pytest.raises(ValueError):
        _guard(fake_task(), pid, sid, boom)
    status, last_error = scene_row(sid)
    assert status == "failed"
    assert last_error.startswith("ValueError: ffmpeg çöktü")
    assert not ks.r().sismember(ks.TASKS.format(pid), "task-123")


def test_guard_turns_budget_stop_into_frozen_scene_not_failure(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)

    def over_budget():
        raise ks.BudgetExceeded(pid)

    assert _guard(fake_task(), pid, sid, over_budget) == "frozen"
    status, last_error = scene_row(sid)
    assert status == "frozen" and last_error is None
