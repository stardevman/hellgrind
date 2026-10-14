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
                     paid: bool = False, keep_status: bool = False):
    """batch: toplu dağıtımın parçası; bitince (başarılı ya da değil) bariyere haber verir, son kareden sonra videolar
    kuyruğa girer."""
    def work():
        from app.engine.storyboard import make_board
        return make_board(project_id, scene_id, standalone=standalone, keep_status=keep_status,
                          **({"explicit": True} if paid else {}))
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


@celery.task(bind=True, name="hg.complete_draft")
def complete_draft(self, project_id: str, scene_id: str):
    """Seedance 2.5 taslağını 1080p'ye tamamlar (engine/finishing.run_complete)."""
    def work():
        from app.engine.finishing import run_complete
        return run_complete(project_id, scene_id)
    return _guard(self, project_id, scene_id, work)


@celery.task(bind=True, name="hg.upscale_clip")
def upscale_clip(self, project_id: str, scene_id: str, variation_id: str, endpoint: str | None = None,
                 batch: bool = False):
    """Klibi 1080p'ye çıkarır (engine/finishing.run_upscale); orijinal varyasyon olarak kalır."""
    def work():
        from app.engine.finishing import run_upscale
        return run_upscale(project_id, variation_id, endpoint, batch)
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
    step = round_step(project_id, scene_id)
    if step is None:                                       # renk eşleme turunun çekimi değil
        maybe_assemble_draft(project_id, scene_id)
        from app.engine.finishing import upscale_done      # bütün film upscale'i: son çekimden sonra film birleşir
        if not upscale_done(project_id, scene_id):
            maybe_rematch(project_id)
    elif step:                                             # turun son çekimi: tur biter, film bir kez birleşir
        end_round(project_id)
    maybe_refresh_continuity(project_id, scene_id)
    killswitch.refreeze_after(project_id, scene_id)
    return result


IN_FLIGHT = ("pending", "storyboard", "probing", "generating", "correcting", "refined", "audio", "lipsync", "grading",
             "muxing")


def maybe_rematch(project_id: str) -> bool:
    """Klibi değişen (upscale, taslak tamamlama, yükseltme) çekimler "eşleme bekliyor"sa ve projede süren iş kalmadıysa
    otomatik renk eşleme montajdan sonra çalışır (ücretli modda; ücretsiz modun prosedürel çekimleri kasıtlı olarak farklı
    palettedir). True: süreklilik + eşleme kuyruğa verildi."""
    from app.models import Scene as SceneRow
    from sqlalchemy import select
    if settings.dry_run or killswitch.is_frozen(project_id) or round_active(project_id):
        return False
    with session_scope() as s:
        rows = s.scalars(select(SceneRow).where(SceneRow.project_id == project_id)).all()
        pending = [r for r in rows if (r.grade_auto or {}).get("pending")]
        if not pending or any(r.status in IN_FLIGHT for r in rows):
            return False
        for r in pending:
            r.grade_auto = {k: v for k, v in (r.grade_auto or {}).items() if k != "pending"} or None
    continuity.delay(project_id, None, True, True)      # tam denetim + tek tur eşleme; tur bitince film birleşir
    killswitch.event(project_id, "color_rematch", shots=len(pending))
    return True


# ---------- otomatik renk eşleme turu ----------
# Eşleme tek turda biter: turun GRADE + MUX işleri yeni bir eşleme ya da kaba kurgu tetiklemez; turun son montajından
# sonra (bütün çekimler hazırsa) film bir kez birleşir ve bu birleşme eşlemeyi yeniden başlatmaz.

ROUND = "hg:color_round:{}"
ROUND_TTL_S = 2 * 3600                  # turun bir işi düşerse tur kendiliğinden kapanır


def round_active(project_id: str) -> bool:
    return bool(killswitch.r().exists(ROUND.format(project_id)))


