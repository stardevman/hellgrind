"""Aşama zincirleri ve kuyruk yönlendirmesi. Veritabanı gerektirmez."""
import pytest

from app.core.rerender import ALL, Stage
from app.worker.celery_app import celery
from app.worker.dispatch import build_chain


def names(chain):
    return [t.task for t in chain.tasks]


def test_full_plan_chains_all_stages_in_pipeline_order():
    assert names(build_chain("p", "s", ALL)) == ["hg.storyboard_scene", "hg.generate_scene", "hg.lipsync_scene",
                                                 "hg.mux_scene"]


@pytest.mark.parametrize("stages,expected", [
    (Stage.LIPSYNC | Stage.MUX, ["hg.lipsync_scene", "hg.mux_scene"]),
    (Stage.MUX, ["hg.mux_scene"]),
    (Stage.VISUAL, ["hg.generate_scene"]),
])
def test_partial_plans_only_chain_the_planned_stages(stages, expected):
    assert names(build_chain("p", "s", stages)) == expected


def test_empty_plan_builds_no_chain():
    assert build_chain("p", "s", Stage.NONE) is None


def test_links_are_immutable_so_results_are_not_passed_between_stages():
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


def test_worker_consumes_every_queue_that_tasks_are_routed_to():
    assert {q.name for q in celery.conf.task_queues} == {"gen", "post", "control"}
