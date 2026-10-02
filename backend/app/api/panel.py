"""Kontrol panelini besleyen uç noktalar: ön ayarlar, senaryo yükleme, toplu görünüm, medya, film."""
import json
from typing import Literal
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import storage
from app.api.deps import ProjectId, ensure_not_frozen, project_or_404
from app.api.plan import dispatch_all
from app.api.sequences import sequences_out
from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS, project_settings, settings
from app.db import get_session
from app.hermes.ingest import ingest
from app.hermes.rules import SAMPLE_SCRIPT
from app.hermes import lint
from app.style import bible
from app.models import Asset, AudioTrack, Character, Scene, SceneAsset, SceneCharacter, Variation
from app.schemas import ScriptIn

router = APIRouter(tags=["panel"])

PRESETS = {
    "preview": {"probe_n": 4, "full_n": 8, "wave_size": 4, "max_correction_rounds": 2},
    "brief": {},           # proje varsayılanları: 10 test + 50'ye kadar tam varyasyon
}
MEDIA_PREFIXES = ("thumbs/", "variations/", "quarantine/", "lipsync/", "final/", "timeline/", "audio/", "uploads/",
                  "assets/", "motion/", "storyboard/")
EVENT_LIMIT = 30


@router.get("/meta")
def meta():
    return {"dry_run": settings.dry_run, "hermes_mode": settings.hermes_mode, "tts_engine": settings.tts_engine,
            "presets": PRESETS, "defaults": DEFAULT_PROJECT_SETTINGS, "sample_script": SAMPLE_SCRIPT}


@router.post("/projects/{project_id}/script")
def upload_script(project_id: ProjectId, body: ScriptIn, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    try:
        summary = ingest(project_id, body.text, auto_music=body.auto_music)
    except killswitch.ProjectFrozen:                  # LLM modunda ayrıştırma sırasında bütçe dolabilir
        raise HTTPException(423, "Bütçe limiti doldu ya da proje donduruldu")
    result = {"planned": {}, "busy": [], "blocked": {}, "capped": [], "awaiting_board": [], "frozen": False}
    if body.auto_render:
        s.expire_all()
        result = dispatch_all(s, project_id)
    return {**summary, **result}


@router.post("/projects/{project_id}/render-all")
def render_all(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    return dispatch_all(s, project_id)


def _media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


def _film(key: str | None) -> dict | None:
    if not key:
        return None
    try:
        cut = json.loads(storage.read_bytes(key.rsplit(".", 1)[0] + ".json"))
    except Exception:                                 # kesme listesi henüz yok ya da okunamıyor
        cut = None
    return {"url": _media(key), "cut": cut}


@router.get("/projects/{project_id}/overview")
def overview(project_id: ProjectId, s: Session = Depends(get_session)):
    """Paneli tek çağrıda besler. Sorgu sayısı sahne sayısından bağımsızdır (sahne başına sorgu yok)."""
    p = project_or_404(s, project_id)
    scenes = s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)).all()
    ids = [sc.id for sc in scenes]
    cfg = project_settings(p.settings)
    variations, tracks, cast, items = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    names = dict(s.execute(select(Character.id, Character.name).where(Character.project_id == project_id)).all())
    if ids:
        for v in s.scalars(select(Variation).where(Variation.scene_id.in_(ids))
                           .order_by(Variation.tier, Variation.wave_no, Variation.created_at)):
            variations[v.scene_id].append(v)
        for t in s.scalars(select(AudioTrack).where(AudioTrack.scene_id.in_(ids), AudioTrack.is_active)
                           .order_by(AudioTrack.start_offset_s, AudioTrack.kind)):
            tracks[t.scene_id].append(t)
        for scene_id, name, aid, status in s.execute(
                select(SceneCharacter.scene_id, Character.name, Asset.id, Asset.status)
                .join(Character, Character.id == SceneCharacter.character_id)
                .outerjoin(Asset, Asset.id == Character.asset_id)
                .where(SceneCharacter.scene_id.in_(ids)).order_by(Character.name)):
            cast[scene_id].append(name)
            items[scene_id].append((str(aid) if aid else None, "character", name, status))
        for scene_id, aid, type_, name, status in s.execute(
                select(SceneAsset.scene_id, Asset.id, Asset.type, Asset.name, Asset.status)
                .join(Asset, Asset.id == SceneAsset.asset_id)
                .where(SceneAsset.scene_id.in_(ids)).order_by(Asset.type, Asset.name)):
            items[scene_id].append((str(aid), type_, name, status))
    check = cfg["lint_strictness"] != "off"

    scenes_out = [{
        "id": str(sc.id), "scene_code": sc.scene_code, "status": sc.status, "engine": sc.engine,
        "sequence_id": str(sc.sequence_id) if sc.sequence_id else None,
        "board_status": sc.board_status, "board_note": sc.board_note, "board_url": _media(sc.board_key),
        "upgrade_requested": sc.upgrade_requested, "cut_tier": sc.cut_tier, "draft_url": _media(sc.draft_video_key),
        "heading": (sc.script_text or "").split("\n")[0], "description": sc.description, "emotion": sc.emotion,
        "camera": sc.camera, "duration_s": sc.duration_s, "action_complexity": sc.action_complexity,
        "spent_usd": float(sc.spent_usd), "last_error": sc.last_error, "correction_count": sc.correction_count,
        "characters": cast[sc.id], "final_url": _media(sc.final_video_key),
        "assets": [{"id": aid, "type": type_, "name": name, "status": status} for aid, type_, name, status in items[sc.id]],
        "missing": [{"id": m.asset_id, "type": m.type, "name": m.name, "status": m.status}
                    for m in (lint.find_missing(items[sc.id], cfg["asset_min_status"]) if check else [])],
        "tracks": [{"id": str(t.id), "kind": t.kind, "source": t.source, "text": t.text, "emotion": t.emotion,
                    "start_offset_s": t.start_offset_s, "gain_db": t.gain_db, "version": t.version,
                    "character_id": str(t.character_id) if t.character_id else None,
                    "character": names.get(t.character_id), "mood": (t.params or {}).get("mood")} for t in tracks[sc.id]],
        "variations": [{
            "id": str(v.id), "tier": v.tier, "wave_no": v.wave_no, "status": v.status, "seed": v.seed,
            "score": v.composite_score, "rank": v.refined_rank, "cost_usd": float(v.cost_usd),
            "anomalies": sorted({a["type"] for a in (v.anomalies or [])}),
            "reason": (v.params or {}).get("purge_reason"), "thumb_url": _media(v.keyframe_key),
            "video_url": _media(v.video_key or (v.params or {}).get("quarantine_key")),   # karantinadakiler de önizlenir
        } for v in variations[sc.id]],
    } for sc in scenes]

    events = [json.loads(e) for e in killswitch.r().lrange(killswitch.EVENTS.format(project_id), 0, EVENT_LIMIT - 1)]
    timeline, draft_timeline = _film(p.timeline_key), _film(p.draft_timeline_key)
    return {
        "project": {"id": str(p.id), "title": p.title, "status": p.status,
                    "budget_limit_usd": float(p.budget_limit_usd), "spent_usd": float(p.spent_usd),
                    "reserved_usd": float(p.reserved_usd), "frozen": killswitch.is_frozen(project_id),
                    "soft_limited": killswitch.is_soft_limited(project_id), "settings": cfg,
                    "dry_run": settings.dry_run, "style_version": bible.active_version(s, project_id),
                    "asset_counts": dict(s.execute(select(Asset.status, func.count()).where(Asset.project_id == project_id)
                                                   .group_by(Asset.status)).all())},
        "scenes": scenes_out, "sequences": sequences_out(s, project_id), "events": events, "timeline": timeline,
        "draft_timeline": draft_timeline,
    }


