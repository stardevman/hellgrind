"""Kürasyon kaydı, verimlilik metrikleri ve profesyonel dışa aktarma (Faz 17)."""
import json
import os
import shutil
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import storage
from app.api.deps import ProjectId, project_or_404
from app.config import settings
from app.curation import ACTIONS_TR
from app.db import get_session
from app.models import CurationEvent, Project, Scene
from app.post import export
from app.quality import metrics

router = APIRouter(tags=["export"])


def curation_lines(s: Session, project_id: str) -> str:
    """Her satır bir karar: zaman, eylem (Türkçe adıyla), sahne kodu, varyasyon ve payload."""
    codes = dict(s.execute(select(Scene.id, Scene.scene_code).where(Scene.project_id == project_id)).all())
    rows = s.scalars(select(CurationEvent).where(CurationEvent.project_id == project_id)
                     .order_by(CurationEvent.created_at, CurationEvent.id)).all()
    return "".join(json.dumps({
        "id": e.id, "at": e.created_at.isoformat() if e.created_at else None, "action": e.action,
        "action_tr": ACTIONS_TR.get(e.action, e.action), "scene_id": str(e.scene_id) if e.scene_id else None,
        "scene_code": codes.get(e.scene_id), "variation_id": str(e.variation_id) if e.variation_id else None,
        "payload": e.payload}, ensure_ascii=False) + "\n" for e in rows)


@router.get("/projects/{project_id}/curation.jsonl")
def curation_export(project_id: ProjectId, s: Session = Depends(get_session)):
    """Kürasyon kaydı (JSON Lines): projeye özel kalite modelinin eğitim verisi."""
    project_or_404(s, project_id)
    return Response(curation_lines(s, project_id), media_type="application/x-ndjson",
                    headers={"Content-Disposition": 'attachment; filename="kurasyon.jsonl"'})


@router.get("/projects/{project_id}/curation")
def curation_recent(project_id: ProjectId, limit: int = Query(30, ge=1, le=500), s: Session = Depends(get_session)):
    """Son kararlar (panel için)."""
    project_or_404(s, project_id)
    lines = curation_lines(s, project_id).splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines)]


@router.get("/projects/{project_id}/metrics")
def project_metrics(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    return metrics.compute(project_id)


def _cut(p: Project) -> dict:
    if not p.timeline_key:
        raise HTTPException(409, "Önce filmi birleştir: dışa aktarma birleştirilmiş filmin kesme listesinden yapılır.")
    try:
        return json.loads(storage.read_bytes(p.timeline_key.rsplit(".", 1)[0] + ".json"))
    except Exception:
        raise HTTPException(409, "Filmin kesme listesi okunamadı; filmi yeniden birleştir.")


@router.get("/projects/{project_id}/export")
def export_project(project_id: ProjectId, format: str = Query(..., pattern="^(edl|otio|package)$"),
                   s: Session = Depends(get_session)):
    """format=edl (CMX3600), otio (OpenTimelineIO JSON) ya da package (çekim klasörleri ve stem'lerle zip)."""
    p = project_or_404(s, project_id)
    cut, title = _cut(p), p.title
    jumps = {x["to"] for x in (p.continuity or {}).get("pairs", []) if x.get("jump")}
    if format == "edl":
        return Response(export.edl(cut, title), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="film.edl"'})
    if format == "otio":
        return Response(json.dumps(export.otio(cut, title, jumps=jumps), ensure_ascii=False, indent=2),
                        media_type="application/json", headers={"Content-Disposition": 'attachment; filename="film.otio"'})
    lines = curation_lines(s, project_id)
    s.close()
    os.makedirs(settings.work_dir, exist_ok=True)
    work = tempfile.mkdtemp(dir=settings.work_dir)
    try:
        path = export.package(project_id, cut, os.path.join(work, "paket.zip"), work, lines, jumps)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(path, media_type="application/zip", filename="paket.zip",
                        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True))
