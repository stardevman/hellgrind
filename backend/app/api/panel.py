"""Kontrol panelini besleyen uç noktalar: ön ayarlar, senaryo yükleme, toplu görünüm, medya, film."""
import json
from typing import Literal
from collections import defaultdict
from types import SimpleNamespace

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import storage
from app.api.deps import ProjectId, ensure_not_frozen, project_or_404
from app.api.plan import dispatch_all
from app.api.projects import display_status
from app.api.sequences import sequences_out
from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS, project_settings, settings
from app.curation import curated
from app.db import get_session
from app.hermes.ingest import ingest
from app.hermes.rules import SAMPLE_SCRIPT
from app.core.h3mode import (edit_references, effective_mode, face_expected, fl2va_identity_warning, keyframe_model,
                             strict_refs)
from app.core.hashing import grade_settings
from app.engine import fal_plan
from app.engine.iteration import reference_set
from app.engine.providers import h3_disabled
from app.engine.methods import suggest_method
from app.core.chain import camera_of
from app.engine.storyboard import chain_enabled, clean_key, parent_token
from app.hermes import lint
from app.style import bible
from app.models import Asset, AudioTrack, Character, Revision, Scene, SceneAsset, SceneCharacter, Variation
from app.schemas import EstimateIn, ScriptIn

router = APIRouter(tags=["panel"])

PRESETS = {
    "preview": {"probe_n": 4, "full_n": 8, "wave_size": 4, "max_correction_rounds": 2},
    "brief": {},           # proje varsayılanları: 10 test + 50'ye kadar tam varyasyon
    # Yalnızca MiniMax H3 sunucusuyla en ucuz ücretli deneme: çekim başına en çok 4 klip (2 test + 2 tam), 480p, düzeltme
    # turu yok. Referans kontrolü uyarıya iner: varlık onay akışı ve hareket testleri (GPU süresi) atlanır. Ses yereldir.
    "trial": {"probe_n": 2, "full_n": 2, "wave_size": 2, "refined_k": 1, "max_correction_rounds": 0,
              "probe_resolution": "480p", "full_resolution": "720p", "lint_strictness": "warn",
              "paid_audio": False},
    # Üretim (ekonomik): gerçek film için en az aday. Çekim başına 3 test (480p) + 3 tam (720p) klip, tek düzeltme turu,
    # havuzda tek aday. Proje varsayılanları (10 test / 50 tam) değişmez.
    "economy": {"probe_n": 3, "probe_resolution": "480p", "full_n": 3, "full_resolution": "720p", "wave_size": 3,
                "refined_k": 1, "max_correction_rounds": 1},
}

# Diyalogsuz (dudak senkronu yok), aksiyonsuz, 3 saniyelik tek çekimlik deneme senaryosu (MiniMax H3 4 sn üretir, kesilir).
# İngilizce: sağlayıcıya çeviri olmadan gider. Kerem konuşmadığı için işaretle kadroya alınır.
TRIAL_SCRIPT = """# SEQUENCE: Trial

EXT. ROOFTOP - SUNSET

[CHARACTER: Kerem]

Kerem stands on a rooftop at sunset, looking over a calm city.
"""
MEDIA_PREFIXES = ("thumbs/", "variations/", "quarantine/", "lipsync/", "final/", "timeline/", "audio/", "uploads/",
                  "assets/", "motion/", "storyboard/", "external/", "guides/", "revisions/", "graded/", "luts/",
                  "seam/", "keyframes/", "cutin/", "state_refs/", "frames/")
MUTABLE_PREFIXES = ("storyboard/",)             # içerik-adresli değil: yeniden çizim aynı anahtara yazar
EVENT_LIMIT = 30


def quality_meta() -> dict:
    """Hangi kalite skoru kullanılıyor; gerçekse kurulu mu (modeller yüklenmez)."""
    from app.quality import gate, scorer
    try:
        mode = gate.scorer_mode()
    except ValueError:
        return {"mode": "invalid", "ready": False, "reason": f"Bilinmeyen QUALITY_SCORER: {settings.quality_scorer}"}
    reason = scorer.unavailable() if mode == "real" else None
    return {"mode": mode, "ready": reason is None, "reason": reason}


