import logging
import traceback

from app.budget import killswitch
from app.db import session_scope
from app.engine.iteration import run_tiered_generation
from app.models import Scene
from app.post import pipeline
from app.worker.celery_app import celery

log = logging.getLogger("hellgrind.tasks")


def _guard(task, project_id, scene_id, fn):
    """Ortak sarmalayıcı: Kill-Switch kontrolü, görev kaydı, hata yakalama."""
    if killswitch.is_frozen(project_id):
        _status(scene_id, "frozen")
        return "frozen"
    killswitch.register_task(project_id, task.request.id)
    try:
        return fn()
    except killswitch.ProjectFrozen:
        _status(scene_id, "frozen")
        return "frozen"
    except Exception as e:
        log.exception("görev hatası scene=%s", scene_id)
        _status(scene_id, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        raise
    finally:
        killswitch.unregister_task(project_id, task.request.id)


def _status(scene_id, status, error=None):
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        if sc:
            sc.status = status
            if error:
                sc.last_error = error


@celery.task(bind=True, name="hg.generate_scene")
def generate_scene(self, project_id: str, scene_id: str):
    return _guard(self, project_id, scene_id, lambda: run_tiered_generation(project_id, scene_id))


@celery.task(bind=True, name="hg.lipsync_scene")
def lipsync_scene(self, project_id: str, scene_id: str):
    return _guard(self, project_id, scene_id, lambda: pipeline.voice_and_lipsync(scene_id))


@celery.task(bind=True, name="hg.mux_scene")
def mux_scene(self, project_id: str, scene_id: str):
    return _guard(self, project_id, scene_id, lambda: pipeline.mux_scene(scene_id))


@celery.task(name="hg.watchdog")
def watchdog():
    return killswitch.watchdog()


@celery.task(name="hg.reap_stale")
def reap_stale():
    return killswitch.reap_stale_reservations()
