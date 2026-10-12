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
    if "camera" in changes:                                       # yönetmenin kamerası: kesme kamerası atanmaz
        changes["camera"] = {**changes["camera"], "manual": True}
    if changes.get("cut_method") == "auto":
        changes["cut_method"] = None
    if changes.get("entry_mode") == "walk_in":                    # devam çekimi + açık giriş eylemi
        changes["transition_in"] = "continuous"
    elif changes.get("entry_mode") == "cut":                      # varsayılan: belirgin kamera değişikliğiyle kesme
        changes["transition_in"] = None
    if "prompt_spec" in changes:                                  # düzeltme turlarının eklediği anahtarlar kalır
        changes["prompt_spec"] = {**(sc.prompt_spec or {}), **changes["prompt_spec"]}
    opts = dict(sc.video_opts or {})                              # fal video: çözünürlük ve model sesi (auto: Ayarlar)
    if "video_resolution" in changes:
        value = changes.pop("video_resolution")
        opts.pop("resolution", None) if value == "auto" else opts.update(resolution=value)
    if "video_audio" in changes:
        value = changes.pop("video_audio")
        opts.pop("audio", None) if value == "auto" else opts.update(audio=value == "true")
    if opts != (sc.video_opts or {}):
        sc.video_opts = opts or None
    for key, value in changes.items():
        setattr(sc, key, value)
    if "action_complexity" in changes and "engine" not in changes:
        from app.config import project_settings
        from app.models import Project
        sc.engine = route_engine(sc, project_settings(s.get(Project, sc.project_id).settings))
    if {"camera", "description", "entry_mode", "transition_in"} & changes.keys() and sc.transition_auto is not None:
        from app.core.chain import sync_seams                     # kadraj değişti: geçişler yeniden sınıflanır
        s.flush()
        sync_seams(s, sc.project_id)
    s.commit()
    return {"ok": True, "hint": "Değişikliği uygulamak için sahneyi yeniden üret."}


@router.post("/scenes/{scene_id}/render")
@curated(lambda kw, r: None if kw.get("dry_run") else "force_rerender" if kw.get("force_visual") else "render")
def render(scene_id: SceneId, force_visual: bool = False, thaw: bool = False, dry_run: bool = False,
           s: Session = Depends(get_session)):
    """Yalnızca değişen girdilerin aşamalarını çalıştırır; force_visual görüntüyü baştan üretir. thaw: proje donmuşsa
    yalnızca bu çekim için donma kaldırılır, çekimin işi bitince proje yeniden dondurulur.
    dry_run: kuru çalıştırma; hiçbir şey kuyruğa verilmez ve fal.ai'ye gidilmez: uç nokta, tam istek gövdesi (yüklenecek
    dosyalar yerel anahtar ve SHA-256 olarak), referans numaralandırması, maliyet tahmini ve motor/mod gerekçesi."""
    sc = scene_or_404(s, scene_id)
    if dry_run:
        from app.api.render_plan import scene_dry_run
        return scene_dry_run(s, sc, force_visual=force_visual)
    if thaw:
        from app.budget import killswitch
        killswitch.thaw_for_scene(str(sc.project_id), scene_id)
    return dispatch_result(s, sc, force_visual=force_visual, restyle=True)    # yönetmenin açık isteği


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
    return {"version": version, "track_id": str(track.id), **dispatch_result(s, sc, post_only=True)}


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
def promote(scene_id: SceneId, variation_id: VariationId, override: bool = False, full: bool = False,
            s: Session = Depends(get_session)):
    """Küratör seçimi: Rafine Havuz'dan bir varyasyonu 1. sıraya alır; yalnızca dudak senkronu ve montaj yeniden çalışır.
    override: yönetmen geçersiz kılması: kapıdan kalan (eski olmayan) aday da seçilebilir. full: seçilen adayın seed'iyle
    tam tur çözünürlüğünde (720p) yeni klip üretilir ve o klip 1. sıraya girer."""
    sc = scene_or_404(s, scene_id)
    v = s.get(Variation, variation_id)
    if v is None or str(v.scene_id) != scene_id:
        raise HTTPException(404, "Bu sahnenin böyle bir varyasyonu yok")
    if (v.refined_rank is None or full) and (override or full):
        return _override(s, sc, v, full)
    if v.refined_rank is None:
        raise HTTPException(404, "Bu sahnenin Rafine Havuz'unda böyle bir varyasyon yok (kapıdan kalan adayı seçmek "
                                 "için yönetmen seçimi: override=true)")
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
        from app.engine.finishing import _reset_auto_grade
        _reset_auto_grade(s, sc)                    # klip değişti: renk eşleme montajdan sonra yeniden
        s.commit()
    return {**dispatch_result(s, sc, post_only=True), **_mixed(s, sc)}     # seçim asla görüntü üretmez


