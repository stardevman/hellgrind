"""Storyboard, taslak geçiş ve iyileştirme geçişi (Faz 14).

Akış: kareler hazırlanır -> yönetmen onaylar ya da değişiklik ister -> taslak geçiş (yalnızca test turu, geçen ilk
varyasyon) -> kaba kurgu kendiliğinden birleşir -> yönetmen zayıf çekimleri "İyileştir" ile işaretler -> iyileştirme
geçişi yalnızca işaretli çekimlerde tam turu çalıştırır.
"""
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.api.deps import ProjectId, SceneId, ensure_not_frozen, project_or_404, scene_or_404
from app.api.plan import (AwaitingBoard, Blocked, Busy, SequenceCapped, dispatch_all, dispatch_result, ensure_idle,
                          plan_and_dispatch)
from app.budget import killswitch
from app.config import settings
from app.core.rerender import Stage, board_stale, fingerprints
from app.core.state import load_assets, load_characters
from app.curation import curated
from app.db import get_session
from app.engine.storyboard import build_pdf, dialogue_of
from app.models import Scene, Sequence
from app.schemas import BoardIn, UpgradeIn
from app.style import bible
from app.worker.dispatch import build_chain

router = APIRouter(tags=["storyboard"])
BOARD_ACTIONS = {"approved": "board_approve", "changes_requested": "board_reject", "pending": "board_reset"}


def _scenes(s: Session, project_id: str) -> list[Scene]:
    return s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)).all()


@router.post("/projects/{project_id}/storyboard")
@curated("storyboard_prepare")
def prepare_storyboard(project_id: ProjectId, s: Session = Depends(get_session)):
    """Karesi olmayan ya da eskiyen her çekim için kare çizer (yalnızca STORYBOARD aşaması; görüntü üretilmez).
    Kareler sahneyi onaya bırakır. Varlık kontrolü uygulanmaz: storyboard, varlıklar kilitlenmeden önce de hazırlanır."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    style = bible.active_version(s, project_id)
    queued, current, skipped = [], [], []
    for sc in _scenes(s, project_id):
        fp = fingerprints(sc, load_characters(s, sc.id), [], load_assets(s, sc.id), style)
        if not board_stale(sc, fp):
            current.append(sc.scene_code)
            continue
        seq = s.get(Sequence, sc.sequence_id) if sc.sequence_id else None
        try:
            ensure_idle(sc)
        except Busy:
            skipped.append(sc.scene_code)
            continue
        if seq is not None and seq.status == "capped":
            skipped.append(sc.scene_code)
            continue
        sc.status, sc.last_error = "storyboard", None
        s.commit()
        build_chain(project_id, str(sc.id), Stage.STORYBOARD).apply_async()
        queued.append(sc.scene_code)
    killswitch.event(project_id, "storyboard_queued", queued=len(queued))
    return {"queued": queued, "current": current, "skipped": skipped}


@router.post("/scenes/{scene_id}/board/draw")
@curated("storyboard_prepare")
def draw_board(scene_id: SceneId, paid: bool = False, s: Session = Depends(get_session)):
    """Tek çekimin karesini çizer (yalnızca STORYBOARD; görüntü üretilmez). Ref2VA çekiminde varsayılan kare yereldir
    (ücretsiz); paid=true ücretli kareyi yeni seed'lerle her zaman yeniden çizer (Qwen-Image / Qwen-Image-Edit ya da
    Flux); ücretli kaynak yoksa 409 döner (iş kuyruğa alınmış gibi görünmesin)."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    if paid and not settings.dry_run:
        from app.core.h3mode import keyframe_model, strict_refs
        from app.engine.storyboard import PAID_SOURCES, board_source
        from app.config import project_settings
        from app.models import Project
        chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
        strict = strict_refs(project_settings(s.get(Project, sc.project_id).settings))
        if board_source("fl2va", True, keyframe_model(sc, chars, assets, strict)) not in PAID_SOURCES:
            raise HTTPException(409, f"{sc.scene_code}: ücretli kare kaynağı yok (RunPod'da keyframe.json / "
                                     "edit_keyframe.json iş akışı ya da fal.ai anahtarı); kare yeniden çizilemez.")
    sc.status, sc.last_error = "storyboard", None
    s.commit()
    from app.worker import tasks
    tasks.storyboard_scene.si(project_id=str(sc.project_id), scene_id=scene_id, standalone=True, paid=paid).apply_async()
    return {"queued": [sc.scene_code], "paid": paid}