def measure_of(params: dict | None) -> dict | None:
    """Gerçek skorun panel için özeti: stil ve tarif uyumu, karakter başına yüz benzerliği."""
    p = params or {}
    detail = p.get("frame_score_detail") if p.get("stage") == "first_frame" else p.get("score_detail")
    if not detail or "alignment" not in detail:
        return None
    return {"alignment": detail["alignment"], "identity": detail.get("identity", {}), "faces_seen": detail.get("faces_seen", 0)}


def fal_request_view(req: dict | None) -> dict | None:
    """Çekimin fal.ai işi (bekleyen ya da son): kimlik, uç nokta, durum, hata ve sonucun özeti."""
    if not req:
        return None
    return {k: req.get(k) for k in ("request_id", "endpoint", "engine", "mode", "status", "error", "submitted_at",
                                    "completed_at", "result") if req.get(k) is not None}


@router.get("/meta")
def meta():
    return {"dry_run": settings.dry_run, "hermes_mode": settings.hermes_mode, "tts_engine": settings.tts_engine,
            "h3_platform": settings.h3_platform, "keyframe_platform": settings.keyframe_platform,
            "video": {"draft_engine": settings.video_draft_engine, "final_engine": settings.video_final_engine,
                      "engines": [{"id": e, "label": fal_plan.ENGINE_TR[e], "disabled": h3_disabled(e)}
                                  for e in ("h3max", "seedance25", "seedance", "h3")],
                      "h3max_resolutions": list(fal_plan.H3_RESOLUTIONS),
                      "seedance25_resolutions": list(fal_plan.SEEDANCE_RESOLUTIONS),
                      "h3max_resolution": settings.h3max_resolution,
                      "seedance25_resolution": settings.seedance25_resolution,
                      "seedance25_audio": settings.seedance25_audio},
            "quality": quality_meta(), "presets": PRESETS, "defaults": DEFAULT_PROJECT_SETTINGS,
            "sample_script": SAMPLE_SCRIPT, "trial_script": TRIAL_SCRIPT}


@router.post("/projects/{project_id}/script")
@curated("script_upload")
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


@router.post("/estimate")
def estimate(body: EstimateIn, s: Session = Depends(get_session)):
    """Tahmini maliyet, çekim başına ve toplam (ağ yok; ücretsiz modda da gerçek fiyatlarla). Senaryo verilirse ondan
    (proje oluşturma ekranı), verilmezse projenin çekimlerinden."""
    from app.budget import estimate as costs
    if body.preset is not None and body.preset not in PRESETS:
        raise HTTPException(422, f"Bilinmeyen ön ayar: {body.preset}")
    if body.text is None and body.project_id is None:
        raise HTTPException(422, "Senaryo ya da proje gerekli")
    raw = dict(project_or_404(s, body.project_id).settings or {}) if body.project_id else {}
    cfg = project_settings({**raw, **PRESETS.get(body.preset or "", {}), **body.settings})
    result = costs.from_script(body.text, cfg) if body.text is not None else costs.from_project(s, body.project_id, cfg)
    return {**result, "settings": {k: cfg[k] for k in ("probe_n", "probe_resolution", "full_n", "full_resolution",
                                                         "max_correction_rounds")},
            "platform": settings.h3_platform, "dry_run": settings.dry_run}