def _round_begin(project_id: str, scene_ids: list[str]) -> None:
    r = killswitch.r()
    key = ROUND.format(project_id)
    r.delete(key)
    if scene_ids:
        r.sadd(key, *scene_ids)
        r.expire(key, ROUND_TTL_S)


def round_step(project_id: str, scene_id: str) -> bool | None:
    """Turun bir çekiminin montajı bitti. None: çekim turda değil; True: turun son çekimi."""
    r = killswitch.r()
    key = ROUND.format(project_id)
    if not r.srem(key, scene_id):
        return None
    if r.scard(key) == 0:
        r.delete(key)
        return True
    return False


def not_ready(project_id: str) -> list[str]:
    """Bitmemiş çekimlerin kodları. Otomatik montaj (kaba kurgu, upscale sonrası, renk eşleme sonrası) yalnızca bütün
    çekimler bitince yapılır; eksik montaj kendiliğinden üretilmez (elle birleştirme her zaman mümkün)."""
    from sqlalchemy import select
    from app.models import Scene as SceneRow
    with session_scope() as s:
        return [code for code, status in s.execute(select(SceneRow.scene_code, SceneRow.status)
                                                   .where(SceneRow.project_id == project_id)
                                                   .order_by(SceneRow.seq_order)) if status != "done"]


def auto_assemble(project_id: str, kind: str = "final", reason: str = "") -> bool:
    """Bütün çekimler hazırsa filmi birleştirir (bu birleşme renk eşlemeyi yeniden başlatmaz); değilse olay yazar."""
    waiting = not_ready(project_id)
    if waiting:
        killswitch.event(project_id, "auto_assemble_skipped", cut=kind, reason=reason, waiting=waiting[:12],
                         count=len(waiting))
        return False
    assemble_timeline.delay(project_id, kind, False)
    return True


def end_round(project_id: str) -> None:
    killswitch.event(project_id, "color_round_done")
    auto_assemble(project_id, "final", "renk eşleme")


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
def continuity(project_id: str, scene_id: str | None = None, auto: bool = True, assemble: bool = False):
    """Süreklilik kontrolü (renk sıçramaları). scene_id verilirse yalnızca o çekimin komşu geçişleri yenilenir.
    auto: tam denetimde otomatik eşleme turu (ücretli modda); tur sürerken yeni tur açılmaz. assemble: eşleme hiçbir
    çekimi değiştirmese de film birleştirilir (klipleri değişmiş, ör. upscale)."""
    from app.quality.consistency import auto_match_report, check_project
    report = check_project(project_id, only_scene=scene_id)
    killswitch.event(project_id, "continuity", jumps=sum(p["jump"] for p in report["pairs"]), pairs=len(report["pairs"]))
    matched = []
    from app.config import settings
    # tam denetim: otomatik çekim eşleştirme (ücretli modda kendiliğinden; ücretsiz modun prosedürel çekimleri kasıtlı
    # olarak farklı palette, orada Renk sayfasındaki düğmeyle)
    if scene_id is None and auto and not settings.dry_run and not killswitch.is_frozen(project_id) \
            and not round_active(project_id):
        _clear_pending(project_id)                   # bu tur bekleyen bütün eşlemeleri kapsar (yeniden tetiklenmez)
        matched = apply_auto_match(project_id, auto_match_report(project_id), assemble=assemble)
    elif assemble:
        auto_assemble(project_id, "final", "klipler değişti")
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


def _clear_pending(project_id: str) -> None:
    from sqlalchemy import select
    from app.models import Scene as SceneRow
    with session_scope() as s:
        for r in s.scalars(select(SceneRow).where(SceneRow.project_id == project_id)):
            if (r.grade_auto or {}).get("pending"):
                r.grade_auto = {k: v for k, v in r.grade_auto.items() if k != "pending"} or None


