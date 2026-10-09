from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.api.deps import SceneId, VariationId, ensure_not_frozen, scene_or_404
from app.api.plan import dispatch_result, ensure_idle
from app.core.hashing import stable_hash
from app.curation import curated
from app.db import get_session
from app.engine.providers import route_engine
from app.hermes.ingest import dialogue_hash, music_hash
from app.models import AudioTrack, Character, Scene, Variation
from app.schemas import AudioIn, Emotion, ScenePatch

router = APIRouter(tags=["scenes"])
EMOTIONS = set(Emotion.__args__)


@router.get("/scenes/{scene_id}")
def get_scene(scene_id: SceneId, s: Session = Depends(get_session)):
    sc = scene_or_404(s, scene_id)
    return {c.name: getattr(sc, c.name) for c in Scene.__table__.columns}


@router.patch("/scenes/{scene_id}")
@curated("scene_edit")
def patch_scene(scene_id: SceneId, body: ScenePatch, s: Session = Depends(get_session)):
    sc = scene_or_404(s, scene_id)
    changes = body.model_dump(exclude_none=True)
    if changes.get("h3_mode") == "auto":
        changes["h3_mode"] = None
    if "face_expected" in changes:
        changes["face_expected"] = {"auto": None, "true": True, "false": False}[changes["face_expected"]]
    if changes.get("transition_in") == "auto":                    # senaryodan gelen geçişe dön
        changes["transition_in"] = None
    if "prompt_spec" in changes:                                  # düzeltme turlarının eklediği anahtarlar kalır
        changes["prompt_spec"] = {**(sc.prompt_spec or {}), **changes["prompt_spec"]}
    for key, value in changes.items():
        setattr(sc, key, value)
    if "action_complexity" in changes and "engine" not in changes:
        sc.engine = route_engine(sc)                              # motor, aksiyon karmaşıklığından yönlendirilir
    s.commit()
    return {"ok": True, "hint": "Değişikliği uygulamak için sahneyi yeniden üret."}


@router.post("/scenes/{scene_id}/render")
@curated(lambda kw, r: "force_rerender" if kw.get("force_visual") else "render")
def render(scene_id: SceneId, force_visual: bool = False, thaw: bool = False, s: Session = Depends(get_session)):
    """Yalnızca değişen girdilerin aşamalarını çalıştırır; force_visual görüntüyü baştan üretir. thaw: proje donmuşsa
    yalnızca bu çekim için donma kaldırılır, çekimin işi bitince proje yeniden dondurulur."""
    sc = scene_or_404(s, scene_id)
    if thaw:
        from app.budget import killswitch
        killswitch.thaw_for_scene(str(sc.project_id), scene_id)
    return dispatch_result(s, sc, force_visual=force_visual)


def _content_hash(body: AudioIn, mood: str | None, emotion: str | None) -> str:
    """Yalnızca duyulan şeyi belirler; ingest ile aynı formül. Ofset ve kazanç ayrıca özetlenir, bu yüzden kazanç
    değişikliği bir repliği yeniden seslendirmez."""
    if body.kind == "dialogue" and body.source == "tts":
        return dialogue_hash(body.character_id, {"line": body.text, "emotion": emotion})
    if body.kind == "music" and body.source == "procedural":
        return music_hash(mood)
    return stable_hash({"kind": body.kind, "source": body.source, "file_key": body.file_key, "text": body.text,
                        "character": str(body.character_id)})