@router.get("/media")
def media(key: str = Query(min_length=1, max_length=512)):
    """Depodaki bir nesneyi sunar. Küçük ve değişmez dosyalar (jpg, json) doğrudan ve önbellekli; videolar ve sesler,
    tarayıcının MinIO'ya erişeceği adresle imzalanmış bir URL'ye 307 ile yönlenir (ileri-geri sarılabilir)."""
    if not key.startswith(MEDIA_PREFIXES) or any(part in ("", ".", "..") for part in key.split("/")):
        raise HTTPException(400, "Geçersiz medya anahtarı")
    if key.endswith((".jpg", ".json")):
        try:
            data = storage.read_bytes(key)
        except Exception:
            raise HTTPException(404, "Dosya bulunamadı")
        kind = "image/jpeg" if key.endswith(".jpg") else "application/json"
        return Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    if not storage.exists(key):
        raise HTTPException(404, "Dosya bulunamadı")
    return RedirectResponse(storage.presign(key), status_code=307)


@router.post("/projects/{project_id}/assemble")
def assemble(project_id: ProjectId, kind: Literal["final", "draft"] = "final", s: Session = Depends(get_session)):
    """Tamamlanan sahneleri tek filmde birleştirir; kind=draft kaba kurguyu (taslak geçişin klipleri) birleştirir.
    Para harcamaz: donmuş projede de çalışır."""
    project_or_404(s, project_id)
    rows = s.execute(select(Scene.status, Scene.final_video_key, Scene.draft_video_key)
                     .where(Scene.project_id == project_id)).all()
    count = sum(1 for st, final, draft in rows
                if (st == "done" and final) or (kind == "draft" and draft))
    if not count:
        raise HTTPException(409, "Henüz tamamlanmış sahne yok")
    from app.worker.tasks import assemble_timeline
    if kind == "draft":
        assemble_timeline.delay(project_id, "draft")
    else:
        assemble_timeline.delay(project_id)
    return {"queued": True, "scenes": count, **({"kind": kind} if kind == "draft" else {})}
