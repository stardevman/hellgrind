"""Teslim uç noktaları: teslim şartnamesi, QC raporu (PDF), hak zinciri belgeleri, yapay zekâ kullanım beyanı ve teslim
paketi. Hepsi yerel ve ücretsizdir; hiçbir ücretli servis çağrılmaz."""
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import ProjectId, project_or_404
from app.api.uploads import receive
from app.budget import killswitch
from app.config import settings
from app.curation import curated
from app.db import get_session, session_scope
from app.engine.references import sha_of
from app.models import RightsItem
from app.post import delivery
from app.schemas import DeliverySpecIn

router = APIRouter(tags=["delivery"])
RIGHTS_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".txt", ".doc", ".docx", ".odt", ".md")


def state(s: Session, project_id: str) -> dict:
    p = project_or_404(s, project_id)
    spec = delivery.spec_of(p)
    qc = p.qc
    rights = delivery.rights_state(s, project_id)
    return {"spec": spec or delivery.DEFAULT_SPEC, "custom": spec is not None,
            "presets": delivery.PRESETS, "options": {"resolution": list(delivery.RESOLUTIONS), "fps": list(delivery.FPS),
                                                     "codec": delivery.CODECS},
            "film": {"key": p.timeline_key, "url": f"/api/media?key={p.timeline_key}" if p.timeline_key else None},
            "qc": {**qc, "stale": qc.get("film_key") != p.timeline_key} if qc else None,
            "rights": {"items": rights["items"], "missing": rights["missing"], "ai_rows": len(rights["ai_rows"])},
            "package": {"files": delivery.plan(s, project_id),
                        "url": f"/api/media?key={p.delivery_key}" if p.delivery_key else None}}


@router.get("/projects/{project_id}/delivery")
def get_delivery(project_id: ProjectId, s: Session = Depends(get_session)):
    return state(s, project_id)


@router.put("/projects/{project_id}/delivery")
@curated("delivery_spec")
def put_delivery(project_id: ProjectId, body: DeliverySpecIn, s: Session = Depends(get_session)):
    """Teslim şartnamesi. Final film bir sonraki birleştirmede bu biçimle çıkar (görüntü motoru çağrılmaz)."""
    p = project_or_404(s, project_id)
    p.delivery = body.model_dump()
    s.commit()
    killswitch.event(project_id, "delivery_spec", preset=body.preset, resolution=body.resolution, fps=body.fps)
    return state(s, project_id)


@router.post("/projects/{project_id}/qc")
@curated("qc_run")
def run_qc(project_id: ProjectId, s: Session = Depends(get_session)):
    """QC raporunu kuyruğa verir (final film üzerinde; ücretsiz)."""
    p = project_or_404(s, project_id)
    if not p.timeline_key:
        raise HTTPException(409, "Önce film birleştirilmeli.")
    from app.worker.tasks import qc_task
    qc_task.delay(project_id)
    return {"queued": True}


@router.get("/projects/{project_id}/qc.pdf")
def qc_report_pdf(project_id: ProjectId, s: Session = Depends(get_session)):
    p = project_or_404(s, project_id)
    if not p.qc:
        raise HTTPException(404, "Henüz QC raporu yok.")
    return Response(delivery.qc_pdf(p.qc, p.title), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="qc_raporu.pdf"'})


@router.post("/projects/{project_id}/rights/{kind}", status_code=201)
@curated("rights_upload")
async def upload_right(project_id: ProjectId, kind: str, request: Request, filename: str = Query(..., max_length=200),
                       note: str | None = Query(None, max_length=500)):
    """Hak zinciri belgesi (ham gövde): senaryo hak devri, ses oyuncusu sözleşmesi/rızası, müzik lisansı, model koşulları."""
    if kind not in delivery.RIGHTS:
        raise HTTPException(404, "Böyle bir hak zinciri maddesi yok")
    name = os.path.basename(filename).strip()
    if not name.lower().endswith(RIGHTS_EXT):
        raise HTTPException(422, f"Belge türü desteklenmiyor ({', '.join(RIGHTS_EXT)}).")

    def ready():
        with session_scope() as s:
            project_or_404(s, project_id)
    await run_in_threadpool(ready)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "doc")
        await receive(request, path)

        def store():
            key = f"rights/{project_id}/{sha_of(path)}{os.path.splitext(name)[1].lower()}"
            if not storage.exists(key):
                storage.put_file(path, key, "application/octet-stream")
            with session_scope() as s:
                row = RightsItem(project_id=project_id, kind=kind, filename=name, file_key=key, note=note)
                s.add(row)
                s.flush()
                return {"id": str(row.id), "kind": kind, "filename": name}
        return await run_in_threadpool(store)


@router.delete("/rights/{item_id}")
@curated("rights_remove")
def remove_right(item_id: str, s: Session = Depends(get_session)):
    row = s.get(RightsItem, item_id)
    if row is None:
        raise HTTPException(404, "Belge yok")
    out = {"id": item_id, "project_id": str(row.project_id), "kind": row.kind}
    s.delete(row)
    s.commit()
    return out


@router.get("/projects/{project_id}/rights/ai-declaration")
def ai_declaration(project_id: ProjectId, format: str = Query("json", pattern="^(json|csv)$"), s: Session = Depends(get_session)):
    """Yapay zekâ kullanım beyanı: servis geçmişinden (çekim, motor, işlem, tarih, mod)."""
    project_or_404(s, project_id)
    rows = delivery.ai_declaration(s, project_id)
    if format == "csv":
        return Response(delivery.ai_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="yapay_zeka_beyani.csv"'})
    return {"rows": rows}


@router.post("/projects/{project_id}/delivery/package")
@curated("delivery_package")
def make_package(project_id: ProjectId, s: Session = Depends(get_session)):
    """Teslim paketini kuyruğa verir. Eksik hak zinciri maddesi engellemez; yanıtta listelenir."""
    p = project_or_404(s, project_id)
    if not p.timeline_key:
        raise HTTPException(409, "Önce film birleştirilmeli.")
    from app.worker.tasks import delivery_package
    delivery_package.delay(project_id)
    return {"queued": True, "missing": delivery.rights_state(s, project_id)["missing"]}