@router.put("/scenes/{scene_id}/audio")
@curated(lambda kw, r: "music_change" if kw["body"].kind == "music" else "audio_change")
def replace_audio(scene_id: SceneId, body: AudioIn, s: Session = Depends(get_session)):
    """Ses kanalının yeni sürümü: yalnızca gereken aşamalar çalışır (müzik değişince yalnızca montaj)."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))           # 423 dönen istek ses kanallarını değiştirmiş olmamalı
    ensure_idle(sc)

    if body.character_id and s.scalar(select(Character.id).where(
            Character.id == body.character_id, Character.project_id == sc.project_id)) is None:
        raise HTTPException(422, "Karakter bu projeye ait değil")
    if body.source == "upload" and not storage.exists(body.file_key):
        raise HTTPException(422, f"Ses dosyası depoda yok: {body.file_key}")
    mood = (body.params.get("mood") or body.emotion or sc.emotion) if body.kind == "music" else None
    if mood is not None and mood not in EMOTIONS:
        raise HTTPException(422, f"Geçersiz müzik duygusu: {mood}")
    emotion = body.emotion or (mood if body.kind == "music" else sc.emotion if body.kind == "dialogue" else None)

    tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.kind == body.kind)))
    if body.kind == "dialogue":
        retired = [t for t in tracks if t.id == body.replaces and t.is_active] if body.replaces else []
        if body.replaces and not retired:
            raise HTTPException(404, "Değiştirilecek aktif diyalog kanalı bu sahnede yok")
    else:
        retired = [t for t in tracks if t.is_active]            # müzik ve efektler türün tüm eski kanallarının yerini alır
    version = max((t.version for t in tracks), default=0) + 1
    for t in retired:
        t.is_active = False
    track = AudioTrack(scene_id=sc.id, character_id=body.character_id, kind=body.kind, source=body.source,
                       text=body.text, emotion=emotion, params={**body.params, **({"mood": mood} if mood else {})},
                       content_hash=_content_hash(body, mood, emotion), file_key=body.file_key,
                       start_offset_s=body.start_offset_s, gain_db=body.gain_db, is_active=True, version=version)
    s.add(track)
    s.commit()
    return {"version": version, "track_id": str(track.id), **dispatch_result(s, sc)}


@router.get("/scenes/{scene_id}/variations")
def list_variations(scene_id: SceneId, s: Session = Depends(get_session)):
    scene_or_404(s, scene_id)
    rows = s.scalars(select(Variation).where(Variation.scene_id == scene_id)
                     .order_by(Variation.tier, Variation.wave_no, Variation.created_at)).all()
    return [{"id": str(v.id), "tier": v.tier, "wave_no": v.wave_no, "status": v.status, "seed": v.seed,
             "engine": v.engine, "clip_score": v.clip_score, "composite_score": v.composite_score,
             "anomalies": v.anomalies, "refined_rank": v.refined_rank, "cost_usd": float(v.cost_usd),
             "video_key": v.video_key, "quarantine_key": (v.params or {}).get("quarantine_key"),
             "purge_reason": (v.params or {}).get("purge_reason")} for v in rows]


@router.post("/scenes/{scene_id}/promote/{variation_id}")
@curated("promote")
def promote(scene_id: SceneId, variation_id: VariationId, s: Session = Depends(get_session)):
    """Küratör seçimi: Rafine Havuz'dan bir varyasyonu 1. sıraya alır; yalnızca dudak senkronu ve montaj yeniden çalışır."""
    sc = scene_or_404(s, scene_id)
    v = s.get(Variation, variation_id)
    if v is None or str(v.scene_id) != scene_id or v.refined_rank is None:
        raise HTTPException(404, "Bu sahnenin Rafine Havuz'unda böyle bir varyasyon yok")
    ensure_not_frozen(str(sc.project_id))           # 423 dönen istek havuz sırasını değiştirmiş olmamalı
    ensure_idle(sc)
    if v.refined_rank != 1:
        current = s.scalar(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank == 1))
        old = v.refined_rank
        v.refined_rank = None                       # kısmi benzersiz indeks: takas adım adım, her adım flush edilerek
        s.flush()
        if current is not None:
            current.refined_rank = old
            s.flush()
        v.refined_rank = 1
        sc.lipsync_video_key = None                 # Stateless Re-render: LIPSYNC + MUX planlanır, VISUAL değil
        s.commit()
    return dispatch_result(s, sc)
