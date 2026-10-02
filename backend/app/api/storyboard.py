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
from app.api.plan import AwaitingBoard, Blocked, Busy, SequenceCapped, dispatch_all, ensure_idle, plan_and_dispatch
from app.budget import killswitch
from app.config import settings
from app.core.rerender import Stage, board_stale, fingerprints
from app.core.state import load_assets, load_characters
from app.db import get_session
from app.engine.storyboard import build_pdf, dialogue_of
from app.models import Scene, Sequence
from app.schemas import BoardIn, UpgradeIn
from app.style import bible
from app.worker.dispatch import build_chain

router = APIRouter(tags=["storyboard"])


def _scenes(s: Session, project_id: str) -> list[Scene]:
    return s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)).all()


@router.post("/projects/{project_id}/storyboard")
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


@router.post("/scenes/{scene_id}/board")
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


@router.post("/scenes/{scene_id}/upgrade")
def flag_upgrade(scene_id: SceneId, body: UpgradeIn, s: Session = Depends(get_session)):
    """Kaba kurguda zayıf bulunan çekimi iyileştirme geçişi için işaretler (ya da işareti kaldırır)."""
    sc = scene_or_404(s, scene_id)
    sc.upgrade_requested = body.requested
    s.commit()
    return {"scene_id": scene_id, "upgrade_requested": sc.upgrade_requested}


@router.post("/projects/{project_id}/draft-pass")
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