def _mixed(s: Session, sc: Scene) -> dict:
    """Seçilen klip parçanın kardeşlerinden farklı bir üretimdense uyarı ve tek tık seçenekleri."""
    from app.engine import scene_parts
    g = scene_parts.scene_generations(s, sc).get(sc.scene_code)
    if not g:
        return {}
    return {"warning": g["message"], "mixed": g,
            "options": [{"action": "adopt", "label": "Bütün sahneyi bu motorla değiştir",
                         "path": f"/scenes/{sc.id}/generation/adopt?variation_id={g['variation_id']}"},
                        {"action": "shot", "label": "Bu çekimi ayrı üret",
                         "path": f"/scenes/{sc.id}/shot-engine?engine={g['engine']}"}]}


def _override(s: Session, sc: Scene, v: Variation, full: bool) -> dict:
    """Yönetmen geçersiz kılması: eski olmayan (bugünkü ilk/son kareden üretilmiş) aday. Karantinadaki video geri alınır."""
    from app.config import project_settings
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import current_conditioning
    from app.models import Project
    from app.quality import gate
    from app.style import bible
    from app.worker.dispatch import claim_dispatch
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    params = v.params or {}
    cond = current_conditioning(s, sc)
    if params.get("stale_keyframe") or (cond and params.get("cond") != cond):
        raise HTTPException(409, "Bu aday eski bir kareden üretildi; seçilemez. Bugünkü kareden üretilen bir aday seçin.")
    if v.status in ("queued", "running", "failed"):
        raise HTTPException(409, "Bu adayın videosu yok.")
    if full:                                        # aynı seed ile tam tur (720p): görevde üretilir, sonra montaj
        from app.core.rerender import Stage, plan_rerender, stage_names
        from app.worker import tasks
        from app.worker.dispatch import build_chain
        project = s.get(Project, sc.project_id)
        tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)))
        _, fp = plan_rerender(sc, load_characters(s, sc.id), tracks, load_assets(s, sc.id),
                              bible.active_version(s, sc.project_id), project.lut_hash)
        post_stages = Stage.LIPSYNC | Stage.MUX | (Stage.GRADE if fp.grade is not None else Stage.NONE)
        if not claim_dispatch(sc.id):
            raise HTTPException(409, f"{sc.scene_code} için üretim zaten sıraya alındı.")
        sc.status, sc.last_error = "pending", None
        s.commit()
        post = build_chain(str(sc.project_id), str(sc.id), post_stages)
        (tasks.override_full.si(project_id=str(sc.project_id), scene_id=str(sc.id), variation_id=str(v.id))
         | post).apply_async()
        return {"stages": ["VISUAL", *stage_names(post_stages)], "override": "full"}
    key = v.video_key
    if not (key and storage.exists(key)):
        quarantine = params.get("quarantine_key")
        if not (quarantine and storage.exists(quarantine)):
            raise HTTPException(409, "Bu adayın videosu silinmiş (karantinada yok).")
        key = f"variations/{sc.id}/{v.id}.mp4"
        storage.copy(quarantine, key)               # karantinadan geri alınır (kopya; karantina kaydı kalır)
        v.video_key = key
    style = bible.active_version(s, sc.project_id)
    fp = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), style)
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    # "Eski stil" işaretli çekimde görsel özet korunur: seçim, klibi yeni stille üretilmiş saymaz.
    held = sc.visual_hash if bible.stale_view(sc) else fp
    gate.promote_override(s, sc.id, v.id, {"status": "refined", "visual_hash": held,
                                           "lipsync_video_key": None,
                                           "final_video_key": None, "last_error": None},
                          int(cfg["refined_k"]), {"from": str(v.id), "was": v.status, "gate": v.anomalies or []})
    from app.engine.finishing import _reset_auto_grade
    _reset_auto_grade(s, s.get(Scene, sc.id))       # klip değişti (karma kurgu olabilir): renk eşleme montajdan sonra
    s.commit()
    from app.budget import killswitch
    killswitch.event(str(sc.project_id), "director_override", scene_id=str(sc.id), code=sc.scene_code,
                     variation=str(v.id), full=False)
    return {**dispatch_result(s, sc, post_only=True), "override": "promote", **_mixed(s, sc)}