@router.post("/scenes/{scene_id}/board")
@curated(lambda kw, r: BOARD_ACTIONS[kw["body"].status])
def review_board(scene_id: SceneId, body: BoardIn, s: Session = Depends(get_session)):
    """Yönetmenin kararı: approved | changes_requested (not ile) | pending (kararı geri al)."""
    sc = scene_or_404(s, scene_id)
    if body.status == "approved" and not sc.board_key:
        raise HTTPException(409, f"{sc.scene_code}: onaylanacak storyboard karesi yok; önce kareleri hazırla.")
    sc.board_status = body.status
    if body.note is not None:
        sc.board_note = body.note.strip() or None
    s.commit()
    killswitch.event(str(sc.project_id), "board_status", scene_id=scene_id, code=sc.scene_code, status=body.status)
    return {"scene_id": scene_id, "board_status": sc.board_status, "board_note": sc.board_note}


@router.post("/scenes/{scene_id}/board/accept")
@curated("board_approve")
def accept_board(scene_id: SceneId, s: Session = Depends(get_session)):
    """Kareyi elle onayla: kalite ya da kimlik kontrolünü geçemeyen ilk kare yönetmenin gözüyle kabul edilir ve video
    aşamasına geçilir (proje donmuşsa yalnızca onaylanır; üretime devam edince video üretilir)."""
    sc = scene_or_404(s, scene_id)
    if not sc.board_key:
        raise HTTPException(409, f"{sc.scene_code}: onaylanacak ilk kare yok; önce kareyi çizdirin.")
    previous = sc.board_note
    sc.board_status = "approved"
    sc.board_note = "Elle onaylandı" + (f" (kontrol: {previous})" if previous else "")
    if sc.status in ("board_review", "failed", "needs_review"):
        sc.status, sc.last_error = "parsed", None
    s.commit()
    pid = str(sc.project_id)
    killswitch.event(pid, "board_status", scene_id=scene_id, code=sc.scene_code, status="approved", manual=True)
    redraw = chain_redraw(s, sc)
    if killswitch.is_frozen(pid):
        return {"scene_id": scene_id, "board_status": "approved", "stages": [], "frozen": True, "chain_redraw": redraw}
    return {"scene_id": scene_id, "board_status": "approved", "frozen": False, "chain_redraw": redraw,
            **dispatch_result(s, sc)}


def chain_redraw(s: Session, sc: Scene) -> list[str]:
    """Bu kareden zincirle türeyen ve karesi eski ya da eksik olan sonraki çekimler (kodlar)."""
    from app.core.chain import parent_of, stale, successors
    from app.engine.storyboard import chain_enabled
    out = []
    if not chain_enabled():
        return out
    for nxt in successors(s, sc):
        parent = parent_of(s, nxt)
        if not nxt.board_key or stale(nxt, parent) or (parent is not None and parent.scene_code in out):
            out.append(nxt.scene_code)
    return out


@router.get("/scenes/{scene_id}/chain-impact")
def chain_impact(scene_id: SceneId, s: Session = Depends(get_session)):
    """Bu kare yeniden çizilirse (ya da kabul edilip zincir devam ederse) yeniden üretilecekler: N kare (kendisi ve
    zincirde ondan türeyenler), M klip (önceki çekim dahil, klibi olan çekimler) ve tahmini maliyet."""
    from app.budget import estimate as costs
    from app.config import project_settings
    from app.core.h3mode import edit_mode
    from app.engine.storyboard import dependents
    from app.models import Project, Variation
    sc = scene_or_404(s, scene_id)
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    from app.core.chain import next_continuous
    shots = dependents(s, sc)
    frames = [x for x in shots if x.id == sc.id or x.seq_order > sc.seq_order]
    ends = [x for x in frames if cfg.get("chain_end_frame", True) and next_continuous(s, x) is None]
    clipped = [x for x in shots if s.scalar(select(Variation.id).where(
        Variation.scene_id == x.id, Variation.status.in_(("passed", "refined"))).limit(1)) is not None]
    from app.budget import pricing
    frame_usd = pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode=edit_mode(cfg)) * len(frames) \
        + pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode="lightning") * len(ends)
    shot_costs = [costs.shot(cfg, x.scene_code, float(x.duration_s or 4.0), x.engine or "h3") for x in clipped]
    clip_usd = sum(c["expected_usd"] - c["frame_usd"] for c in shot_costs)          # kare ayrıca sayıldı
    return {"frames": len(frames), "end_frames": len(ends), "clips": len(clipped),
            "estimate_usd": round(frame_usd + clip_usd, 4),
            "frame_codes": [x.scene_code for x in frames], "clip_codes": [x.scene_code for x in clipped]}


