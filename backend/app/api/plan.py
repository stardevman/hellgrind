"""Bir sahne için Stateless Re-render planını çıkarır ve gereken aşamaları kuyruğa verir."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ensure_not_frozen
from app.budget import killswitch
from app.config import project_settings
from app.core.rerender import ALL, Stage, plan_rerender, stage_names
from app.core.state import load_assets, load_characters
from app.hermes import lint
from app.models import AudioTrack, Project, Scene
from app.style import bible
from app.worker.dispatch import build_chain, claim_dispatch, release_dispatch

# Bu durumlardaki sahne bir worker tarafından işleniyor olabilir.
ACTIVE_STATUSES = ("probing", "generating", "correcting", "audio", "lipsync", "muxing")


class Busy(HTTPException):
    def __init__(self, detail: str):
        super().__init__(409, detail)


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
    result = lint.check_scene(s, sc.id, project_settings(s.get(Project, sc.project_id).settings))
    if result.ok:
        return
    missing = [m.name for m in result.missing]
    if result.blocks:
        sc.status, sc.last_error = "blocked", result.message()
        s.commit()
        killswitch.event(pid, "blocked", scene_id=str(sc.id), missing=missing)
        raise Blocked(sc.scene_code, result)
    killswitch.event(pid, "lint_warning", scene_id=str(sc.id), missing=missing)


def ensure_idle(sc: Scene) -> None:
    """Sahne şu an işleniyorsa 409. Durum tek başına yetmez: çöken bir worker sahneyi sonsuza dek "işleniyor"
    bırakabilir; bu yüzden projede gerçekten çalışan bir görev de aranır."""
    if sc.status in ACTIVE_STATUSES and killswitch.has_running_tasks(str(sc.project_id)):
        raise Busy(f"{sc.scene_code} şu an işleniyor ({sc.status}); bitince yeniden dene.")


def plan_and_dispatch(s: Session, sc: Scene, *, force_visual: bool = False) -> list[str]:
    """Planlanan aşamaların adlarını döndürür ve zinciri kuyruğa verir; yapılacak iş yoksa [] döner.

    force_visual: görüntüyü baştan üretir. prompt_revision artar, böylece seed'ler (ve klipler) yenilenir; görsel özet
    revizyonu içermediğinden sonraki plan yine "değişiklik yok" der."""
    pid = str(sc.project_id)
    ensure_not_frozen(pid)
    tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)))
    stages, _ = plan_rerender(sc, load_characters(s, sc.id), tracks, load_assets(s, sc.id),
                              bible.active_version(s, sc.project_id))
    if force_visual:
        stages = ALL
    if stages == Stage.NONE:
        return []
    ensure_idle(sc)
    if Stage.VISUAL in stages:
        lint_gate(s, sc)                     # para harcanmadan önce: eksik referansla görüntü üretilmez
    locked = Stage.VISUAL in stages
    if locked and not claim_dispatch(sc.id):
        raise Busy(f"{sc.scene_code} için üretim zaten sıraya alındı; birkaç saniye sonra yeniden dene.")
    try:
        if force_visual:
            sc.prompt_revision += 1
        sc.status, sc.last_error = "pending", None
        s.commit()
        build_chain(pid, str(sc.id), stages).apply_async()
    except Exception:
        if locked:
            release_dispatch(sc.id)
        raise
    return stage_names(stages)


def dispatch_result(s: Session, sc: Scene, **kwargs) -> dict:
    """Uç noktaların ortak yanıtı: {"stages": [...]} ya da engellendiyse {"stages": [], "blocked": {...}}."""
    try:
        return {"stages": plan_and_dispatch(s, sc, **kwargs)}
    except Blocked as e:
        return {"stages": [], "blocked": e.payload()}


def dispatch_all(s: Session, project_id: str) -> dict:
    """Projedeki her sahne için planı çıkarıp kuyruğa verir. İşlenmekte olan sahne atlanır (hata sayılmaz); eksik
    referanslı sahne `blocked` olur ve `blocked` altında eksikleriyle bildirilir; üretim sırasında proje donarsa (bütçe)
    kalan sahneler atlanır ve `frozen` bildirilir: yapılan iş geri alınmış gibi görünmesin."""
    planned, busy, blocked, frozen = {}, [], {}, False
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all():
        if frozen or killswitch.is_frozen(project_id):
            frozen, planned[sc.scene_code] = True, []
            continue
        try:
            planned[sc.scene_code] = plan_and_dispatch(s, sc)
        except Busy:
            planned[sc.scene_code] = []
            busy.append(sc.scene_code)
        except Blocked as e:
            planned[sc.scene_code] = []
            blocked[sc.scene_code] = [m["name"] for m in e.payload()["missing"]]
    return {"planned": planned, "busy": busy, "blocked": blocked, "frozen": frozen}
