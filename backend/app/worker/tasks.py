"""Celery görevleri. Worker'lar durumsuzdur: girdi yalnızca kimliklerdir, gerisini veritabanından okurlar.

Görev gövdeleri iş modüllerini (engine, post) görev içinde yükler: worker açılışı hafif kalır ve
bu modül, onlardan bağımsız test edilebilir.
"""
import logging
import traceback

from app.budget import killswitch
from app.db import session_scope
from app.models import Scene
from app.worker.celery_app import celery
from app.worker.dispatch import release_dispatch

log = logging.getLogger("hellgrind.tasks")


def _set_status(scene_id: str, status: str, error: str | None = None) -> None:
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        if scene is not None:
            scene.status = status
            if error:
                scene.last_error = error


def _guard(task, project_id: str, scene_id: str, work):
    """Her sahne görevinin ortak sarmalayıcısı: Kill-Switch kontrolü, görev kaydı, hata kaydı.

    - Proje donmuşsa iş başlamaz; sahne `frozen` olur.
    - Görev kimliği Redis'e yazılır: freeze() kuyruktakileri bu kayıttan iptal eder.
    - Bütçe durdurması (ProjectFrozen) hata sayılmaz: sahne `frozen` olur.
    - Başka her hata `last_error`'a yazılır, sahne `failed` olur ve hata yeniden fırlatılır; böylece
      zincirin sonraki halkaları çalışmaz.
    """
    if killswitch.is_frozen(project_id):
        _set_status(scene_id, "frozen")
        return "frozen"
    killswitch.register_task(project_id, task.request.id)
    try:
        return work()
    except killswitch.ProjectFrozen:
        _set_status(scene_id, "frozen")
        return "frozen"
    except Exception as e:
        log.exception("görev hatası scene=%s", scene_id)
        _set_status(scene_id, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        raise
    finally:
        killswitch.unregister_task(project_id, task.request.id)


# ---------- sahne aşamaları ----------

@celery.task(bind=True, name="hg.generate_scene")
def generate_scene(self, project_id: str, scene_id: str):
    release_dispatch(scene_id)          # kuyruk aralığı bitti: sahne artık durum ve görev kaydıyla korunur

    def work():
        from app.engine.iteration import run_tiered_generation
        return run_tiered_generation(project_id, scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.lipsync_scene")
def lipsync_scene(self, project_id: str, scene_id: str):
    def work():
        from app.post import pipeline
        return pipeline.voice_and_lipsync(scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.mux_scene")
def mux_scene(self, project_id: str, scene_id: str):
    def work():
        from app.post import pipeline
        return pipeline.mux_scene(scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(name="hg.assemble_timeline")
def assemble_timeline(project_id: str):
    from app.post.timeline import assemble
    try:
        result = assemble(project_id)
    except Exception as e:
        killswitch.event(project_id, "timeline_failed", error=f"{type(e).__name__}: {e}")
        raise
    if "error" in result:                                  # tamamlanmış sahne yok: başarı olayı yazılmaz
        killswitch.event(project_id, "timeline_failed", error=result["error"])
    else:
        killswitch.event(project_id, "timeline", **{k: v for k, v in result.items() if k != "cutlist_key"})
    return result


# ---------- varlıklar ----------

@celery.task(bind=True, name="hg.motion_test")
def motion_test(self, project_id: str, variation_id: str):
    """Varlığın hareket testi. Dondurulmuş projede başlamaz; test satırı `failed` olur."""
    killswitch.register_task(project_id, self.request.id)
    try:
        from app.engine.motion_test import run
        return run(project_id, variation_id)
    except killswitch.ProjectFrozen:
        return "frozen"
    finally:
        killswitch.unregister_task(project_id, self.request.id)


# ---------- Kill-Switch bekçileri (Celery beat) ----------

@celery.task(name="hg.watchdog")
def watchdog():
    return killswitch.watchdog()


@celery.task(name="hg.reap_stale")
def reap_stale():
    return killswitch.reap_stale_reservations()