@router.post("/projects/{project_id}/chain/reset")
@curated("chain_reset")
def chain_reset(project_id: ProjectId, s: Session = Depends(get_session)):
    """Temiz başlangıç: bütün çekimlerin klipleri "eski" işaretlenir (silinmez), zincirdeki kareler (her sekansın ilk
    karesi hariç) eskir ve videodan önce yeniden çizilir; çekimler yeniden üretilmeye hazır olur."""
    from app.core.chain import parent_of
    from app.models import Variation
    project_or_404(s, project_id)
    if killswitch.has_running_tasks(project_id):
        raise HTTPException(409, "Üretim sürüyor; önce 'Üretimi durdur' ile durdurun.")
    reason = "temiz başlangıç"
    clips = stale = 0
    for sc in _scenes(s, project_id):
        for v in s.scalars(select(Variation).where(Variation.scene_id == sc.id,
                                                   Variation.status.in_(("passed", "refined", "generated")))):
            v.params = {**(v.params or {}), "stale_keyframe": reason}
            clips += 1
        sc.visual_hash = None
        if sc.status not in ("blocked",):
            sc.status, sc.last_error = "parsed", None
        if parent_of(s, sc) is not None and sc.board_key:
            sc.board_parent = "reset"                       # önceki kareyle bağı koptu: zincirle yeniden çizilir
            stale += 1
    s.commit()
    killswitch.event(project_id, "chain_reset", clips=clips, stale_frames=stale)
    return {"clips": clips, "stale_frames": stale}


@router.post("/scenes/{scene_id}/board/redraw-chain")
@curated("storyboard_prepare")
def redraw_chain(scene_id: SceneId, s: Session = Depends(get_session)):
    """"Sonraki N kareyi yeniden çiz": bu kareden türeyen eski ya da eksik zincir karelerini sırayla çizer (zincirin son
    eski karesi istenir; make_board öncekileri en eskisinden başlayarak çizer). Görüntü üretilmez."""
    from app.core.chain import successors
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    codes = chain_redraw(s, sc)
    if not codes:
        return {"queued": [], "codes": []}
    last = next(x for x in reversed(successors(s, sc)) if x.scene_code == codes[-1])
    ensure_idle(last)
    from app.worker import tasks
    tasks.storyboard_scene.si(project_id=str(sc.project_id), scene_id=str(last.id), standalone=True).apply_async()
    return {"queued": [last.scene_code], "codes": codes}


@router.post("/projects/{project_id}/boards/approve")
@curated("board_approve_all")
def approve_strip(project_id: ProjectId, s: Session = Depends(get_session)):
    """Storyboard önce: şeritteki bütün kareler (ilk kareler ve son kareler) tek tıkla onaylanır, ardından videolar
    başlar. İnceleme bekleyen (değişiklik istenen) ya da karesi olmayan çekim onaylanmaz ve listelenir; yönetmen önce o
    kareyi düzeltir (yeniden çizim zincirde sonraki kareleri de yeniden çizer)."""
    project_or_404(s, project_id)
    approved, review, missing = [], [], []
    for sc in _scenes(s, project_id):
        if not sc.board_key:
            missing.append(sc.scene_code)
        elif sc.board_status == "changes_requested":
            review.append(sc.scene_code)
        else:
            if sc.board_status != "approved":
                sc.board_status = "approved"
                approved.append(sc.scene_code)
            if sc.status == "board_review":
                sc.status, sc.last_error = "parsed", None
    s.commit()
    killswitch.event(project_id, "board_strip_approved", approved=approved, review=review, missing=missing)
    if killswitch.is_frozen(project_id):
        return {"approved": approved, "review": review, "missing": missing, "frozen": True, "planned": {}}
    return {"approved": approved, "review": review, "missing": missing, **dispatch_all(s, project_id)}


@router.post("/scenes/{scene_id}/board/end/redraw")
@curated("end_board_redraw")
def redraw_end_board(scene_id: SceneId, s: Session = Depends(get_session)):
    """Çekim sonu karesini (Kn_end) yeni seed'lerle yeniden çizer. Çekimin klipleri eski son kareden üretildiyse "eski"
    işaretlenir. Gerekmiyorsa (sonraki çekim continuous ya da ayar kapalı) 409."""
    from app.config import project_settings
    from app.engine.storyboard import end_wanted
    from app.models import Project
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    if not end_wanted(s, sc, cfg, load_characters(s, sc.id), load_assets(s, sc.id)):
        raise HTTPException(409, f"{sc.scene_code}: son kare gerekmiyor (sonraki çekim kesintisiz devam ediyor, çekim "
                                 "FL2VA değil ya da proje ayarı kapalı).")
    if not sc.board_key:
        raise HTTPException(409, f"{sc.scene_code}: önce çekimin ilk karesi çizilmeli.")
    from app.worker import tasks
    tasks.end_board_scene.si(project_id=str(sc.project_id), scene_id=scene_id).apply_async()
    return {"queued": [sc.scene_code]}


