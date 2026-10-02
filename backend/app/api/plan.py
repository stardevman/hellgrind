"""Bir sahne için Stateless Re-render planını çıkarır ve gereken aşamaları kuyruğa verir."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ensure_not_frozen
from app.budget import killswitch
from app.core.rerender import ALL, Stage, plan_rerender, stage_names
from app.core.state import load_characters
from app.models import AudioTrack, Scene
from app.worker.dispatch import build_chain, claim_dispatch, release_dispatch

# Bu durumlardaki sahne bir worker tarafından işleniyor olabilir.
ACTIVE_STATUSES = ("probing", "generating", "correcting", "audio", "lipsync", "muxing")


class Busy(HTTPException):
    def __init__(self, detail: str):
        super().__init__(409, detail)


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
    stages, _ = plan_rerender(sc, load_characters(s, sc.id), tracks)
    if force_visual:
        stages = ALL
    if stages == Stage.NONE:
        return []
    ensure_idle(sc)
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