def _failure_reason(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    text = detail if isinstance(detail, str) and detail else str(e)
    return (text or type(e).__name__)[:200]


def _not_dispatched(result: dict) -> str:
    """dispatch_result kuyruğa hiçbir şey vermediyse sebebi (Türkçe)."""
    if result.get("blocked"):
        return "eksik referans: " + ", ".join(m.get("name", "?") for m in result["blocked"].get("missing", [])) \
            if isinstance(result["blocked"], dict) and result["blocked"].get("missing") else "çekim engelli"
    if result.get("awaiting_plan"):
        return "plan onayı bekliyor"
    if result.get("awaiting_board"):
        return "storyboard onayı bekliyor"
    return "yalnızca renk ve montaj yetmiyor (önce ses ya da dudak senkronu gerekli)"


def apply_auto_match(project_id: str, report: dict | list, assemble: bool = False) -> list[str]:
    """Otomatik eşleştirilen çekimlerde yalnızca GRADE + MUX çalışır (görüntü asla). Kuyruğa verilen çekimler tek bir
    turdur: turun montajları yeni eşleme ya da kaba kurgu tetiklemez, son montajdan sonra film bir kez birleşir. Sonuç
    (değişenler, atlananlar ve sebepleri, referans çekim) olaya yazılır; değişen yoksa da. Uygulanamayan çekimin
    sebebi color_auto_failed olayına yazılır."""
    if isinstance(report, list):
        report = {"changed": report, "skipped": [], "references": {}}
    from app.api.plan import dispatch_result
    from app.core.rerender import Stage
    from app.db import session_scope
    codes, queued = [], []
    with session_scope() as s:
        for sid in report["changed"]:
            sc = s.get(Scene, sid)
            try:
                result = dispatch_result(s, sc, allowed=Stage.GRADE | Stage.MUX, post_only=True)
            except Exception as e:                      # noqa: BLE001 — meşgul ya da donmuş: ayar yazıldı, sonra uygulanır
                reason = _failure_reason(e)
            else:
                if result.get("stages"):
                    codes.append(sc.scene_code)
                    queued.append(str(sc.id))
                    continue
                reason = _not_dispatched(result)
            killswitch.event(project_id, "color_auto_failed", scene_id=sid, code=sc.scene_code, error=reason)
    _round_begin(project_id, queued)
    killswitch.event(project_id, "color_auto_matched", codes=codes, skipped=report.get("skipped", []),
                     references=report.get("references", {}), round=bool(queued))
    if not queued and assemble:
        auto_assemble(project_id, "final", "klipler değişti")
    return codes


def maybe_assemble_draft(project_id: str, scene_id: str) -> None:
    """Taslak geçişin son çekimi bitince kaba kurgu (animatik) kendiliğinden birleştirilir. Ücretsizdir. Yalnızca
    bütün çekimler bitmişse: süren, başarısız ya da onay bekleyen çekim varken eksik kurgu üretilmez."""
    from sqlalchemy import func, select
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        if scene is None or scene.cut_tier != "draft" or scene.status != "done":
            return
        busy = s.scalar(select(func.count()).select_from(Scene).where(Scene.project_id == project_id,
                                                                      Scene.status.in_(IN_FLIGHT)))
    if not busy:                                     # son süren iş: bitmeyen (başarısız, onay bekleyen) varsa olay yazılır
        auto_assemble(project_id, "draft", "kaba kurgu")


@celery.task(name="hg.assemble_timeline")
def assemble_timeline(project_id: str, kind: str = "final", rematch: bool = True):
    """rematch=False: otomatik birleşme (renk eşleme turu sonrası); süreklilik ölçülür ama yeni eşleme turu açılmaz."""
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
            continuity.delay(project_id, None, rematch)
    return result


@celery.task(name="hg.voice_temp")
def voice_temp(project_id: str, sequence_id: str | None = None):
    """Seslendirilmemiş sentez repliklerinin geçici sesi (Ses sayfası; ücretsiz modda espeak)."""
    from app.post.voice import ensure_temp
    done = ensure_temp(project_id, sequence_id)
    killswitch.event(project_id, "voice_temp", shots=done)
    return done


@celery.task(name="hg.assemble_animatic")
def assemble_animatic(project_id: str):
    """Animatik: onaylı kareler (yoksa çekim kartı) geçici ya da final sesle, sesin süresine göre (yerel FFmpeg)."""
    from app.post.animatic import assemble
    try:
        result = assemble(project_id)
    except Exception as e:
        killswitch.event(project_id, "animatic_failed", error=f"{type(e).__name__}: {e}")
        raise
    if "error" in result:
        killswitch.event(project_id, "animatic_failed", error=result["error"])
    else:
        killswitch.event(project_id, "animatic", **{k: v for k, v in result.items() if k != "key"})
    return result


@celery.task(name="hg.qc")
def qc_task(project_id: str):
    """QC raporu: final film üzerinde siyah, donma, loudness, fotosensitif, çizgi titremesi (yerel FFmpeg)."""
    import datetime as dt
    import json
    import os
    import tempfile
    from app import storage
    from app.config import settings
    from app.db import session_scope
    from app.models import Project
    from app.post import delivery
    with session_scope() as s:
        p = s.get(Project, project_id)
        key, spec = p.timeline_key, delivery.spec_of(p)
    if not key:
        return {"error": "film yok"}
    cut = json.loads(storage.read_bytes(key[:-4] + ".json"))
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        report = delivery.run_qc(storage.download(key, os.path.join(tmp, "film.mp4")), spec, cut)
    report.update(film_key=key, checked_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    with session_scope() as s:
        s.get(Project, project_id).qc = report
    killswitch.event(project_id, "qc", passed=report["passed"],
                     failed=[c["name"] for c in report["checks"] if not c["passed"]])
    return {"passed": report["passed"]}


@celery.task(name="hg.delivery_package")
def delivery_package(project_id: str):
    """Teslim paketi (zip): master, izleme kopyası, stem'ler, M&E, SRT, EDL/OTIO, QC raporu, hak zinciri."""
    from app.post import delivery
    try:
        result = delivery.build(project_id)
    except Exception as e:
        killswitch.event(project_id, "delivery_failed", error=f"{type(e).__name__}: {e}")
        raise
    if "error" in result:
        killswitch.event(project_id, "delivery_failed", error=result["error"])
    else:
        killswitch.event(project_id, "delivery_package", files=len(result["files"]), qc_passed=result["qc_passed"])
    return result


# ---------- sahne istemi yazıcı ----------

@celery.task(name="hg.write_prompt")
def write_prompt(project_id: str, scene_id: str, force: bool = False):
    """Parçanın istemini LLM ile yazdırır (önbellekte varsa çağrı yok; force: yeniden yazdır). Sonuç olaya yazılır;
    yazılamazsa plan kurallar istemiyle kalır."""
    from app.engine import prompt_writer, scene_parts
    from app.models import Scene as SceneRow
    with session_scope() as s:
        plan = scene_parts.plan_part(s, s.get(SceneRow, scene_id))
        inp, codes = plan.writer_input, [x.scene_code for x in plan.shots]
    ih = prompt_writer.input_hash(inp)
    try:
        w = prompt_writer.write(project_id, inp, force=force)
    except Exception as e:                              # noqa: BLE001 — kurallar istemiyle kalır, iz olay olarak
        killswitch.event(project_id, "prompt_write_failed", part=codes, error=prompt_writer.full_error(e))
        return {"ok": False}
    finally:
        killswitch.r().delete(prompt_writer.WRITING.format(ih))
    killswitch.event(project_id, "prompt_written", part=codes, ok=w.prompt is not None, chars=len(w.prompt or ""),
                     usd=round(w.usd, 4), errors=w.errors[:4])
    return {"ok": w.prompt is not None, "input_hash": ih}


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
