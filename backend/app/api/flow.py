"""Google Flow köprüsü (app/engine/flow_pack.py): çekim başına elle üretim paketi. Hiçbir ücretli servis çağrılmaz;
klipler Flow'da elle üretilip dış çekim olarak yüklenir (POST /scenes/{id}/external)."""
import os
import re
import shutil
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import ProjectId, project_or_404
from app.config import settings
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
