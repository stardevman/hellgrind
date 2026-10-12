"""Çekim üretim yöntemi, dış çekim ve previs rehberi yükleme, elle düzeltme (Faz 15)."""
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import SceneId, VariationId, ensure_not_frozen, paid_errors, scene_or_404
from app.api.plan import dispatch_result, ensure_idle
from app.api.uploads import extension, receive
from app.config import settings
from app.core.hashing import visual_fingerprint
from app.curation import curated
from app.db import get_session, session_scope
from app.engine import methods
from app.engine.references import EXTENSIONS, sha_of, streams
from app.models import Revision, Scene, Variation
from app.quality.anomaly import detect_anomalies
from app.schemas import MethodIn, RevisionActiveIn

router = APIRouter(tags=["methods"])
VIDEO_EXTS, IMAGE_EXTS = EXTENSIONS["video"], EXTENSIONS["image"]
METHOD_TR = {"generate": "yapay zekâ üretimi", "previs_guide": "previs rehberi", "external": "dış çekim"}


def _ready(scene_id: str) -> None:
    """Yüklemeden önce: sahne var, proje dondurulmamış (yükleme sonrası montaj kuyruğa girer), sahne meşgul değil."""
    with session_scope() as s:
        sc = scene_or_404(s, scene_id)
        ensure_not_frozen(str(sc.project_id))
        ensure_idle(sc)


def _check_video(path: str, what: str) -> None:
    if "video" not in streams(path):
        raise HTTPException(422, f"{what}: dosyada görüntü akışı yok.")
    anomalies = detect_anomalies(path)
    if anomalies:
        found = ", ".join(sorted({a["type"] for a in anomalies}))
        raise HTTPException(422, f"{what} kalite kontrolünden geçemedi ({found}).")


def _apply_method(scene_id: str, method: str, **fields) -> dict:
    """Yöntemi (ve dosya alanlarını) yazar; dış çekimi rafine #1 yapar; planı çıkarıp kuyruğa verir."""
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        sc.method = method
        for key, value in fields.items():
            setattr(sc, key, value)
        fingerprint = visual_fingerprint(sc, [], []) if method == "external" else None
    if method == "external":
        methods.register_external(scene_id, fingerprint)        # görüntü üretilmez, para harcanmaz
    with session_scope() as s:
        result = dispatch_result(s, s.get(Scene, scene_id))
    return {"method": method, **result}


@router.post("/scenes/{scene_id}/keyframe")
@curated("keyframe_upload")
async def upload_keyframe(scene_id: SceneId, request: Request, filename: str = Query(..., min_length=1, max_length=200)):
    """MiniMax H3 ilk karesi (ham gövde, ?filename=kare.png): storyboard karesinin yerine geçer; FL2VA ve Ref2VA videoları
    bu kareden başlar (kompozisyon kareyle kontrol edilir). JPEG'e çevrilir; siyah ya da tek renk kare reddedilir.
    Görsel özet değişir: uygulamak için sahne yeniden üretilir (kendiliğinden üretim başlamaz)."""
    ext = extension(filename, IMAGE_EXTS, "İlk kare")
    await run_in_threadpool(_ready, scene_id)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "upload" + ext)
        await receive(request, path)

        def store():
            from PIL import Image, UnidentifiedImageError
            from app.quality.anomaly import detect_frame_anomalies
            jpg = os.path.join(tmp, "keyframe.jpg")
            try:
                with Image.open(path) as im:
                    im.convert("RGB").save(jpg, "JPEG", quality=95)
            except (UnidentifiedImageError, OSError):
                raise HTTPException(422, "İlk kare okunamadı: PNG, JPEG ya da WebP yükleyin.")
            anomalies = detect_frame_anomalies(jpg)
            if anomalies:
                found = ", ".join(sorted({a["type"] for a in anomalies}))
                raise HTTPException(422, f"İlk kare kalite kontrolünden geçemedi ({found}).")
            sha = sha_of(jpg)
            key = f"keyframes/{scene_id}/{sha}.jpg"
            if not storage.exists(key):
                storage.put_file(jpg, key, "image/jpeg")
            with session_scope() as s:
                sc = s.get(Scene, scene_id)
                sc.keyframe_key, sc.keyframe_hash = key, sha
            return {"scene_id": scene_id, "keyframe_url": f"/api/media?key={key}",
                    "hint": "İlk kare kaydedildi; uygulamak için sahneyi yeniden üret."}
        return await run_in_threadpool(store)


