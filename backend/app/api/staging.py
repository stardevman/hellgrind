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


def guide_info(s, sc: Scene, images: dict) -> dict:
    """Kukla rehberinin durumu ve video isteğine ekleyeceği yaklaşık bedel (çekimin motoru ve çözünürlüğüyle)."""
    from app.config import project_settings
    from app.engine import fal_plan
    from app.models import Project
    seconds = animatic.guide_shot(sc)["duration_s"]
    engine = sc.engine if sc.engine in animatic.GUIDE_SECONDS else fal_plan.draft_engine()
    out = {"on": bool(sc.motion_guide), "seconds": seconds, "engine": engine,
           "engine_label": fal_plan.ENGINE_TR.get(engine, engine), "extra_usd": None}
    if engine in animatic.GUIDE_SECONDS:
        cfg = project_settings(s.get(Project, sc.project_id).settings)
        res = fal_plan.resolution(engine, sc, cfg)
        n = len(images)
        base = fal_plan.cost(engine, res, sc.duration_s or 5.0, n, "ref")["reserve_usd"]
        more = fal_plan.cost(engine, res, sc.duration_s or 5.0, n, "ref", video_s=seconds)["reserve_usd"]
        out.update(extra_usd=round(more - base, 3), base_usd=round(base, 3), resolution=res)
    return out


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
            "images": sorted(images), "actions": list(staging.ACTIONS), "guide": guide_info(s, sc, images),
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
    """dummy: video modeline gidecek rehberin kendisi (video süresi, alt yazısız, künyesiz); değilse animatikteki klip."""
    with session_scope() as s, workspace() as tmp:
        sc = s.get(Scene, scene_id)
        shot = animatic.guide_shot(sc) if dummy else _shot(s, sc)
        key = animatic.staged_clip(s, sc, shot, tmp, dummy=dummy, label="" if dummy else sc.scene_code)
    if key is None:
        raise HTTPException(409, "Çekimin görseli yok: sahnelenecek bir şey bulunamadı.")
    return {"key": key, "url": f"/api/media?key={key}", "dummy": dummy, "duration_s": shot["duration_s"]}


@router.post("/scenes/{scene_id}/staging/preview")
async def preview_staging(scene_id: SceneId, dummy: bool = Query(False)):
    """Tek çekimi çizer (önbellekli). dummy=true: kukla rehber videosu (konum, hareket, kamera; görünüş yok)."""
    with session_scope() as s:
        scene_or_404(s, scene_id)
    return await run_in_threadpool(_preview, scene_id, dummy)


@router.put("/scenes/{scene_id}/staging/guide")
@curated("staging_guide")
def put_guide(scene_id: SceneId, on: bool = Body(..., embed=True), s: Session = Depends(get_session)):
    """Kukla rehberini video isteğine ekler ya da çıkarır. Görüntü özetine girer: açıkken plan (ve onayı) değişir,
    rehber değişince çekim yeniden üretilir. Bedel artar (referans videosu token'ı), planda görünür."""
    sc = scene_or_404(s, scene_id)
    sc.motion_guide = bool(on)
    s.commit()
    return view(s, sc)
