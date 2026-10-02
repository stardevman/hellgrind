from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import storage
from app.core.hashing import stable_hash
from app.core.rerender import ALL, Stage, plan_rerender, stage_names
from app.api.deps import SceneId, ensure_not_frozen
from app.db import get_session
from app.engine.iteration import load_characters
from app.models import AudioTrack, Scene, Variation
from app.schemas import AudioTrackIn, ScenePatch
from app.worker.dispatch import build_chain

router = APIRouter(tags=["scenes"])


def _scene_or_404(s: Session, scene_id: SceneId) -> Scene:
    sc = s.get(Scene, scene_id)
    if not sc:
        raise HTTPException(404)
    return sc


def _plan_and_dispatch(s: Session, sc: Scene, force: Stage = Stage.NONE) -> list[str]:
    pid = str(sc.project_id)
    ensure_not_frozen(pid)
    tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.is_active)).all()
    stages, _ = plan_rerender(sc, load_characters(s, sc.id), tracks)
    stages |= force
    job = build_chain(pid, str(sc.id), stages)
    if job:
        sc.status = "pending"
        s.commit()
        job.apply_async()
    return stage_names(stages)


@router.get("/scenes/{scene_id}")
def get_scene(scene_id: SceneId, s: Session = Depends(get_session)):
    sc = _scene_or_404(s, scene_id)
    return {c.name: getattr(sc, c.name) for c in Scene.__table__.columns}


@router.patch("/scenes/{scene_id}")
def patch_scene(scene_id: SceneId, body: ScenePatch, s: Session = Depends(get_session)):
    sc = _scene_or_404(s, scene_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(sc, k, v)
    s.commit()
    return {"ok": True, "hint": "Değişikliği uygulamak için POST /scenes/{id}/render çağır."}


@router.post("/scenes/{scene_id}/render")
def render(scene_id: SceneId, force_visual: bool = False, s: Session = Depends(get_session)):
    sc = _scene_or_404(s, scene_id)
    return {"stages": _plan_and_dispatch(s, sc, ALL if force_visual else Stage.NONE)}


@router.put("/scenes/{scene_id}/audio")
def replace_audio(scene_id: SceneId, body: AudioTrackIn, s: Session = Depends(get_session)):
    """Ses değiştir: yalnızca gereken aşamalar çalışır (video motoru tetiklenmez)."""
    sc = _scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))          # 423 dönen istek ses kanallarını değiştirmiş olmamalı
    prev = s.scalars(select(AudioTrack).where(
        AudioTrack.scene_id == sc.id, AudioTrack.kind == body.kind, AudioTrack.is_active,
        AudioTrack.character_id.is_not_distinct_from(body.character_id))).all()
    version = max((p.version for p in prev), default=0) + 1
    s.execute(update(AudioTrack).where(AudioTrack.id.in_([p.id for p in prev])).values(is_active=False))
    s.add(AudioTrack(scene_id=sc.id, version=version, is_active=True,
                     content_hash=stable_hash(body.model_dump(mode="json")), **body.model_dump()))
    s.commit()
    return {"version": version, "stages": _plan_and_dispatch(s, sc)}


@router.get("/scenes/{scene_id}/variations")
def list_variations(scene_id: SceneId, s: Session = Depends(get_session)):
    rows = s.scalars(select(Variation).where(Variation.scene_id == scene_id)
                     .order_by(Variation.tier, Variation.wave_no, Variation.created_at)).all()
    return [{"id": str(v.id), "tier": v.tier, "wave_no": v.wave_no, "status": v.status, "seed": v.seed,
             "clip_score": v.clip_score, "composite_score": v.composite_score, "anomalies": v.anomalies,
             "refined_rank": v.refined_rank, "cost_usd": float(v.cost_usd), "video_key": v.video_key}
            for v in rows]


@router.get("/scenes/{scene_id}/final-url")
def final_url(scene_id: SceneId, s: Session = Depends(get_session)):
    sc = _scene_or_404(s, scene_id)
    if not sc.final_video_key:
        raise HTTPException(404, "Henüz final video yok")
    return {"key": sc.final_video_key, "url": storage.presign(sc.final_video_key)}
