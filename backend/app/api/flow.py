"""Google Flow: çekim başına elle üretim paketi (app/engine/flow_pack.py; ücretli servis çağrılmaz, klipler elle üretilip
dış çekim olarak yüklenir) ve köprü üzerinden doğrudan üretim (app/engine/flow_run.py; ücretli modda Mac'teki
tools/flow_bridge gflow-cli ile Flow'u sürer, ücretsiz modda yerel taslak)."""
import os
import re
import shutil
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import ProjectId, SceneId, ensure_not_frozen, project_or_404, scene_or_404
from app.api.plan import ACTIVE_STATUSES
from app.config import settings
from app.curation import curated
from app.db import get_session
from app.engine import flow_pack
from app.models import Scene

router = APIRouter(tags=["flow"])
QUALITY = "^(lite|fast|quality)$"
SCOPE = "^(open|all)$"


def _shots(s: Session, project_id: str, quality: str, scope: str) -> list[flow_pack.FlowShot]:
    q = select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)
    scenes = [sc for sc in s.scalars(q) if scope == "all" or sc.status != "done"]
    out = []
    for sc in scenes:
        try:
            out.append(flow_pack.shot(s, sc, quality))
        except Exception as e:                                   # noqa: BLE001 — ör. çeviri modeli yok
            from app.hermes.translate import TranslationUnavailable
            if isinstance(e, TranslationUnavailable):
                raise HTTPException(409, f"{sc.scene_code}: {e}")
            raise
    return out


@router.get("/projects/{project_id}/flow")
def flow_plan(project_id: ProjectId, quality: str = Query("fast", pattern=QUALITY), scope: str = Query("open", pattern=SCOPE),
              s: Session = Depends(get_session)):
    """Paketin içeriği ve kredi tahmini. scope=open: bitmemiş çekimler (varsayılan), all: hepsi."""
    project_or_404(s, project_id)
    shots = _shots(s, project_id, quality, scope)
    return {"quality": quality, "per_clip": flow_pack.FLOW_CREDITS[quality], "credits": sum(x.credits for x in shots),
            "shots": [x.as_dict() for x in shots]}


@router.get("/projects/{project_id}/flow.zip")
def flow_zip(project_id: ProjectId, quality: str = Query("fast", pattern=QUALITY), scope: str = Query("open", pattern=SCOPE),
             s: Session = Depends(get_session)):
    """Flow paketi (zip): BENIOKU.txt, plan.csv, çekim başına istem.txt, başlangıç/bitiş kareleri ve malzemeler."""
    p = project_or_404(s, project_id)
    title, shots = p.title, _shots(s, project_id, quality, scope)
    if not shots:
        raise HTTPException(409, "Pakete girecek çekim yok: bütün çekimler bitmiş (hepsi için scope=all).")
    s.close()
    os.makedirs(settings.work_dir, exist_ok=True)
    work = tempfile.mkdtemp(dir=settings.work_dir)
    name = "flow-" + (re.sub(r"[^\w-]+", "-", title, flags=re.UNICODE).strip("-").lower() or "paket") + ".zip"
    try:
        path = flow_pack.build_zip(title, shots, quality, os.path.join(work, "flow.zip"), work)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))


# ---------- köprü üzerinden doğrudan üretim ----------

class FlowRunIn(BaseModel):
    quality: str | None = Field(None, pattern=QUALITY)
    scope: str = Field("open", pattern=SCOPE)
    scene_ids: list[str] | None = None


def _queue(s: Session, project_id: str, scenes: list[Scene], quality: str) -> dict:
    from app.engine.flow_run import estimate_usd, quality_of
    from app.worker.dispatch import claim_dispatch
    from app.worker.tasks import flow_scene
    quality = quality_of(quality)
    queued, busy = [], []
    for sc in scenes:
        if sc.status in ACTIVE_STATUSES or not claim_dispatch(sc.id):
            busy.append(sc.scene_code)
            continue
        flow_scene.apply_async(args=[project_id, str(sc.id), quality])
        queued.append(sc.scene_code)
    per = flow_pack.FLOW_CREDITS[quality]
    return {"queued": queued, "busy": busy, "quality": quality, "credits": per * len(queued),
            "estimate_usd": round(estimate_usd(quality) * len(queued), 4), "dry_run": settings.dry_run}


@router.post("/projects/{project_id}/flow/run")
@curated("flow_generate")
def flow_run(project_id: ProjectId, body: FlowRunIn, s: Session = Depends(get_session)):
    """Seçilen (ya da kapsamdaki) çekimleri Google Flow'da üretir: çekim başına bir klip, köprü işleri sırayla yürütür.
    Ücretsiz modda Flow çağrılmaz, yerel taslak üretilir (tutar sanal). Her çekim bütçe rezervasyonundan geçer."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    q = select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)
    scenes = list(s.scalars(q))
    if body.scene_ids:
        wanted = set(body.scene_ids)
        scenes = [sc for sc in scenes if str(sc.id) in wanted]
    elif body.scope == "open":
        scenes = [sc for sc in scenes if sc.status != "done"]
    if not scenes:
        raise HTTPException(409, "Üretilecek çekim yok: bütün çekimler bitmiş (hepsi için scope=all).")
    return _queue(s, project_id, scenes, body.quality)


@router.post("/scenes/{scene_id}/flow/run")
@curated("flow_generate")
def flow_run_scene(scene_id: SceneId, body: FlowRunIn, s: Session = Depends(get_session)):
    """Tek çekimi Google Flow'da üretir (bkz. /projects/{id}/flow/run)."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    r = _queue(s, str(sc.project_id), [sc], body.quality)
    if r["busy"]:
        raise HTTPException(409, f"{sc.scene_code} şu an işleniyor; bitince yeniden dene.")
    return {**r, "project_id": str(sc.project_id), "scene_id": scene_id}
