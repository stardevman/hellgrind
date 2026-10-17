"""Görsel stüdyosu uçları: görsel üretimi (metinden ya da kaynaklardan), galeri, seçme/gizleme ve varlığa referans atama.

Üretim worker'da (gen kuyruğu) çalışır; panel galeriyi yoklar. Atama, Varlıklar'daki referans yüklemesiyle aynı yoldan
geçer: sürüm artar, varlık taslağa döner ve onay akışı (durağan onayı, hareket testi, kilit) yeniden işler.
"""
import datetime as dt
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.api import assets as assets_api
from app.api.deps import ImageId, ProjectId, ensure_not_frozen, project_or_404
from app.config import settings
from app.core import series
from app.core.hashing import seed_for
from app.curation import curated
from app.db import get_session, session_scope
from app.engine import references, studio
from app.models import Asset, StudioImage
from app.schemas import StudioAssignIn, StudioGenerateIn, StudioPatch
from app.style import bible

router = APIRouter(tags=["images"])

LIST_LIMIT = 200


def _media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


def image_out(row: StudioImage) -> dict:
    return {
        "id": str(row.id), "project_id": str(row.project_id), "batch_id": str(row.batch_id), "prompt": row.prompt,
        "composed_prompt": row.composed_prompt, "negative": row.negative, "mode": row.mode, "aspect": row.aspect,
        "kind": row.kind, "width": row.width, "height": row.height, "seed": row.seed,
        "sources": [{"label": x.get("label"), "thumb_url": _media(x.get("thumb") or x.get("key")),
                     "image_id": x.get("image_id"), "asset_id": x.get("asset_id"), "kind": x.get("kind")}
                    for x in row.sources or []],
        "target_asset_id": str(row.target_asset_id) if row.target_asset_id else None, "apply_style": row.apply_style,
        "status": row.status, "url": _media(row.file_key), "thumb_url": _media(row.thumb_key),
        "cost_usd": float(row.cost_usd or 0), "engine": row.engine, "error": row.error, "favorite": row.favorite,
        "hidden": row.hidden, "assignments": list(row.assignments or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def image_or_404(s: Session, image_id: str, lock: bool = False) -> StudioImage:
    q = select(StudioImage).where(StudioImage.id == image_id)
    row = s.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise HTTPException(404, "Görsel bulunamadı")
    return row


def project_asset(s: Session, project_id: str, asset_id) -> Asset:
    a = s.get(Asset, asset_id)
    if a is None or not s.scalar(series.episode_assets_q(project_id).where(Asset.id == a.id).with_only_columns(Asset.id)):
        raise HTTPException(404, "Bu projede böyle bir varlık yok")
    return a


def readiness() -> tuple[bool, str | None]:
    """Ücretli modda fal.ai anahtarı gerekir; ücretsiz modda yerel taslak her zaman çizilir."""
    if settings.dry_run:
        return True, None
    from app.engine import paid
    if not paid.available("fal_key"):
        return False, "Görsel üretimi için fal.ai anahtarı gerekli (Ayarlar › Ücretli servisler)."
    return True, None


@router.get("/images/options")
def options():
    ready, why = readiness()
    return {
        "aspects": [{"id": k, "label": v[0], "width": v[1], "height": v[2]} for k, v in studio.ASPECTS.items()],
        "estimates": {m: {k: studio.estimate(m, k) for k in studio.ASPECTS} for m in ("text", "edit")},
        "max_count": studio.MAX_COUNT, "max_sources": studio.MAX_SOURCES, "engine": studio.engine_now(),
        "models": {"text": settings.fal_qwen_image_endpoint, "edit": settings.fal_qwen_edit_endpoint},
        "ready": ready, "reason": why,
    }


@router.get("/projects/{project_id}/images")
def list_images(project_id: ProjectId, hidden: bool = False, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    stale = dt.datetime.now(dt.timezone.utc) - studio.STALE
    for row in s.scalars(select(StudioImage).where(StudioImage.project_id == project_id,
                                                   StudioImage.status.in_(("queued", "running")),
                                                   StudioImage.created_at < stale)):
        row.status, row.error = "failed", "Worker yanıt vermedi (iş yarıda kaldı); yeniden üret."
    s.commit()
    q = select(StudioImage).where(StudioImage.project_id == project_id)
    if not hidden:
        q = q.where(StudioImage.hidden.is_(False))
    rows = s.scalars(q.order_by(StudioImage.created_at.desc(), StudioImage.seed).limit(LIST_LIMIT)).all()
    return [image_out(r) for r in rows]


def _sources(s: Session, project_id: str, items) -> list[dict]:
    out = []
    for x in items:
        if x.image_id:
            src = s.get(StudioImage, x.image_id)
            if src is None or str(src.project_id) != project_id or src.status != "done" or not src.file_key:
                raise HTTPException(422, "Kaynak görsel bu projede yok ya da henüz üretilmedi")
            out.append({"key": src.file_key, "thumb": src.thumb_key, "image_id": str(src.id),
                        "label": f"Görsel {str(src.id)[:4]}"})
            continue
        a = project_asset(s, project_id, x.asset_id)
        ref = (a.references or {}).get(x.kind) or {}
        key = (ref.get("frames") or [None])[0] if x.kind == "turnaround" else ref.get("key")
        if not key:
            raise HTTPException(422, f"{a.name}: {references.REFERENCE_KINDS[x.kind][1].lower()} yok")
        out.append({"key": key, "thumb": ref.get("thumb") or key, "asset_id": str(a.id), "kind": x.kind,
                    "label": f"{a.name} · {references.REFERENCE_KINDS[x.kind][1]}"})
    return out


@router.post("/projects/{project_id}/images", status_code=202)
@curated("image_generate")
def generate_images(project_id: ProjectId, body: StudioGenerateIn, s: Session = Depends(get_session)):
    """Görselleri kuyruğa alır (her biri ayrı seed). Bütçe yetmiyorsa ya da proje donmuşsa hiçbir şey başlamaz."""
    p = project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    ready, why = readiness()
    if not ready:
        raise HTTPException(409, why)
    asset = project_asset(s, project_id, body.asset_id) if body.asset_id else None
    if not body.prompt.strip() and asset is None:
        raise HTTPException(422, "Ne çizileceğini yaz ya da bir varlık seç.")
    if body.mode == "edit" and not body.sources:
        raise HTTPException(422, "Kaynaktan düzenleme için en az bir kaynak görsel seç.")
    sources = _sources(s, project_id, body.sources) if body.mode == "edit" else []
    per = studio.estimate(body.mode, body.aspect)
    left = float(p.budget_limit_usd) - float(p.spent_usd) - float(p.reserved_usd)
    if per * body.count > left + 1e-9:
        raise HTTPException(409, f"Bütçe yetmez: {body.count} görsel ≈ {per * body.count:.2f} USD, kalan "
                                 f"{max(left, 0):.2f} USD.")
    style = bible.active(s, project_id) if body.apply_style else None
    try:
        composed = studio.compose(body.prompt, body.kind, asset, style)
    except Exception as e:                              # çeviri modeli yok gibi: istem kurulamadı, hiçbir şey başlamaz
        from app.hermes.translate import TranslationUnavailable
        if isinstance(e, TranslationUnavailable):
            raise HTTPException(409, str(e))
        raise
    _, w, h = studio.ASPECTS[body.aspect]
    batch = uuid.uuid4()
    rows = []
    for i in range(body.count):
        seed = (body.seed + i) % 2**31 if body.seed is not None else seed_for(project_id, str(batch), i)
        fields = {"composed_prompt": composed.prompt, "negative": composed.negative, "mode": body.mode, "width": w,
                  "height": h, "seed": seed, "source_keys": [x["key"] for x in sources], "engine": studio.engine_now()}
        row = StudioImage(project_id=p.id, batch_id=batch, prompt=body.prompt.strip(), composed_prompt=composed.prompt,
                          negative=composed.negative, mode=body.mode, aspect=body.aspect,
                          kind=asset.type if asset else body.kind, width=w, height=h, seed=seed, sources=sources,
                          target_asset_id=asset.id if asset else None, apply_style=body.apply_style,
                          input_hash=studio.input_hash(fields), status="queued")
        s.add(row)
        rows.append(row)
    s.commit()
    from app.worker.tasks import studio_image as task   # tasks -> celery_app döngüsünü önlemek için geç import
    for row in rows:
        try:
            task.delay(project_id, str(row.id))
        except Exception:                               # kuyruğa verilemedi: "sırada" görünüp kalmasın
            with session_scope() as s2:
                for r in rows:
                    x = s2.get(StudioImage, r.id)
                    if x.status == "queued":
                        x.status, x.error = "failed", "Kuyruğa verilemedi (Redis/worker)."
            raise
    s.expire_all()
    return {"batch_id": str(batch), "estimate_usd": round(per * body.count, 4),
            "images": [image_out(s.get(StudioImage, r.id)) for r in rows]}


def _patch_action(kw, result) -> str | None:
    body = kw.get("body")
    if body is None:
        return None
    if body.favorite is not None:
        return "image_favorite" if body.favorite else "image_unfavorite"
    if body.hidden is not None:
        return "image_hide" if body.hidden else "image_unhide"
    return None


@router.patch("/images/{image_id}")
@curated(_patch_action)
def patch_image(image_id: ImageId, body: StudioPatch, s: Session = Depends(get_session)):
    """Seç (favori) ya da gizle. Gizlenen görsel silinmez; "Gizlenenleri göster" ile geri gelir."""
    row = image_or_404(s, image_id, lock=True)
    if body.favorite is not None:
        row.favorite = body.favorite
    if body.hidden is not None:
        row.hidden = body.hidden
    s.commit()
    return image_out(row)


@router.post("/images/{image_id}/assign")
@curated("image_assign")
def assign_image(image_id: ImageId, body: StudioAssignIn):
    """Görseli bir varlığa referans olarak atar (durağan görsel ya da ifade sayfası). Varlık taslağa döner; eski referans
    referans geçmişinde kalır. Aynı görseli yeniden atamak bir şey değiştirmez."""
    with session_scope() as s:
        row = image_or_404(s, image_id)
        if row.status != "done" or not row.file_key:
            raise HTTPException(409, "Görsel henüz üretilmedi.")
        pid, key = str(row.project_id), row.file_key
        a = project_asset(s, pid, body.asset_id)
        aid, name = str(a.id), a.name
    assets_api._precheck(aid, body.kind)                 # kilitli varlık, ifade sayfası yalnızca karakterde
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = storage.download(key, os.path.join(tmp, "studio.png"))
        asset = assets_api.attach_reference(aid, body.kind, path, f"{name}-{body.kind}.png", "image/png")
    with session_scope() as s:
        row = image_or_404(s, image_id, lock=True)
        row.assignments = [*(row.assignments or []),
                           {"asset_id": aid, "name": name, "kind": body.kind, "version": asset["version"],
                            "at": dt.datetime.now(dt.timezone.utc).isoformat()}]
        s.flush()
        return {**image_out(row), "asset": asset}
