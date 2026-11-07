"""Hareketli animatik: çekimin sahneleme planını okuma, düzenleme, kurallara döndürme ve önizleme (ücretsiz, yerel).

Plan, yönetmenin elle düzenleyebildiği JSON'dur (post/staging.py). Boşsa kurallardan çıkarılır (kadraj, kamera hareketi,
duygu, kadro, görseller). Önizleme tek çekimi çizer; kukla sürümü (dummy) görünüş taşımayan rehber videodur: video modeline
hareket referansı olarak verilebilir.
"""
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import SceneId, scene_or_404
from app.curation import curated
from app.db import get_session, session_scope
from app.models import Scene
from app.post import animatic, staging
from app.post.pipeline import workspace

router = APIRouter(tags=["staging"])


def _shot(s, sc: Scene) -> dict:
    """Animatikteki hâliyle çekim (süre sesten ya da çekimden, replikler)."""
    shot = next((x for x in animatic.plan(s, str(sc.project_id)) if x["scene_id"] == str(sc.id)), None)
    if shot is None:
        raise HTTPException(404, "Çekim bulunamadı.")
    return shot


def view(s, sc: Scene) -> dict:
    shot = _shot(s, sc)
    images, info = animatic.shot_images(s, sc)
    with workspace() as tmp:
        local = {}
        if "frame" in images and storage.exists(images["frame"]):
            local["frame"] = storage.download(images["frame"], os.path.join(tmp, "frame" + (os.path.splitext(images["frame"])[1] or ".jpg")))
        plan = animatic.effective_plan(sc, images, info, shot["duration_s"], local) if images else None
    return {"scene_id": str(sc.id), "scene_code": sc.scene_code, "duration_s": shot["duration_s"],
            "source": "director" if sc.staging else ("rules" if images else None), "plan": plan,
            "images": sorted(images), "actions": list(staging.ACTIONS),
            "note": None if images else "Çekimin görseli yok (ilk kare, onaylı kare ya da kadro/mekân referansı): "
                                        "animatikte çekim kartı görünür."}


@router.get("/scenes/{scene_id}/staging")
def get_staging(scene_id: SceneId, s: Session = Depends(get_session)):
    sc = scene_or_404(s, scene_id)
    return view(s, sc)


@router.put("/scenes/{scene_id}/staging")
@curated("staging_edit")
def put_staging(scene_id: SceneId, plan: dict = Body(..., embed=True), s: Session = Depends(get_session)):
    """Yönetmenin planı: doğrulanır ve kaydedilir; animatik yeniden birleştirilince bu çekim yeniden çizilir."""
    sc = scene_or_404(s, scene_id)
    errs = staging.validate(plan)
    if errs:
        raise HTTPException(422, " ".join(errs))
    sc.staging = {**plan, "version": 1, "source": "director"}
    s.commit()
    return view(s, sc)


@router.delete("/scenes/{scene_id}/staging")
@curated("staging_reset")
def reset_staging(scene_id: SceneId, s: Session = Depends(get_session)):
    sc = scene_or_404(s, scene_id)
    sc.staging = None
    s.commit()
    return view(s, sc)


def _preview(scene_id: str, dummy: bool) -> dict:
    with session_scope() as s, workspace() as tmp:
        sc = s.get(Scene, scene_id)
        shot = _shot(s, sc)
        key, _ = animatic.staged_clip(s, sc, shot, tmp, dummy=dummy, label="" if dummy else sc.scene_code)
    if key is None:
        raise HTTPException(409, "Çekimin görseli yok: sahnelenecek bir şey bulunamadı.")
    return {"key": key, "url": f"/api/media?key={key}", "dummy": dummy, "duration_s": shot["duration_s"]}


@router.post("/scenes/{scene_id}/staging/preview")
async def preview_staging(scene_id: SceneId, dummy: bool = Query(False)):
    """Tek çekimi çizer (önbellekli). dummy=true: kukla rehber videosu (konum, hareket, kamera; görünüş yok)."""
    with session_scope() as s:
        scene_or_404(s, scene_id)
    return await run_in_threadpool(_preview, scene_id, dummy)
