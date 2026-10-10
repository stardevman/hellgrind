"""Renk aşaması ve süreklilik (Faz 16): çekim renk ayarı, önce/sonra önizleme, film geneli LUT, renk sıçramaları."""
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import ProjectId, SceneId, ensure_not_frozen, project_or_404, scene_or_404
from app.api.plan import dispatch_all, dispatch_result, ensure_idle
from app.api.uploads import receive
from app.budget import killswitch
from app.config import settings
from app.core.hashing import grade_settings, stable_hash
from app.core.rerender import Stage
from app.curation import curated
from app.db import get_session, session_scope
from app.engine.references import sha_of
from app.models import Project
from app.post import grade
from app.quality.anomaly import probe_duration
from app.schemas import GradeIn

router = APIRouter(tags=["grade"])
REGRADE = Stage.GRADE | Stage.MUX


@router.post("/scenes/{scene_id}/grade")
@curated("grade_change")
def set_grade(scene_id: SceneId, body: GradeIn, s: Session = Depends(get_session)):
    """Çekimin renk ayarı. Yalnızca renk değiştiği için görüntü ve dudak senkronu yeniden çalışmaz: GRADE + MUX."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    sc.grade = grade.clamp(body.model_dump())
    sc.grade_auto = None                     # yönetmenin ayarı: otomatik eşleştirme bu çekime dokunmaz
    s.commit()
    killswitch.event(str(sc.project_id), "grade_changed", scene_id=scene_id, code=sc.scene_code)
    return {"grade": grade_settings(sc.grade), **dispatch_result(s, sc)}


def _preview(scene_id: str, raw: dict | None, before: bool) -> bytes:
    with session_scope() as s:
        sc = scene_or_404(s, scene_id)
        source = sc.lipsync_video_key
        lut_key = s.get(Project, sc.project_id).lut_key
    if not source:
        raise HTTPException(409, "Bu çekimin henüz renklendirilecek videosu yok.")
    settings_ = None if before else grade.clamp(raw or {})
    key = f"thumbs/grade/{scene_id}/{stable_hash([source, settings_, None if before else lut_key])}.jpg"
    if not storage.exists(key):
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            video = storage.download(source, os.path.join(tmp, "v.mp4"))
            lut = storage.download(lut_key, os.path.join(tmp, "film.cube")) if lut_key and not before else None
            out = grade.jpeg_frame(video, probe_duration(video) / 2, os.path.join(tmp, "p.jpg"), settings_, lut)
            storage.put_file(out, key, "image/jpeg")
    return storage.read_bytes(key)


@router.get("/scenes/{scene_id}/grade/preview.jpg")
def grade_preview(scene_id: SceneId, before: bool = False, exposure: float = Query(0.0, ge=-3, le=3),
                  contrast: float = Query(1.0, ge=0.5, le=2), saturation: float = Query(1.0, ge=0, le=3),
                  gamma: float = Query(1.0, ge=0.3, le=3), temperature: int = Query(6500, ge=2000, le=12000)):
    """Önce/sonra karşılaştırması: çekimin orta karesi renklendirilmeden (before=true) ya da verilen ayarla ve film
    LUT'uyla. Kaydetmeden denemek içindir; sonuç içerik-adresli önbelleğe alınır."""
    data = _preview(scene_id, dict(exposure=exposure, contrast=contrast, saturation=saturation, gamma=gamma,
                                   temperature=temperature), before)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _regrade(project_id: str) -> dict:
    """LUT değişince bitmiş çekimlerin yalnızca rengi ve montajı yenilenir; görüntüsü üretilmemiş çekimler beklemez."""
    with session_scope() as s:
        result = dispatch_all(s, project_id, allowed=REGRADE)
    return {"regraded": [code for code, stages in result["planned"].items() if stages], "busy": result["busy"]}


@router.post("/projects/{project_id}/lut")
@curated("lut_change")
async def upload_lut(project_id: ProjectId, request: Request, filename: str = Query("film.cube", max_length=200)):
    """Film geneli 3B LUT (.cube, istek gövdesinde). Bütün bitmiş çekimlerde GRADE + MUX çalışır."""
    if not filename.lower().endswith(".cube"):
        raise HTTPException(422, "LUT için .cube dosyası yükle.")

    def ready():
        with session_scope() as s:
            project_or_404(s, project_id)
        ensure_not_frozen(project_id)
    await run_in_threadpool(ready)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "film.cube")
        await receive(request, path)

        def store():
            try:
                with open(path, encoding="utf-8", errors="strict") as f:
                    size = grade.validate_cube(f.read())
            except UnicodeDecodeError:
                raise HTTPException(422, "LUT metin dosyası değil.")
            except grade.InvalidLut as e:
                raise HTTPException(422, str(e))
            sha = sha_of(path)
            key = f"luts/{project_id}/{sha}.cube"
            if not storage.exists(key):
                storage.put_file(path, key, "text/plain")
            with session_scope() as s:
                p = s.get(Project, project_id)
                p.lut_key, p.lut_hash = key, sha
            killswitch.event(project_id, "lut_changed", size=size, name=os.path.basename(filename))
            return {"lut": {"key": key, "size": size, "name": os.path.basename(filename)}, **_regrade(project_id)}
        return await run_in_threadpool(store)


@router.delete("/projects/{project_id}/lut")
@curated("lut_change")
def remove_lut(project_id: ProjectId, s: Session = Depends(get_session)):
    p = project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    p.lut_key = p.lut_hash = None
    s.commit()
    killswitch.event(project_id, "lut_changed", removed=True)
    return {"lut": None, **_regrade(project_id)}


@router.post("/projects/{project_id}/color/auto-match")
@curated("grade_auto")
def run_auto_match(project_id: ProjectId, s: Session = Depends(get_session)):
    """Otomatik çekim eşleştirmeyi kuyruğa verir (para harcamaz): referans çekime göre renk ayarları çözülür, GRADE + MUX."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    from app.worker.tasks import auto_match_task
    auto_match_task.delay(project_id)
    return {"queued": True}


@router.post("/scenes/{scene_id}/grade/auto-undo")
@curated("grade_auto_undo")
def undo_auto_match(scene_id: SceneId, s: Session = Depends(get_session)):
    """Otomatik eşleştirmeyi geri alır: önceki renk ayarı geri gelir ve bu çekim bir daha otomatik eşleştirilmez."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    auto = sc.grade_auto or {}
    if not auto.get("applied"):
        raise HTTPException(409, f"{sc.scene_code}: otomatik eşleştirilmiş bir renk ayarı yok.")
    sc.grade = auto.get("previous") or {}
    sc.grade_auto = {"disabled": True, "undone": auto}
    s.commit()
    killswitch.event(str(sc.project_id), "grade_changed", scene_id=scene_id, code=sc.scene_code, undo_auto=True)
    return {"grade": grade_settings(sc.grade), **dispatch_result(s, sc)}


@router.post("/projects/{project_id}/continuity")
@curated("continuity_check")
def run_continuity(project_id: ProjectId, s: Session = Depends(get_session)):
    """Süreklilik kontrolünü kuyruğa verir (para harcamaz). Rapor overview ve GET ile okunur."""
    project_or_404(s, project_id)
    from app.worker.tasks import continuity
    continuity.delay(project_id)
    return {"queued": True}


@router.get("/projects/{project_id}/continuity")
def get_continuity(project_id: ProjectId, s: Session = Depends(get_session)):
    return project_or_404(s, project_id).continuity or {"pairs": []}

