"""Kontrol paneli için toplu uç noktalar: proje listesi, senaryo yükleme, genel görünüm, medya, montaj."""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.api.scenes import _plan_and_dispatch
from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.api.deps import ProjectId, SceneId, VariationId, ensure_not_frozen
from app.db import get_session
from app.engine.iteration import load_characters
from app.hermes.ingest import ingest
from app.hermes.rules import SAMPLE_SCRIPT
from app.models import AudioTrack, Project, Scene, Variation
from app.schemas import ScriptIn

router = APIRouter(tags=["panel"])

PRESETS = {
    "preview": {"probe_n": 4, "full_n": 8, "wave_size": 4, "max_correction_rounds": 2,
                "probe_resolution": "480p", "full_resolution": "720p"},
    "brief": {},   # brifingdeki gibi: 10 test + 50'ye kadar tam varyasyon
}


@router.get("/meta")
def meta():
    return {"dry_run": settings.dry_run, "hermes_mode": settings.hermes_mode, "tts_engine": settings.tts_engine,
            "presets": PRESETS, "defaults": DEFAULT_PROJECT_SETTINGS, "sample_script": SAMPLE_SCRIPT}


@router.get("/projects")
def list_projects(s: Session = Depends(get_session)):
    rows = s.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [{"id": str(p.id), "title": p.title, "status": p.status, "spent_usd": float(p.spent_usd),
             "budget_limit_usd": float(p.budget_limit_usd)} for p in rows]


@router.post("/projects/{project_id}/script")
def upload_script(project_id: ProjectId, body: ScriptIn, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    ensure_not_frozen(project_id)
    try:
        summary = ingest(project_id, body.text, auto_music=body.auto_music)
    except killswitch.ProjectFrozen:                  # LLM modunda ayrıştırma sırasında bütçe dolabilir
        raise HTTPException(423, "Bütçe limiti doldu ya da proje donduruldu")
    planned = {}
    if body.auto_render:
        s.expire_all()
        for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all():
            planned[sc.scene_code] = _plan_and_dispatch(s, sc)
    return {**summary, "planned": planned}


@router.post("/projects/{project_id}/render-all")
def render_all(project_id: ProjectId, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    out = {}
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all():
        out[sc.scene_code] = _plan_and_dispatch(s, sc)
    return out


def _media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


@router.get("/projects/{project_id}/overview")
def overview(project_id: ProjectId, s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    scenes_out = []
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all():
        vs = s.scalars(select(Variation).where(Variation.scene_id == sc.id)
                       .order_by(Variation.tier.desc(), Variation.wave_no, Variation.created_at)).all()
        tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.is_active)
                           .order_by(AudioTrack.start_offset_s)).all()
        scenes_out.append({
            "id": str(sc.id), "scene_code": sc.scene_code, "status": sc.status, "engine": sc.engine,
            "heading": sc.script_text, "description": sc.description, "emotion": sc.emotion, "camera": sc.camera,
            "duration_s": sc.duration_s, "action_complexity": sc.action_complexity, "spent_usd": float(sc.spent_usd),
            "last_error": sc.last_error, "correction_count": sc.correction_count,
            "characters": [c.name for c in load_characters(s, sc.id)],
            "final_url": _media(sc.final_video_key),
            "tracks": [{"id": str(t.id), "kind": t.kind, "source": t.source, "text": t.text, "emotion": t.emotion,
                        "start_offset_s": t.start_offset_s, "gain_db": t.gain_db,
                        "character_id": str(t.character_id) if t.character_id else None,
                        "mood": (t.params or {}).get("mood")} for t in tracks],
            "variations": [{
                "id": str(v.id), "tier": v.tier, "wave_no": v.wave_no, "status": v.status, "seed": v.seed,
                "score": v.composite_score, "rank": v.refined_rank, "cost_usd": float(v.cost_usd),
                "anomalies": sorted({a["type"] for a in (v.anomalies or [])}),
                "reason": (v.params or {}).get("purge_reason"),
                "thumb_url": _media(v.keyframe_key),
                "video_url": _media(v.video_key or (v.params or {}).get("quarantine_key")),
            } for v in vs],
        })
    events = [json.loads(e) for e in killswitch.r().lrange(killswitch.EVENTS.format(project_id), 0, 30)]
    cut = None
    if p.timeline_key:
        try:
            cut = json.loads(storage.read_bytes(p.timeline_key.rsplit(".", 1)[0] + ".json"))
        except Exception:
            cut = None
    return {
        "project": {"id": str(p.id), "title": p.title, "status": p.status,
                    "budget_limit_usd": float(p.budget_limit_usd), "spent_usd": float(p.spent_usd),
                    "reserved_usd": float(p.reserved_usd), "frozen": killswitch.is_frozen(project_id),
                    "soft_limited": killswitch.is_soft_limited(project_id),
                    "settings": {**DEFAULT_PROJECT_SETTINGS, **(p.settings or {})}, "dry_run": settings.dry_run},
        "scenes": scenes_out,
        "events": events,
        "timeline": {"url": _media(p.timeline_key), "cut": cut} if p.timeline_key else None,
    }


@router.get("/media")
def media(key: str):
    if ".." in key or key.startswith("/"):
        raise HTTPException(400)
    if key.endswith((".jpg", ".json")):                # küçük ve değişmez: doğrudan, önbellekli
        try:
            data = storage.read_bytes(key)
        except Exception:
            raise HTTPException(404)
        mt = "image/jpeg" if key.endswith(".jpg") else "application/json"
        return Response(data, media_type=mt, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    return RedirectResponse(storage.presign(key), status_code=307)   # video: MinIO'dan, ileri-geri sarılabilir


@router.post("/scenes/{scene_id}/promote/{variation_id}")
def promote(scene_id: SceneId, variation_id: VariationId, s: Session = Depends(get_session)):
    """Küratör seçimi: Rafine Havuz'dan bir varyasyonu 1. sıraya alır; yalnızca ses ve montaj yeniden çalışır."""
    sc = s.get(Scene, scene_id)
    v = s.get(Variation, variation_id)
    if not sc or not v or str(v.scene_id) != scene_id or v.refined_rank is None:
        raise HTTPException(404, "Bu sahnenin Rafine Havuz'unda böyle bir varyasyon yok")
    ensure_not_frozen(str(sc.project_id))          # 423 dönen istek havuz sırasını değiştirmiş olmamalı
    if v.refined_rank != 1:
        cur = s.scalar(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank == 1))
        old = v.refined_rank
        v.refined_rank = None
        s.flush()
        if cur:
            cur.refined_rank = old
            s.flush()
        v.refined_rank = 1
        sc.lipsync_video_key = None        # Stateless Re-render: LIPSYNC + MUX planlanır, VISUAL değil
        s.commit()
    return {"stages": _plan_and_dispatch(s, sc)}


@router.post("/projects/{project_id}/assemble")
def assemble(project_id: ProjectId, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    from app.worker.tasks import assemble_timeline
    assemble_timeline.delay(project_id)
    return {"queued": True}