@router.delete("/scenes/{scene_id}/keyframe")
@curated("keyframe_clear")
def clear_keyframe(scene_id: SceneId, s: Session = Depends(get_session)):
    """Yüklenen ilk kareyi kaldırır: video yeniden storyboard karesinden başlar."""
    sc = scene_or_404(s, scene_id)
    sc.keyframe_key = sc.keyframe_hash = None
    s.commit()
    return {"scene_id": scene_id, "hint": "Yüklenen ilk kare kaldırıldı; uygulamak için sahneyi yeniden üret."}


@router.post("/scenes/{scene_id}/external")
@curated("external_upload")
async def upload_external(scene_id: SceneId, request: Request, filename: str = Query(..., min_length=1, max_length=200)):
    """Dışarıda bitirilmiş çekim (ham gövde, ?filename=kovalama.mp4). Görüntü üretilmez; klip anomali kontrolünden,
    sesten ve montajdan geçer. Dosyanın içerik özeti görsel özetin yerine geçer."""
    ext = extension(filename, VIDEO_EXTS, "Dış çekim")
    await run_in_threadpool(_ready, scene_id)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "external" + ext)
        await receive(request, path)

        def store():
            _check_video(path, "Dış çekim")
            sha = sha_of(path)
            key = f"external/{scene_id}/{sha}{ext}"
            if not storage.exists(key):
                storage.put_file(path, key)
            return _apply_method(scene_id, "external", external_key=key, external_hash=sha)
        return await run_in_threadpool(store)


@router.post("/scenes/{scene_id}/guide")
@curated("guide_upload")
async def upload_guide(scene_id: SceneId, request: Request, filename: str = Query(..., min_length=1, max_length=200)):
    """Previs (3D taslak) rehber videosu. Sahne rehberle üretilir: ücretsiz modda rehber, stil kılavuzunun renk diliyle
    doğrudan kullanılır; ücretli modda video-to-video stil aktarımı (Faz 18)."""
    ext = extension(filename, VIDEO_EXTS, "Previs rehberi")
    await run_in_threadpool(_ready, scene_id)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "guide" + ext)
        await receive(request, path)

        def store():
            _check_video(path, "Previs rehberi")
            sha = sha_of(path)
            key = f"guides/{scene_id}/{sha}{ext}"
            if not storage.exists(key):
                storage.put_file(path, key)
            return _apply_method(scene_id, "previs_guide", guide_key=key, guide_hash=sha)
        return await run_in_threadpool(store)


@router.post("/scenes/{scene_id}/method")
@curated("method_change")
def set_method(scene_id: SceneId, body: MethodIn, s: Session = Depends(get_session)):
    """Yöntem seçimi. Dış çekim ve previs için önce dosyası yüklenmiş olmalı. Üretime dönüş, önceki klipleri yeniden
    kullanır (dış çekime geçerken havuz silinmez)."""
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    if body.method == "external" and not sc.external_key:
        raise HTTPException(422, f"{sc.scene_code}: önce dış çekim videosunu yükle.")
    if body.method == "previs_guide" and not sc.guide_key:
        raise HTTPException(422, f"{sc.scene_code}: önce previs rehber videosunu yükle.")
    s.close()
    return _apply_method(scene_id, body.method)


# ---------- elle düzeltme ----------

def _variation(s: Session, variation_id: str) -> Variation:
    v = s.get(Variation, variation_id)
    if v is None or v.scene_id is None:
        raise HTTPException(404, "Varyasyon bulunamadı")
    return v