@router.post("/scenes/{scene_id}/upgrade")
@curated(lambda kw, r: "upgrade_request" if kw["body"].requested else "upgrade_cancel")
def flag_upgrade(scene_id: SceneId, body: UpgradeIn, s: Session = Depends(get_session)):
    """Kaba kurguda zayıf bulunan çekimi iyileştirme geçişi için işaretler (ya da işareti kaldırır)."""
    sc = scene_or_404(s, scene_id)
    sc.upgrade_requested = body.requested
    s.commit()
    return {"scene_id": scene_id, "upgrade_requested": sc.upgrade_requested}


@router.post("/projects/{project_id}/draft-pass")
@curated("draft_pass")
def draft_pass(project_id: ProjectId, s: Session = Depends(get_session)):
    """Taslak geçiş: görüntüsü üretilecek her çekim yalnızca test turuyla üretilir (onay zorunluysa yalnızca onaylılar).
    Son çekim bitince kaba kurgu kendiliğinden birleştirilir. Tam tur hiç çalışmaz."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    result = dispatch_all(s, project_id, mode="draft")
    drafted = [code for code, stages in result["planned"].items() if "VISUAL" in stages]
    killswitch.event(project_id, "draft_pass", shots=len(drafted))
    return {**result, "drafted": drafted}


@router.post("/projects/{project_id}/upgrade-pass")
@curated("upgrade_pass")
def upgrade_pass(project_id: ProjectId, s: Session = Depends(get_session)):
    """İyileştirme geçişi: yalnızca "İyileştir" ile işaretli taslak çekimlerde tam tur. Test turu klipleri aynı seed'lerle
    yeniden kullanılır; yalnızca tam tur dalgaları ücretlendirilir."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    planned, skipped, busy, blocked, awaiting, capped = {}, [], [], {}, [], []
    for sc in _scenes(s, project_id):
        if not sc.upgrade_requested:
            continue
        if sc.cut_tier != "draft":
            skipped.append(sc.scene_code)            # zaten tam turdan geçmiş ya da hiç üretilmemiş
            continue
        try:
            planned[sc.scene_code] = plan_and_dispatch(s, sc, rerun_visual=True, mode="full")
        except Busy:
            busy.append(sc.scene_code)
        except Blocked as e:
            blocked[sc.scene_code] = [m["name"] for m in e.payload()["missing"]]
        except AwaitingBoard:
            awaiting.append(sc.scene_code)
        except SequenceCapped:
            capped.append(sc.scene_code)
    killswitch.event(project_id, "upgrade_pass", shots=len(planned))
    return {"planned": planned, "skipped": skipped, "busy": busy, "blocked": blocked, "awaiting_board": awaiting,
            "capped": capped}


@router.get("/projects/{project_id}/storyboard.pdf")
def storyboard_pdf(project_id: ProjectId, s: Session = Depends(get_session)):
    """Sayfa başına 6 kare; sekans başlıkları, çekim kodu, kamera, süre ve diyalog. Çekim kodları sırayla PDF'in
    Subject alanında da durur."""
    p = project_or_404(s, project_id)
    sequences = {q.id: (q.code, q.title) for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
    rows = [{"code": sc.scene_code, "sequence": sequences.get(sc.sequence_id), "camera": sc.camera or {},
             "duration": sc.duration_s, "board_status": sc.board_status, "note": sc.board_note,
             "dialogue": dialogue_of(s, sc.id), "board_key": sc.board_key} for sc in _scenes(s, project_id)]
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        frames = {}
        for r in rows:
            frames[r["code"]] = None
            if r["board_key"] and storage.exists(r["board_key"]):
                with Image.open(storage.download(r["board_key"], os.path.join(tmp, f"{r['code']}.jpg"))) as im:
                    frames[r["code"]] = im.copy()
        path = build_pdf(p.title, rows, frames, os.path.join(tmp, "storyboard.pdf"))
        data = Path(path).read_bytes()
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="storyboard.pdf"'})

