"""Bir sahne için Stateless Re-render planını çıkarır ve gereken aşamaları kuyruğa verir."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ensure_not_frozen
from app.budget import killswitch
from app.config import project_settings
from app.core.rerender import PRODUCTION, Stage, board_stale, plan_rerender, stage_names
from app.engine.storyboard import board_ready
from app.core.state import load_assets, load_characters
from app.hermes import lint
from app.models import AudioTrack, Project, Scene, Sequence
from app.style import bible
from app.worker.dispatch import Batch, batch_holding, build_chain, claim_dispatch, release_dispatch

# Bu durumlardaki sahne bir worker tarafından işleniyor olabilir.
ACTIVE_STATUSES = ("storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing")


class Busy(HTTPException):
    def __init__(self, detail: str):
        super().__init__(409, detail)


class SequenceCapped(HTTPException):
    """Sahnenin sekansı bütçe tavanında: ücretli aşamalar (görüntü, dudak senkronu) başlamaz. Montaj ücretsizdir, serbesttir."""
    def __init__(self, sc: Scene, seq: Sequence):
        cap = f"{float(seq.budget_cap_usd):.2f}" if seq.budget_cap_usd is not None else "-"
        super().__init__(423, f"{sc.scene_code}: {seq.code} sekansı bütçe tavanına ulaştı ({cap} USD); "
                              "devam etmek için sekans tavanını yükselt.")


class AwaitingBoard(Exception):
    """Onay zorunlu ve storyboard karesi onaylı değil: görüntü üretimi başlamaz (gerekirse yalnızca kare çizildi)."""

    def __init__(self, scene_code: str, stages: list[str]):
        super().__init__(f"{scene_code}: storyboard karesi onay bekliyor")
        self.scene_code, self.stages = scene_code, stages


class AwaitingPlan(Exception):
    """Referans modu: kuru çalıştırma planı onaylanmadı; görüntü üretimi başlamaz (sahne plan_review)."""

    def __init__(self, scene_code: str, plan: dict):
        super().__init__(f"{scene_code}: plan onayı bekliyor")
        self.scene_code, self.plan = scene_code, plan


class Blocked(Exception):
    """Üretim öncesi kontrol: çekimde onaylı referansı olmayan varlık var; sahne `blocked` oldu, hiçbir şey harcanmadı."""

    def __init__(self, scene_code: str, result: lint.LintResult):
        super().__init__(result.message())
        self.scene_code, self.result = scene_code, result

    def payload(self) -> dict:
        return {"message": self.result.message(),
                "missing": [{"asset_id": m.asset_id, "type": m.type, "name": m.name, "status": m.status}
                            for m in self.result.missing]}


def lint_gate(s: Session, sc: Scene) -> None:
    """Görüntü üretimi planlandıysa referans eksiksizliğini denetler. block modunda sahneyi `blocked` yapar ve Blocked
    fırlatır; warn modunda yalnızca olay yazar."""
    pid = str(sc.project_id)
    from app.engine.scene_parts import lint_cfg                # referans modunda durağan görsel onayı yeter
    result = lint.check_scene(s, sc.id, lint_cfg(project_settings(s.get(Project, sc.project_id).settings), sc.engine))
    if result.ok:
        return
    missing = [m.name for m in result.missing]
    if result.blocks:
        sc.status, sc.last_error = "blocked", result.message()
        s.commit()
        killswitch.event(pid, "blocked", scene_id=str(sc.id), missing=missing)
        raise Blocked(sc.scene_code, result)
    killswitch.event(pid, "lint_warning", scene_id=str(sc.id), missing=missing)


class MissingReferences(Blocked):
    """Referans modu: çekimde görseli olmayan karakter, nesne ya da mekân var; render engellenir."""

    def __init__(self, scene_code: str, missing: list[dict], message: str):
        Exception.__init__(self, message)
        self.scene_code, self.missing, self.text = scene_code, missing, message

    def payload(self) -> dict:
        return {"message": self.text, "missing": [{"asset_id": m.get("asset_id"), "type": m["kind"], "name": m["name"],
                                                   "status": "no_image"} for m in self.missing]}


def reference_gate(s: Session, sc: Scene, cfg: dict) -> None:
    """Referans modu, görüntü üretimi planlandı: eksik referans -> blocked; plan onayı gerekli ve yoksa -> plan_review."""
    from app.engine import scene_parts
    from app.engine.part_render import clear_failure
    plan = scene_parts.plan_part(s, sc)
    pid = str(sc.project_id)
    clear_failure(plan.plan_hash)                     # yönetmenin yeniden üretim isteği: önceki 4xx işareti kalkar
    if plan.missing:
        message = ("Eksik referans: " + ", ".join(m["name"] for m in plan.missing) +
                   ". Varlıklar sayfasından referans görselini yükleyin; görseli olmayan varlıkla video üretilmez.")
        for x in plan.shots:
            x.status, x.last_error = "blocked", message
        s.commit()
        killswitch.event(pid, "blocked", scene_id=str(sc.id), missing=[m["name"] for m in plan.missing])
        raise MissingReferences(sc.scene_code, plan.missing, message)
    if scene_parts.plan_required(cfg) and not plan.approved():
        for x in plan.shots:
            if x.status not in ACTIVE_STATUSES:
                x.status, x.last_error = "plan_review", "Plan onayı bekliyor: kuru çalıştırma planını onaylayın."
        s.commit()
        from app.engine import prompt_writer
        if prompt_writer.needed(plan):                # yönetmen onaylamadan önce LLM istemi yazılsın (arka planda)
            prompt_writer.queue(pid, str(sc.id), plan)
        raise AwaitingPlan(sc.scene_code, {"part": [x.scene_code for x in plan.shots], "plan_hash": plan.plan_hash,
                                           "usd": plan.cost["reserve_usd"]})


def ensure_idle(sc: Scene) -> None:
    """Sahne şu an işleniyorsa 409. Durum tek başına yetmez: çöken bir worker sahneyi sonsuza dek "işleniyor"
    bırakabilir; bu yüzden projede gerçekten çalışan bir görev de aranır."""
    if sc.status in ACTIVE_STATUSES and killswitch.has_running_tasks(str(sc.project_id)):
        raise Busy(f"{sc.scene_code} şu an işleniyor ({sc.status}); bitince yeniden dene.")


def board_upgrade(s: Session, sc: Scene) -> bool:
    """Ücretli modda karakterli çekimin karesi metinden (Qwen-Image) ya da ücretsiz moddan kalma yerel çizilmişse ve
    karakter referansından çizilebiliyorsa: kare STORYBOARD aşamasında yeniden çizilir."""
    from app.config import settings
    if sc.keyframe_key or (sc.engine or "h3") not in ("h3", "seedance", "h3max", "seedance25") or not sc.board_key:
        return False
    from app.engine.scene_parts import reference_mode
    if reference_mode():
        return False                                  # referans modu: hiçbir kare çizilmez
    if sc.engine in ("h3max", "seedance25") and sc.h3_mode != "fl2va":
        return False                                  # fal otomatik mod: kare yalnızca onaylanınca videoya gider
    from app.core.chain import parent_of, stale
    from app.core.h3mode import effective_mode, keyframe_model, strict_refs
    from app.engine.storyboard import board_source, chain_enabled, outdated
    chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
    if (sc.engine or "h3") == "h3" and effective_mode(sc, chars, assets) == "ref2va":
        return False
    parent = parent_of(s, sc) if chain_enabled() else None
    if parent is not None and stale(sc, parent):      # zincirdeki önceki kare değişti: bu kare eski
        return True
    if settings.dry_run:
        return False
    strict = strict_refs(project_settings(s.get(Project, sc.project_id).settings))
    return outdated(sc.board_source, board_source("fl2va", False, keyframe_model(sc, chars, assets, strict)))


def model_group(sc: Scene, s: Session) -> tuple[str, str]:
    """Toplu dağıtımda çekimin model grupları: (H3 DiT'i: ref2va ya da fl2va — T2V ve diğer motorlar da fl2va grubunda,
    ilk kare modeli: qwen_edit ya da qwen)."""
    from app.core.h3mode import effective_mode, keyframe_model, strict_refs
    chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
    board = keyframe_model(sc, chars, assets, strict_refs(project_settings(s.get(Project, sc.project_id).settings)))
    if (sc.engine or "h3") != "h3":
        return "fl2va", board
    return effective_mode(sc, chars, assets), board


def plan_and_dispatch(s: Session, sc: Scene, *, force_visual: bool = False, rerun_visual: bool = False,
                      mode: str = "full", allowed: Stage | None = None, batch: Batch | None = None,
                      post_only: bool = False, restyle: bool = False) -> list[str]:
    """Planlanan aşamaların adlarını döndürür ve zinciri kuyruğa verir; yapılacak iş yoksa [] döner.

    force_visual: görüntüyü baştan üretir. prompt_revision artar, böylece seed'ler (ve klipler) yenilenir; görsel özet
    revizyonu içermediğinden sonraki plan yine "değişiklik yok" der.
    rerun_visual: görsel aşamayı aynı seed'lerle yeniden çalıştırır (iyileştirme geçişi: taslağın test klipleri yeniden
    kullanılır, üstüne tam tur gelir). mode="draft": görsel aşama yalnızca test turunu çalıştırır.
    allowed: plan bu aşamaların dışına çıkıyorsa hiçbir şey kuyruğa verilmez (ör. LUT değişince yalnızca renk ve montaj).
    post_only: mevcut klip korunur; görüntü hiçbir koşulda üretilmez (seçim, ses, renk, revizyon, otomatik renk eşleme).
    Klibi olmayan (görsel özeti ve rafine #1'i yok) çekimde yapılacak iş yoktur.
    restyle: "eski stil" işaretli çekim yeniden üretilir. Yönetmenin açık isteğidir (sahneyi üret, stil etkinleştir +
    render); toplu ve otomatik dağıtımlar eski stilli çekimi yeniden üretmez, yalnızca işaretli bırakır.
    batch: çok sahneli dağıtım; zincir hemen kuyruğa verilmez, `batch.launch()` önce kareleri sonra videoları verir.
    Onay zorunluyken kare onaylı değilse görüntü üretilmez: gerekirse yalnızca kare çizilir ve AwaitingBoard fırlatılır."""
    pid = str(sc.project_id)
    ensure_not_frozen(pid)
    from app.engine.iteration import mark_seam_outdated
    mark_seam_outdated(s, sc)                # eski dikiş kuralıyla üretilmiş klip: görüntü yeniden planlanır
    tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)))
    project = s.get(Project, sc.project_id)
    if post_only:
        if force_visual or rerun_visual:
            raise ValueError("post_only görüntü üretimiyle birlikte istenemez")
        if sc.visual_hash is None and not has_winner(s, sc):     # klibi yok: yalnızca ses/renk/montaj yapılamaz
            return []
    chars, assets, style = load_characters(s, sc.id), load_assets(s, sc.id), bible.active_version(s, sc.project_id)
    stages, fp = plan_rerender(sc, chars, tracks, assets, style, project.lut_hash, keep_visual=post_only)
    if Stage.VISUAL in stages and not (force_visual or rerun_visual or restyle) and bible.style_held(sc, fp.visual) \
            and has_winner(s, sc):
        # eski stil: yalnızca stil değişti; çekim işaretli kalır, görüntü yönetmen isteyince üretilir
        stages, fp = plan_rerender(sc, chars, tracks, assets, style, project.lut_hash, keep_visual=True)
    if force_visual or rerun_visual:
        stages |= PRODUCTION | (Stage.STORYBOARD if board_stale(sc, fp) else Stage.NONE) | \
            (Stage.GRADE if fp.grade is not None else Stage.NONE)
    from app.engine import scene_parts
    reference = (sc.method or "generate") == "generate" and scene_parts.active(sc.engine)
    if reference:
        stages &= ~Stage.STORYBOARD         # referans modu: storyboard karesi çizilmez
    elif Stage.VISUAL in stages and Stage.STORYBOARD not in stages and board_upgrade(s, sc):
        stages |= Stage.STORYBOARD          # kare videodan önce, kare grubunda yeniden çizilsin (video kuyruğunun ortasında değil)
    if stages == Stage.NONE or (allowed is not None and stages & ~allowed):
        return []
    ensure_idle(sc)
    seq = s.get(Sequence, sc.sequence_id) if sc.sequence_id else None
    if seq is not None and seq.status == "capped" and stages & (Stage.STORYBOARD | Stage.VISUAL | Stage.LIPSYNC):
        raise SequenceCapped(sc, seq)
    awaiting = False
    if Stage.VISUAL in stages:
        lint_gate(s, sc)                     # para harcanmadan önce: eksik referansla görüntü üretilmez
        cfg = project_settings(project.settings)
        if reference:
            reference_gate(s, sc, cfg)       # eksik referans ve plan onayı (kare onayının yerine)
        elif cfg["require_board_approval"] and not board_ready(sc, fp.visual):
            stages, awaiting = (Stage.STORYBOARD if board_stale(sc, fp) else Stage.NONE), True
            if stages == Stage.NONE:         # kare güncel ama onaysız: yalnızca onaya bırakılır
                sc.status = "board_review"
                s.commit()
                raise AwaitingBoard(sc.scene_code, [])
    locked = Stage.VISUAL in stages
    if locked and not claim_dispatch(sc.id):
        if batch_holding(sc.id):
            raise Busy(f"{sc.scene_code} toplu üretimde: önce bütün storyboard kareleri çiziliyor, video kareler bitince "
                       "kendiliğinden başlayacak.")
        raise Busy(f"{sc.scene_code} için üretim zaten sıraya alındı; birkaç saniye sonra yeniden dene.")
    try:
        if force_visual and locked:
            sc.prompt_revision += 1
        sc.status, sc.last_error = "pending", None
        s.commit()
        if batch is not None:
            model, board = model_group(sc, s)
            batch.add(sc.id, stages, mode, model=model, board=board)
        else:
            build_chain(pid, str(sc.id), stages, mode=mode).apply_async()
    except Exception:
        if locked:
            release_dispatch(sc.id)
        raise
    if awaiting:
        raise AwaitingBoard(sc.scene_code, stage_names(stages))
    return stage_names(stages)


def has_winner(s: Session, sc: Scene) -> bool:
    from app.models import Variation
    return s.scalar(select(Variation.id).where(Variation.scene_id == sc.id, Variation.refined_rank == 1)
                    .limit(1)) is not None


def dispatch_result(s: Session, sc: Scene, **kwargs) -> dict:
    """Uç noktaların ortak yanıtı: {"stages": [...]} ya da engellendiyse {"stages": [], "blocked": {...}}."""
    try:
        return {"stages": plan_and_dispatch(s, sc, **kwargs)}
    except Blocked as e:
        return {"stages": [], "blocked": e.payload()}
    except AwaitingBoard as e:
        return {"stages": e.stages, "awaiting_board": True}
    except AwaitingPlan as e:
        return {"stages": [], "awaiting_plan": True, "plan": e.plan}


def dispatch_all(s: Session, project_id: str, mode: str = "full", allowed: Stage | None = None,
                 post_only: bool = False, restyle: bool = False) -> dict:
    """Projedeki her sahne için planı çıkarıp kuyruğa verir. İşlenmekte olan sahne atlanır (hata sayılmaz); eksik
    referanslı sahne `blocked` olur ve `blocked` altında eksikleriyle bildirilir; üretim sırasında proje donarsa (bütçe)
    kalan sahneler atlanır ve `frozen` bildirilir: yapılan iş geri alınmış gibi görünmesin. Bütçe tavanındaki sekansın
    sahneleri `capped` altında bildirilir; diğer sekanslar üretmeye devam eder. Onay zorunluyken onaysız karesi olan
    sahneler `awaiting_board` altındadır (yalnızca kareleri çizilir).

    Kuyruk sırası (toplu dağıtım): önce bütün storyboard kareleri, videolar son kare bitince (GPU'da model değişimi her
    çekimde değil, bir kez olur)."""
    planned, busy, blocked, capped, awaiting = {}, [], {}, [], []
    plans: list = []
    batch = Batch(project_id)
    try:
        frozen = _plan_all(s, project_id, mode, allowed, batch, planned, busy, blocked, capped, awaiting, plans,
                           post_only=post_only, restyle=restyle)
        batched = batch.launch()
        frozen = frozen or killswitch.is_frozen(project_id)    # kuyruğa verilen ilk işler bütçeyi bitirmiş olabilir
    except Exception:
        for sid, stages, _ in batch.items:   # kuyruğa verilemeyen sahneler kilitte kalmasın
            if Stage.VISUAL in stages:
                release_dispatch(sid)
        raise
    return {"planned": planned, "busy": busy, "blocked": blocked, "capped": capped, "awaiting_board": awaiting,
            "awaiting_plan": plans, "frozen": frozen, "batched": batched is not None}


def _plan_all(s: Session, project_id: str, mode: str, allowed: Stage | None, batch: Batch, planned: dict, busy: list,
              blocked: dict, capped: list, awaiting: list, plans: list | None = None, post_only: bool = False,
              restyle: bool = False) -> bool:
    frozen = False
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all():
        if frozen or killswitch.is_frozen(project_id):
            frozen, planned[sc.scene_code] = True, []
            continue
        try:
            planned[sc.scene_code] = plan_and_dispatch(s, sc, mode=mode, allowed=allowed, batch=batch,
                                                       post_only=post_only, restyle=restyle)
        except AwaitingBoard as e:
            planned[sc.scene_code] = e.stages
            awaiting.append(sc.scene_code)
        except AwaitingPlan:
            planned[sc.scene_code] = []
            if plans is not None:
                plans.append(sc.scene_code)
        except Busy:
            planned[sc.scene_code] = []
            busy.append(sc.scene_code)
        except Blocked as e:
            planned[sc.scene_code] = []
            blocked[sc.scene_code] = [m["name"] for m in e.payload()["missing"]]
        except SequenceCapped:
            planned[sc.scene_code] = []
            capped.append(sc.scene_code)
    return frozen
