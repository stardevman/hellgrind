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


class TaskAbandoned(RuntimeError):
    """Görev çok kez yeniden teslim edildi (worker süreci her seferinde öldürüldü): bir daha çalıştırılmaz."""


MAX_DELIVERIES = 3            # aynı görev (kimliği) en çok bu kadar çalışır; worker süreci öldürülünce yeniden teslim edilir
DELIVERIES = "hg:deliveries:{}"


def _delivery(task, project_id: str, scene_id: str) -> int:
    """Görevin kaçıncı teslimi. Celery acks_late + reject_on_worker_lost: worker süreci öldürülürse (ör. bellek: SIGKILL)
    görev baştan çalışır. Yeniden teslimde önceki denemenin kapanmamış rezervasyonları iade edilir (RunPod işi sürüyorsa
    yeni deneme aynı işi bekler ve kendi rezervasyonunu alır)."""
    task_id = getattr(task.request, "id", None)
    if not task_id:
        return 1
    key = DELIVERIES.format(task_id)
    n = int(killswitch.r().incr(key))
    killswitch.r().expire(key, 7 * 3600)
    if n > 1:
        released = killswitch.release_orphans(project_id, scene_id)
        killswitch.event(project_id, "task_redelivered", scene_id=scene_id, delivery=n, released=released,
                         task=getattr(task, "name", None))
    return n


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
    if _delivery(task, project_id, scene_id) > MAX_DELIVERIES:      # süreç her seferinde öldürülüyor: döngüyü kes
        note = (f"Görev {MAX_DELIVERIES} kez yarıda kesildi (worker süreci sonlandı; bellek yetersiz olabilir). Döngüyü "
                "önlemek için durduruldu; worker günlüğüne bakıp çekimi yeniden üretin.")
        _set_status(scene_id, "failed", note)
        killswitch.event(project_id, "task_abandoned", scene_id=scene_id, task=getattr(task, "name", None))
        raise TaskAbandoned(note)                 # zincirin sonraki halkaları çalışmaz; görev onaylanır, yeniden gelmez
    killswitch.register_task(project_id, task.request.id)
    try:
        return work()
    except killswitch.ProjectFrozen:
        _set_status(scene_id, "frozen")
        return "frozen"
    except Exception as e:
        log.exception("görev hatası scene=%s", scene_id)
        _set_status(scene_id, "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        killswitch.refreeze_after(project_id, scene_id)       # tek çekim için çözülen proje yeniden donar
        raise
    finally:
        killswitch.unregister_task(project_id, task.request.id)


# ---------- sahne aşamaları ----------

@celery.task(bind=True, name="hg.storyboard_scene")
def storyboard_scene(self, project_id: str, scene_id: str, standalone: bool = False, batch: str | None = None,
                     paid: bool = False):
    """batch: toplu dağıtımın parçası; bitince (başarılı ya da değil) bariyere haber verir, son kareden sonra videolar
    kuyruğa girer."""
    def work():
        from app.engine.storyboard import make_board
        return make_board(project_id, scene_id, standalone=standalone, **({"explicit": True} if paid else {}))
    ok = False
    try:
        result = _guard(self, project_id, scene_id, work)
        ok = result != "frozen"
        return result
    finally:
        if batch:
            from app.worker.dispatch import batch_done
            batch_done(batch, scene_id, ok)


@celery.task(bind=True, name="hg.end_board_scene")
def end_board_scene(self, project_id: str, scene_id: str):
    """Çekim sonu karesini (Kn_end) yeni seed'lerle yeniden çizer (yönetmenin isteği); görüntü üretilmez."""
    def work():
        from app.engine.storyboard import make_end_board
        return make_end_board(project_id, scene_id, redraw=True) or "skip (son kare gerekmiyor)"
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.generate_scene")
def generate_scene(self, project_id: str, scene_id: str, mode: str = "full", batch: str | None = None):
    """batch: model gruplu toplu dağıtımın parçası; bitince (başarılı ya da değil) bariyere haber verir, grubun son işi
    bitince sıradaki model grubu kuyruğa girer."""
    release_dispatch(scene_id)          # kuyruk aralığı bitti: sahne artık durum ve görev kaydıyla korunur

    def work():
        from app.engine.iteration import run_tiered_generation
        return run_tiered_generation(project_id, scene_id, mode)
    ok = False
    try:
        result = _guard(self, project_id, scene_id, work)
        ok = result != "frozen"
        return result
    finally:
        if batch:
            from app.worker.dispatch import batch_done
            batch_done(batch, scene_id, ok)


@celery.task(bind=True, name="hg.override_full")
def override_full(self, project_id: str, scene_id: str, variation_id: str):
    """Yönetmen seçimi: seçilen adayın seed'iyle tam tur klibi (iteration.run_override_full)."""
    release_dispatch(scene_id)

    def work():
        from app.engine.iteration import run_override_full
        return run_override_full(project_id, scene_id, variation_id)
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
    killswitch.refreeze_after(project_id, scene_id)
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
    from app.quality.consistency import auto_match_report, check_project
    report = check_project(project_id, only_scene=scene_id)
    killswitch.event(project_id, "continuity", jumps=sum(p["jump"] for p in report["pairs"]), pairs=len(report["pairs"]))
    matched = []
    from app.config import settings
    # tam denetim: otomatik çekim eşleştirme (ücretli modda kendiliğinden; ücretsiz modun prosedürel çekimleri kasıtlı
    # olarak farklı palette, orada Renk sayfasındaki düğmeyle)
    if scene_id is None and not settings.dry_run and not killswitch.is_frozen(project_id):
        matched = apply_auto_match(project_id, auto_match_report(project_id))
    return {"jumps": sum(p["jump"] for p in report["pairs"]), "matched": matched}


@celery.task(name="hg.auto_match")
def auto_match_task(project_id: str):
    """Elle istenen otomatik çekim eşleştirmesi. Çalıştığı ve sonucu (değişen ve atlanan çekimler) her durumda olaya yazılır."""
    from app.quality.consistency import auto_match_report
    killswitch.event(project_id, "color_auto_started")
    try:
        report = auto_match_report(project_id)
    except Exception as e:                              # noqa: BLE001 — iz kalsın
        killswitch.event(project_id, "color_auto_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        raise
    return {"matched": apply_auto_match(project_id, report)}


def apply_auto_match(project_id: str, report: dict | list) -> list[str]:
    """Otomatik eşleştirilen çekimlerde yalnızca GRADE + MUX çalışır; montaj bitince süreklilik yeniden ölçülür. Sonuç
    (değişenler, atlananlar ve sebepleri, referans çekim) olaya yazılır; değişen yoksa da."""
    if isinstance(report, list):
        report = {"changed": report, "skipped": [], "references": {}}
    scene_ids = report["changed"]
    if not scene_ids:
        killswitch.event(project_id, "color_auto_matched", codes=[], skipped=report.get("skipped", []),
                         references=report.get("references", {}))
        return []
    from app.api.plan import dispatch_result
    from app.core.rerender import Stage
    from app.db import session_scope
    codes = []
    with session_scope() as s:
        for sid in scene_ids:
            sc = s.get(Scene, sid)
            try:
                dispatch_result(s, sc, allowed=Stage.GRADE | Stage.MUX)
                codes.append(sc.scene_code)
            except Exception as e:                      # noqa: BLE001 — meşgul ya da donmuş: ayar yazıldı, sonra uygulanır
                killswitch.event(project_id, "color_auto_failed", scene_id=sid, error=str(e)[:200])
    killswitch.event(project_id, "color_auto_matched", codes=codes, skipped=report.get("skipped", []),
                     references=report.get("references", {}))
    return codes


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
    if settings.dry_run or settings.h3_platform != "vast" or not settings.vast_instance_id:
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


@celery.task(name="hg.h3_retrieve")
def h3_retrieve(instance_id: str, destroy_after: bool = False):
    """Çıktı güvencesi: makinenin yerel depoda olmayan çıktılarını indirir ve doğrular (makine gerekirse başlatılır);
    destroy_after ve hepsi indirildiyse eski makine silinir. Ücretsiz modda hiçbir şey yapmaz."""
    if settings.dry_run:
        return "skip"
    from app.live import vast_box                 # ücretli modül: yalnızca fonksiyon içinde
    return vast_box.retrieve_outputs(instance_id, destroy_after)


@celery.task(name="hg.runpod_tick")
def runpod_tick():
    """RunPod Serverless: boşta bekleyen worker'ların bedeli, bakiye ve uç nokta ayarı, yarım kalmış indirmeler (dakikada
    bir). Ücretsiz modda ya da platform RunPod değilse hiçbir şey yapmaz."""
    if settings.dry_run or settings.h3_platform != "runpod" or not settings.runpod_endpoint_id:
        return "skip"
    from app.live import runpod_h3                # ücretli modül: yalnızca fonksiyon içinde
    return runpod_h3.tick()
