"""Celery görevleri. Worker'lar durumsuzdur: girdi yalnızca kimliklerdir, gerisini veritabanından okurlar.

Görev gövdeleri iş modüllerini (engine, post) görev içinde yükler: worker açılışı hafif kalır ve
bu modül, onlardan bağımsız test edilebilir.
"""
import logging
import traceback

from app.budget import killswitch
from app.config import settings
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

@celery.task(bind=True, name="hg.storyboard_scene")
def storyboard_scene(self, project_id: str, scene_id: str, standalone: bool = False):
    def work():
        from app.engine.storyboard import make_board
        return make_board(project_id, scene_id, standalone=standalone)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.generate_scene")
def generate_scene(self, project_id: str, scene_id: str, mode: str = "full"):
    release_dispatch(scene_id)          # kuyruk aralığı bitti: sahne artık durum ve görev kaydıyla korunur

    def work():
        from app.engine.iteration import run_tiered_generation
        return run_tiered_generation(project_id, scene_id, mode)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.lipsync_scene")
def lipsync_scene(self, project_id: str, scene_id: str):
    def work():
        from app.post import pipeline
        return pipeline.voice_and_lipsync(scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.grade_scene")
def grade_scene(self, project_id: str, scene_id: str):
    def work():
        from app.post import pipeline
        return pipeline.grade_scene(scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.mux_scene")
def mux_scene(self, project_id: str, scene_id: str):
    def work():
        from app.post import pipeline
        return pipeline.mux_scene(scene_id)
    result = _guard(self, project_id, scene_id, work)
    maybe_assemble_draft(project_id, scene_id)
    maybe_refresh_continuity(project_id, scene_id)
    return result


IN_FLIGHT = ("pending", "storyboard", "probing", "generating", "correcting", "refined", "audio", "lipsync", "grading",
             "muxing")


def maybe_refresh_continuity(project_id: str, scene_id: str) -> None:
    """Süreklilik raporu olan projede montajı yenilenen çekimin komşu geçişleri yeniden ölçülür (öneri uygulanınca
    uyarı kalksın)."""
    with session_scope() as s:
        from app.models import Project
        p = s.get(Project, project_id)
        if p is None or not p.continuity:
            return
    continuity.delay(project_id, scene_id)


@celery.task(name="hg.continuity")
def continuity(project_id: str, scene_id: str | None = None):
    """Süreklilik kontrolü (renk sıçramaları). scene_id verilirse yalnızca o çekimin komşu geçişleri yenilenir."""
    from app.quality.consistency import check_project
    report = check_project(project_id, only_scene=scene_id)
    killswitch.event(project_id, "continuity", jumps=sum(p["jump"] for p in report["pairs"]), pairs=len(report["pairs"]))
    return {"jumps": sum(p["jump"] for p in report["pairs"])}


def maybe_assemble_draft(project_id: str, scene_id: str) -> None:
    """Taslak geçişin son çekimi bitince kaba kurgu (animatik) kendiliğinden birleştirilir. Ücretsizdir."""
    from sqlalchemy import func, select
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        if scene is None or scene.cut_tier != "draft" or scene.status != "done":
            return
        busy = s.scalar(select(func.count()).select_from(Scene).where(Scene.project_id == project_id,
                                                                      Scene.status.in_(IN_FLIGHT)))
    if not busy:
        assemble_timeline.delay(project_id, "draft")


@celery.task(name="hg.assemble_timeline")
def assemble_timeline(project_id: str, kind: str = "final"):
    from app.post.timeline import assemble
    try:
        result = assemble(project_id, kind)
    except Exception as e:
        killswitch.event(project_id, "timeline_failed", error=f"{type(e).__name__}: {e}", cut=kind)
        raise
    if "error" in result:                                  # tamamlanmış sahne yok: başarı olayı yazılmaz
        killswitch.event(project_id, "timeline_failed", error=result["error"], cut=kind)
    else:
        killswitch.event(project_id, "timeline", cut=kind, **{k: v for k, v in result.items() if k != "cutlist_key"})
        if kind == "final":                                # film birleşince süreklilik denetlenir
            continuity.delay(project_id)
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


@celery.task(name="hg.h3_idle")
def h3_idle():
    """MiniMax H3 makinesi: boşta bekleme bedeli ve boşta durdurma (dakikada bir). Ücretsiz modda hiçbir şey yapmaz."""
    if settings.dry_run or not settings.vast_instance_id:
        return "skip"
    from app.live import vast_box, vast_h3        # ücretli modüller: yalnızca fonksiyon içinde
    return vast_box.idle_tick(queue_empty=vast_h3.queue_empty if settings.h3_endpoint_url else None)


@celery.task(name="hg.h3_replace")
def h3_replace(offer_id: int):
    """Yedek MiniMax H3 makinesi: teklifi kiralar, setup.sh kurulumunu bekler, Ayarlar'ı günceller, eski makineyi siler.
    Kurulum (modeller ~100 GB) bir saati bulabilir. Ücretsiz modda hiçbir şey yapmaz."""
    if settings.dry_run:
        return "skip"
    from app.live import vast_box                 # ücretli modül: yalnızca fonksiyon içinde
    return vast_box.replace(offer_id)