@router.post("/projects/{project_id}/render-all")
@curated("render_all")
def render_all(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    return dispatch_all(s, project_id)


def _media(key: str | None, version: str | None = None) -> str | None:
    """version: yeniden çizilince aynı anahtara yazılan kare (storyboard) için içerik sürümü; adres değişir, tarayıcı eski
    kareyi önbellekten göstermez."""
    if not key:
        return None
    return f"/api/media?key={key}" + (f"&v={version[:12]}" if version else "")


def _film(key: str | None) -> dict | None:
    if not key:
        return None
    try:
        cut = json.loads(storage.read_bytes(key.rsplit(".", 1)[0] + ".json"))
    except Exception:                                 # kesme listesi henüz yok ya da okunamıyor
        cut = None
    return {"url": _media(key), "cut": cut}


def chain_info(scenes: list, enabled: bool = True, scaled: set | None = None) -> dict:
    """Anahtar kare zinciri (core/chain.py), sorgusuz: çekimin geçişi, zincirdeki önceki çekimi, karesinin eskiyip
    eskimediği ve bu kareden türeyen eski/eksik sonraki kareler ("sonraki N kareyi yeniden çiz")."""
    from app.core.chain import (CHAINED, angle_tokens, camera_of, compose_hint, cut_attempts, cut_method, pose_words,
                                stale, transition_of, wants_compose)
    angles_on = settings.qwen_edit_angles == "on" and bool(settings.qwen_edit_angles_lora)
    out, by_seq = {}, defaultdict(list)
    for sc in scenes:                                  # scenes seq_order sırasında
        by_seq[sc.sequence_id or sc.project_id].append(sc)
    for shots in by_seq.values():
        for i, sc in enumerate(shots):
            transition = transition_of(sc)
            parent = shots[i - 1] if enabled and i > 0 and transition in CHAINED else None
            out[sc.id] = {"transition": transition, "transition_explicit": sc.transition_in,
                          "chain_parent": parent.scene_code if parent else None,
                          "chain_stale": bool(parent is not None and stale(sc, parent)), "chain_redraw": [],
                          "cut_method": sc.cut_method, "cut_method_effective": None, "cut_attempts": [],
                          "board_method": sc.board_method, "angle_tokens": None}
            if transition == "cut" and i > 0:          # kesme karesinin yöntemi (kırp + iyileştir, açı LoRA'sı, geniş plan)
                prev_cam, cam = camera_of(shots[i - 1]), camera_of(sc)
                method = cut_method(prev_cam, cam, sc.cut_method, compose=wants_compose(sc, sc.id in (scaled or set())))
                out[sc.id].update(cut_method_effective=method,
                                  cut_attempts=cut_attempts(method, prev_cam, cam, angles_on),
                                  angle_tokens=angle_tokens(prev_cam, cam)["text"],
                                  pose_words=pose_words(getattr(sc, "director_note", None)),
                                  compose_hint=compose_hint(sc),
                                  scaled=sc.id in (scaled or set()))
        for i, sc in enumerate(shots):                  # her kare için: kendisinden türeyen eski ya da eksik kareler
            pending, j = [], i + 1
            while j < len(shots) and out[shots[j].id]["chain_parent"]:
                if out[shots[j].id]["chain_stale"] or not shots[j].board_key or pending:
                    pending.append(shots[j].scene_code)
                j += 1
            out[sc.id]["chain_redraw"] = pending
    return out


@router.get("/projects/{project_id}/overview")
def overview(project_id: ProjectId, s: Session = Depends(get_session)):
    """Paneli tek çağrıda besler. Sorgu sayısı sahne sayısından bağımsızdır (sahne başına sorgu yok)."""
    p = project_or_404(s, project_id)
    scenes = s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)).all()
    ids = [sc.id for sc in scenes]
    cfg = project_settings(p.settings)
    variations, tracks, cast, items = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    refs = defaultdict(list)                          # sahne -> karakterlerin referans görselleri (Ref2VA seti)
    places = defaultdict(list)                        # sahne -> mekân varlıkları
    carried = defaultdict(list)                       # sahne -> zincirden gelen karakterler
    scaled: set = set()                               # gerçek boyutu yazılı karakter ya da nesnesi olan sahneler
    revisions = defaultdict(dict)                     # varyasyon -> {revizyon: sürüm}
    names = dict(s.execute(select(Character.id, Character.name).where(Character.project_id == project_id)).all())
    if ids:
        for v in s.scalars(select(Variation).where(Variation.scene_id.in_(ids))
                           .order_by(Variation.tier, Variation.wave_no, Variation.created_at)):
            variations[v.scene_id].append(v)
        all_vids = [v.id for vs in variations.values() for v in vs]
        if all_vids:
            for r in s.scalars(select(Revision).where(Revision.variation_id.in_(all_vids))):
                revisions[r.variation_id][r.id] = r.version
        for t in s.scalars(select(AudioTrack).where(AudioTrack.scene_id.in_(ids), AudioTrack.is_active)
                           .order_by(AudioTrack.start_offset_s, AudioTrack.kind)):
            tracks[t.scene_id].append(t)
        for scene_id, name, aid, status, images, role, scale, kind in s.execute(
                select(SceneCharacter.scene_id, Character.name, Asset.id, Asset.status, Character.reference_images,
                       SceneCharacter.role, Asset.scale, Asset.type)
                .join(Character, Character.id == SceneCharacter.character_id)
                .outerjoin(Asset, Asset.id == Character.asset_id)
                .where(SceneCharacter.scene_id.in_(ids)).order_by(Character.name)):
            cast[scene_id].append(name)
            if scale:                                  # gerçek boyutu yazılı karakter: kesme karesi sıfırdan kurulur
                scaled.add(scene_id)
            if role == "chain":                        # zincirden gelen: önceki çekimin karesinde kaldı
                carried[scene_id].append(name)
            refs[scene_id].append(SimpleNamespace(name=name, reference_images=images or [], asset_id=aid,
                                                  asset=SimpleNamespace(id=aid, status=status) if aid else None))
            items[scene_id].append((str(aid) if aid else None, "character", name, status))
        for scene_id, aid, type_, name, status, references, scale in s.execute(
                select(SceneAsset.scene_id, Asset.id, Asset.type, Asset.name, Asset.status, Asset.references, Asset.scale)
                .join(Asset, Asset.id == SceneAsset.asset_id)
                .where(SceneAsset.scene_id.in_(ids)).order_by(Asset.type, Asset.name)):
            items[scene_id].append((str(aid), type_, name, status))
            if scale and type_ != "location":
                scaled.add(scene_id)
            if type_ == "location":                    # Qwen-Image-Edit karesinin mekân görseli
                places[scene_id].append(SimpleNamespace(id=aid, type=type_, name=name, status=status,
                                                        references=references or {}))
    check = cfg["lint_strictness"] != "off"

    strict = strict_refs(cfg)
    chain = chain_info(scenes, chain_enabled(), scaled)
    from app.core.chain import plans_for
    seams = plans_for(s, project_id) if scenes else {}

    def shot_assets(sid) -> list:
        return [r.asset for r in refs[sid] if r.asset] + places[sid]

    scenes_out = [{
        "id": str(sc.id), "scene_code": sc.scene_code, "status": sc.status, "engine": sc.engine,
        "engine_label": fal_plan.ENGINE_TR.get(sc.engine or "", sc.engine), "engine_disabled": h3_disabled(sc.engine),
        "video_opts": sc.video_opts or {}, "fal_request": fal_request_view(sc.fal_request),
        "upgrade_available": fal_plan.is_fal(sc.engine) and sc.cut_tier == "draft"
                             and sc.engine != fal_plan.final_engine(cfg),
        "sequence_id": str(sc.sequence_id) if sc.sequence_id else None,
        "board_status": sc.board_status, "board_note": sc.board_note, "board_url": _media(sc.board_key, sc.board_sha or sc.board_hash),
        "upgrade_requested": sc.upgrade_requested, "cut_tier": sc.cut_tier, "draft_url": _media(sc.draft_video_key),
        "method": sc.method, "method_hint": suggest_method(sc), "external_url": _media(sc.external_key),
        "h3_mode": effective_mode(sc, refs[sc.id], shot_assets(sc.id)),
        "h3_mode_explicit": sc.h3_mode,
        "h3_mode_warning": fl2va_identity_warning(sc, refs[sc.id], shot_assets(sc.id), strict),
        "keyframe_model": keyframe_model(sc, refs[sc.id], shot_assets(sc.id), strict),
        **chain[sc.id],
        "transition_reason": sc.transition_reason, "transition_hint": sc.transition_hint,
        "arrival": next((((v.params or {}).get("score_detail") or {}).get("arrival") for v in variations[sc.id]
                         if v.refined_rank == 1), None),                  # kazanan klibin varış skoru
        "chain_cast": carried[sc.id],
        "end_board_url": _media(sc.end_board_key, sc.end_board_sha), "end_board_note": sc.end_board_note,
        "end_board_score": sc.end_board_score,
        "end_board_current": bool(sc.end_board_key and sc.end_board_parent and sc.end_board_parent == parent_token(sc)),
        "edit_refs": [{"name": r["name"], "kind": r["kind"], "url": _media(r["key"])}
                      for r in edit_references(sc, refs[sc.id], shot_assets(sc.id), strict)],
        "face_expected": face_expected(sc, cast[sc.id]), "face_expected_explicit": sc.face_expected,
        "board_score": sc.board_score, "board_source": sc.board_source,
        "keyframe_source": "upload" if sc.keyframe_key else ("board" if sc.board_key and sc.board_source else None),
        "keyframe_url": _media(sc.keyframe_key or (clean_key(sc.board_key) if sc.board_key and sc.board_source else None),
                               sc.keyframe_hash if sc.keyframe_key else (sc.board_sha or sc.board_hash)),
        "h3_refs": [{"name": r["name"], "url": _media(r["key"])} for r in reference_set(refs[sc.id])],
        "guide_url": _media(sc.guide_key), "grade": grade_settings(sc.grade), "graded": bool(sc.graded_video_key),
        "grade_auto": sc.grade_auto,
        "lipsync_ready": bool(sc.lipsync_video_key),
        "heading": (sc.script_text or "").split("\n")[0], "description": sc.description, "emotion": sc.emotion,
        "camera": sc.camera, "camera_plan": sc.camera_plan, "camera_effective": camera_of(sc), "entry_mode": sc.entry_mode,
        "entering": (seams.get(sc.id) or {}).get("entering") or [],
        "entry_options": ["cut", "walk_in"] if (seams.get(sc.id) or {}).get("entering") else None,
        "duration_s": sc.duration_s, "action_complexity": sc.action_complexity,
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
            "reason": (v.params or {}).get("purge_reason"), "stage": (v.params or {}).get("stage"),
            "stale": (v.params or {}).get("stale_keyframe"),                       # eski kareden üretildi
            "overridable": bool(v.refined_rank is None and not (v.params or {}).get("stale_keyframe")
                                and (v.params or {}).get("cond")                 # koşul kaydı olan (bu turdan) aday
                                and v.status in ("passed", "purged", "generated")
                                and (v.video_key or (v.params or {}).get("quarantine_key"))),   # yönetmen seçebilir
            "override": bool((v.params or {}).get("director_override")),
            "review": ((v.params or {}).get("score_detail") or {}).get("review"),   # sınırda: inceleme önerilir
            "arrival": ((v.params or {}).get("score_detail") or {}).get("arrival"),  # son kare hedefe ne kadar vardı
            "last_frame_kind": (v.params or {}).get("last_frame_kind"),
            "measure": measure_of(v.params),
            "thumb_url": _media(v.keyframe_key),
            "video_url": _media(v.video_key or (v.params or {}).get("quarantine_key")),   # karantinadakiler de önizlenir
            "source": (v.params or {}).get("source", "generate"), "revisions": len(revisions[v.id]),
            "revision": revisions[v.id].get(v.revision_id) if v.revision_id else None,   # etkin revizyonun sürümü
        } for v in variations[sc.id]],
    } for sc in scenes]

    events = [json.loads(e) for e in killswitch.r().lrange(killswitch.EVENTS.format(project_id), 0, EVENT_LIMIT - 1)]
    timeline, draft_timeline = _film(p.timeline_key), _film(p.draft_timeline_key)
    return {
        "project": {"id": str(p.id), "title": p.title, "status": p.status,
                    "display_status": display_status(p.status, [sc.status for sc in scenes]),
                    "budget_limit_usd": float(p.budget_limit_usd), "spent_usd": float(p.spent_usd),
                    "reserved_usd": float(p.reserved_usd), "frozen": killswitch.is_frozen(project_id),
                    "soft_limited": killswitch.is_soft_limited(project_id), "settings": cfg,
                    "dry_run": settings.dry_run, "style_version": bible.active_version(s, project_id),
                    "lut": bool(p.lut_key), "continuity": p.continuity,
                    "asset_counts": dict(s.execute(select(Asset.status, func.count()).where(Asset.project_id == project_id)
                                                   .group_by(Asset.status)).all())},
        "scenes": scenes_out, "sequences": sequences_out(s, project_id), "events": events, "timeline": timeline,
        "draft_timeline": draft_timeline,
    }


MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".wav": "audio/wav",
               ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".png": "image/png", ".pdf": "application/pdf",
               ".zip": "application/zip"}
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 1 << 20


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """"bytes=a-b" / "bytes=a-" / "bytes=-n" -> (başlangıç, bitiş); geçersizse 416, başlık yoksa None."""
    if not header:
        return None
    m = RANGE.match(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        raise HTTPException(416, "Geçersiz aralık", headers={"Content-Range": f"bytes */{size}"})
    if m.group(1) == "":
        start, end = max(0, size - int(m.group(2))), size - 1
    else:
        start, end = int(m.group(1)), min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
    if start >= size or start > end:
        raise HTTPException(416, "Aralık dosyanın dışında", headers={"Content-Range": f"bytes */{size}"})
    return start, end


@router.api_route("/media", methods=["GET", "HEAD"])
def media(request: Request, key: str = Query(min_length=1, max_length=512), redirect: bool = False):
    """Depodaki bir nesneyi aynı adresten (API'nin origin'i) sunar. Küçük ve değişmez dosyalar (jpg, json) doğrudan;
    videolar ve sesler Range desteğiyle (206 Partial Content, Accept-Ranges): tarayıcı ileri-geri sarabilir ve başka bir
    host'a (MinIO) yönlenmediği için gömülü tarayıcılarda da (Claude masaüstü paneli) oynar. redirect=true: imzalı MinIO
    adresine 307 (dış paylaşım bağlantısı)."""
    if not key.startswith(MEDIA_PREFIXES) or any(part in ("", ".", "..") for part in key.split("/")):
        raise HTTPException(400, "Geçersiz medya anahtarı")
    cache = {"Cache-Control": "public, max-age=31536000, immutable"}
    if key.endswith((".jpg", ".json")) and request.method == "GET":
        try:
            data = storage.read_bytes(key)
        except Exception:
            raise HTTPException(404, "Dosya bulunamadı")
        kind = "image/jpeg" if key.endswith(".jpg") else "application/json"
        if key.startswith(MUTABLE_PREFIXES):         # yeniden çizilen kare aynı anahtara yazılır: her seferinde doğrula
            import hashlib
            etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
            headers = {"Cache-Control": "no-cache", "ETag": etag}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers=headers)
            return Response(data, media_type=kind, headers=headers)
        return Response(data, media_type=kind, headers=cache)
    try:
        head = storage.client().head_object(Bucket=settings.s3_bucket, Key=key)
    except Exception:
        raise HTTPException(404, "Dosya bulunamadı")
    if redirect:
        return RedirectResponse(storage.presign(key), status_code=307)
    size = int(head["ContentLength"])
    ext = "." + key.rsplit(".", 1)[-1].lower() if "." in key else ""
    kind = MEDIA_TYPES.get(ext) or ("image/jpeg" if ext == ".jpg" else head.get("ContentType") or "application/octet-stream")
    span = _byte_range(request.headers.get("range"), size)
    start, end = span or (0, size - 1)
    headers = {**cache, "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status = 206 if span else 200
    if request.method == "HEAD" or size == 0:
        return Response(status_code=status, headers=headers, media_type=kind)
    body = storage.client().get_object(Bucket=settings.s3_bucket, Key=key, Range=f"bytes={start}-{end}")["Body"]

    def stream():
        try:
            for chunk in iter(lambda: body.read(CHUNK), b""):
                yield chunk
        finally:
            body.close()
    return StreamingResponse(stream(), status_code=status, headers=headers, media_type=kind)


@router.post("/projects/{project_id}/assemble")
@curated("assemble")
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