@router.get("/variations/{variation_id}/revisions")
def list_revisions(variation_id: VariationId, s: Session = Depends(get_session)):
    v = _variation(s, variation_id)
    rows = s.scalars(select(Revision).where(Revision.variation_id == v.id).order_by(Revision.version.desc())).all()
    return {"variation_id": variation_id, "active": str(v.revision_id) if v.revision_id else None,
            "rank": v.refined_rank, "original_url": f"/api/media?key={v.video_key}" if v.video_key else None,
            "revisions": [methods.revision_out(r) for r in rows]}


@router.post("/variations/{variation_id}/revisions", status_code=201)
@curated("revision_upload")
async def upload_revision(variation_id: VariationId, request: Request,
                          kind: str = Query("video", pattern="^(video|first_frame)$"),
                          filename: str = Query(..., min_length=1, max_length=200),
                          note: str | None = Query(None, max_length=2000)):
    """Elle düzeltilmiş video ya da ilk kare (ham gövde). Yeni sürüm açılır, orijinal korunur; varyasyon rafine #1 ise
    yalnızca dudak senkronu ve montaj yeniden çalışır. Ücretli modda ilk kare, varyasyonun prompt ve seed'iyle videoya
    çevrilir (bedeli bir video üretimidir; Flux çağrılmaz)."""
    ext = extension(filename, VIDEO_EXTS if kind == "video" else IMAGE_EXTS,
                    "Video düzeltmesi" if kind == "video" else "İlk kare düzeltmesi")

    def ready():
        with session_scope() as s:
            v = _variation(s, variation_id)
            if not v.video_key:
                raise HTTPException(409, "Bu varyasyonun videosu yok (elenmiş ya da üretilememiş); düzeltilemez.")
            return str(v.scene_id)
    scene_id = await run_in_threadpool(ready)
    await run_in_threadpool(_ready, scene_id)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "revision" + ext)
        await receive(request, path)

        def store():
            if kind == "video" and "video" not in streams(path):
                raise HTTPException(422, "Video düzeltmesi: dosyada görüntü akışı yok.")
            if kind == "first_frame":
                from PIL import Image
                try:
                    with Image.open(path) as im:
                        im.verify()
                except Exception:
                    raise HTTPException(422, "İlk kare okunamadı: dosya bozuk ya da görsel değil.")
            try:
                with paid_errors():                    # ücretli modda ilk kare düzeltmesi videoya çevrilir
                    rev, winner = methods.add_revision(variation_id, kind, path, ext, sha_of(path),
                                                       (note or "").strip() or None)
            except ValueError as e:
                raise HTTPException(422, str(e))
            return _after_revision(scene_id, rev, winner)
        return await run_in_threadpool(store)


def _after_revision(scene_id: str, rev: dict | None, winner: bool) -> dict:
    if not winner:
        return {"revision": rev, "stages": [], "hint": "Bu varyasyon rafine #1 değil; 'Bunu kullan' ile seçilince geçerli olur."}
    with session_scope() as s:
        return {"revision": rev, **dispatch_result(s, s.get(Scene, scene_id), post_only=True)}


@router.post("/variations/{variation_id}/revisions/active")
@curated("revision_activate")
def set_active_revision(variation_id: VariationId, body: RevisionActiveIn, s: Session = Depends(get_session)):
    """Etkin revizyonu seçer; revision_id null ise orijinale döner."""
    v = _variation(s, variation_id)
    if body.revision_id is not None:
        rev = s.get(Revision, body.revision_id)
        if rev is None or rev.variation_id != v.id:
            raise HTTPException(404, "Bu varyasyonun böyle bir revizyonu yok")
    sc = s.get(Scene, v.scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    scene_id = str(v.scene_id)
    s.close()
    rev, winner = methods.activate_revision(variation_id, str(body.revision_id) if body.revision_id else None)
    return _after_revision(scene_id, rev, winner)
