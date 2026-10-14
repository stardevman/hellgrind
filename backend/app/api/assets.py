"""Varlık kütüphanesi (Faz 12): karakter, mekân ve nesne referansları; onay, hareket testi ve kilit.

Durum akışı: draft -> still_approved (durağan referans onaylandı) -> motion_tested (hareket testi onaylandı) -> locked.
Görsel bir referans değişince sürüm artar ve varlık taslağa döner: onaylar yeni referans için yeniden verilir.
Kilitli varlık değiştirilemez; önce kilidi açılır.
"""
import datetime as dt
import os
import tempfile
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import AssetId, ProjectId, SceneId, ensure_not_frozen, paid_errors, project_or_404, scene_or_404
from app.budget import killswitch
from app.config import project_settings, settings
from app.core import series
from app.core.h3mode import suggest_kind
from app.curation import curated
from app.db import get_session, session_scope
from app.engine import motion_test, references
from app.hermes import lint
from app.hermes.ingest import AssetBook, ensure_character
from app.hermes.rules import tr_title
from app.models import Asset, Character, EpisodeAsset, Project, Scene, SceneAsset, SceneCharacter, Variation
from app.schemas import AssetIn, AssetPatch, SceneAssetIn
from app.style import bible

router = APIRouter(tags=["assets"])

STALE_TEST = dt.timedelta(minutes=10)      # bu kadar süredir "sırada" duran test, çöken bir worker'dan kalmıştır
def _media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


def normalize_name(name: str) -> str:
    """Hermes ile aynı biçim ("taksi" ve "TAKSİ" -> "Taksi"): aynı varlık iki kez oluşmasın."""
    clean = tr_title(" ".join((name or "").split()))
    if not clean:
        raise HTTPException(422, "Varlık adı boş olamaz")
    return clean


def asset_or_404(s: Session, asset_id: str, lock: bool = False) -> Asset:
    q = select(Asset).where(Asset.id == asset_id)
    a = s.scalar(q.with_for_update() if lock else q)
    if a is None:
        raise HTTPException(404, "Varlık bulunamadı")
    return a


def ensure_episode_open(s: Session, a: Asset) -> None:
    """Bölüme özel varlık teslim edilmiş (sürüm sabit) bölümde değişmez; dizi varlığı değişebilir: kilitli bölüm sabit
    sürümde kalır."""
    if a.project_id is not None:
        series.ensure_unlocked(s, a.project_id)


def context_project(s: Session, a: Asset, project_id: str | None = None) -> str:
    """Varlığın bütçe, ayar ve stil bağlamı: bölüme özel varlıkta kendi bölümü; dizi varlığında panelin açık olduğu
    bölüm (o dizinin bir bölümü olmalı), verilmemişse varlığın en son bağlandığı bölüm."""
    if a.project_id is not None:
        return str(a.project_id)
    if project_id:
        q = series.series_of(s, project_id)
        if q is None or q.id != a.series_id:
            raise HTTPException(422, "Bu bölüm varlığın dizisine ait değil")
        return project_id
    last = s.scalar(select(EpisodeAsset.project_id).where(EpisodeAsset.asset_id == a.id)
                    .order_by(EpisodeAsset.added_at.desc()).limit(1))
    if last is None:
        raise HTTPException(409, f"{a.name} dizi kütüphanesinde ama hiçbir bölüme bağlı değil; önce bir bölüme bağlayın.")
    return str(last)


def event_projects(s: Session, a: Asset) -> list[str]:
    """Varlık olayının yazılacağı bölümler: bölüme özel varlıkta kendi bölümü, dizi varlığında bağlı bütün bölümler."""
    if a.project_id is not None:
        return [str(a.project_id)]
    return [str(p) for p in s.scalars(select(EpisodeAsset.project_id).where(EpisodeAsset.asset_id == a.id))]


def embedding_of(s: Session, a: Asset) -> int | None:
    ch = s.scalar(select(Character).where(Character.asset_id == a.id))
    return ch.embedding_version if ch else None


def ensure_editable(a: Asset) -> None:
    if a.status == "locked":
        raise HTTPException(409, f"{a.name} kilitli; değiştirmek için önce kilidi aç.")


# ---------- çıktı ----------

def _reference_out(entry: dict) -> dict:
    out = {k: v for k, v in entry.items() if k != "vector"}
    out["url"], out["thumb_url"] = _media(entry.get("key")), _media(entry.get("thumb"))
    out["vector_dims"] = len(entry.get("vector") or [])
    if entry.get("frames"):
        out["frame_urls"] = [_media(k) for k in entry["frames"]]
    return out


def _test_out(v: Variation | None, version: int) -> dict | None:
    if v is None:
        return None
    return {"id": str(v.id), "status": v.status, "seed": v.seed, "cost_usd": float(v.cost_usd),
            "anomalies": sorted({a["type"] for a in (v.anomalies or [])}),
            "current": (v.params or {}).get("asset_version") == version,
            "video_url": _media(v.video_key), "thumb_url": _media(v.keyframe_key)}


def assets_out(s: Session, assets: list[Asset], project_id=None) -> list[dict]:
    """Varlık listesi; sorgu sayısı varlık sayısından bağımsızdır. project_id: stilin geldiği bölüm (dizi varlıkları
    bölümün stil kılavuzuyla gösterilir)."""
    ids = [a.id for a in assets]
    owner = project_id or next((a.project_id for a in assets if a.project_id), None)
    style = bible.active(s, owner) if owner else None
    tests, chars, used = {}, {}, defaultdict(list)
    if ids:
        test_ids = [a.motion_test_variation_id for a in assets if a.motion_test_variation_id]
        if test_ids:
            tests = {v.id: v for v in s.scalars(select(Variation).where(Variation.id.in_(test_ids)))}
        chars = {c.asset_id: c for c in s.scalars(select(Character).where(Character.asset_id.in_(ids)))}
        here = (Scene.project_id == owner) if owner else true()       # dizi varlığı: yalnızca bu bölümün çekimleri
        for aid, code in s.execute(select(SceneAsset.asset_id, Scene.scene_code)
                                   .join(Scene, Scene.id == SceneAsset.scene_id)
                                   .where(SceneAsset.asset_id.in_(ids), here).order_by(Scene.seq_order)):
            used[aid].append(code)
        for aid, code in s.execute(select(Character.asset_id, Scene.scene_code)
                                   .join(SceneCharacter, SceneCharacter.character_id == Character.id)
                                   .join(Scene, Scene.id == SceneCharacter.scene_id)
                                   .where(Character.asset_id.in_(ids), here).order_by(Scene.seq_order)):
            used[aid].append(code)
    out = []
    for a in assets:
        ch = chars.get(a.id)
        out.append({
            "id": str(a.id), "project_id": str(a.project_id) if a.project_id else None,
            "series_id": str(a.series_id) if a.series_id else None, "type": a.type, "name": a.name,
            "description": a.description, "render_style": a.render_style, "status": a.status, "version": a.version,
            "invariants": list(a.invariants or []), "scale": a.scale,
            "effective_style": bible.effective_style(a, style),       # render_style boşsa kılavuzun tür kuralı
            "references": {k: _reference_out(v) for k, v in (a.references or {}).items()},
            "motion_test": _test_out(tests.get(a.motion_test_variation_id), a.version),
            "scenes": used[a.id],
            "character": ({"id": str(ch.id), "voice_id": ch.voice_id, "embedding_version": ch.embedding_version,
                           "kind": ch.kind or "human", "kind_suggested": suggest_kind(a.name, a.description)}
                          if ch else None),
            "reference_history": [{**h, "entry": _reference_out(h["entry"])} for h in (a.reference_history or [])],
        })
    return out


def asset_out(s: Session, a: Asset, project_id=None) -> dict:
    return assets_out(s, [a], project_id)[0]


def _changed(s: Session, a: Asset, action: str) -> dict:
    s.commit()
    for pid in event_projects(s, a):
        killswitch.event(pid, "asset_status", asset_id=str(a.id), name=a.name, type=a.type, status=a.status,
                         action=action)
    return asset_out(s, a)


# ---------- liste ve oluşturma ----------

@router.get("/projects/{project_id}/assets")
def list_assets(project_id: ProjectId, type: str | None = None, status: str | None = None,
                s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    q = series.episode_assets_q(project_id)              # bölüme özel varlıklar ve bağlı dizi varlıkları
    if type:
        q = q.where(Asset.type == type)
    if status:
        q = q.where(Asset.status == status)
    order = {"character": 0, "location": 1, "prop": 2}
    rows = sorted(s.scalars(q), key=lambda a: (order[a.type], a.name))
    return assets_out(s, rows, project_id)


@router.post("/projects/{project_id}/assets", status_code=201)
@curated("asset_create")
def create_asset(project_id: ProjectId, body: AssetIn, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    name = normalize_name(body.name)
    if s.scalar(series.episode_assets_q(project_id).where(Asset.type == body.type, Asset.name == name)
                .with_only_columns(Asset.id)):
        raise HTTPException(409, f"Bu projede '{name}' adlı bir varlık zaten var.")
    book = AssetBook(s, project_id)
    book.library = {}                  # "Bu bölüm": bölüme özel varlık; dizi kütüphanesine Varlıklar › Dizi kütüphanesi
    if body.type == "character":       # karakter varlığı, ses alanlarını tutan karakter satırıyla birlikte
        chars = {c.name: c for c in s.scalars(series.episode_characters_q(project_id))}
        ensure_character(s, book, project_id, name, chars, description=body.description)
    a = book.get(body.type, name, body.description)
    if body.render_style:
        a.render_style = body.render_style
    s.flush()
    return _changed(s, a, "create")


@router.get("/assets/{asset_id}")
def get_asset(asset_id: AssetId, s: Session = Depends(get_session)):
    return asset_out(s, asset_or_404(s, asset_id))


@router.patch("/assets/{asset_id}")
@curated("asset_edit")
def patch_asset(asset_id: AssetId, body: AssetPatch, s: Session = Depends(get_session)):
    """Tarif ya da stil değişince sürüm artar ve hareket testi yeniden gerekir; ses kimliği görüntüyü etkilemez."""
    a = asset_or_404(s, asset_id, lock=True)
    ensure_episode_open(s, a)
    old_version, old_embedding = a.version, embedding_of(s, a)
    changes = body.model_dump(exclude_unset=True)
    if "scale" in changes:                              # boş: ölçek ifadesi kaldırılır
        changes["scale"] = " ".join((changes["scale"] or "").split()) or None
    if "render_style" in changes:                       # boş: istisnayı kaldır, kılavuzun tür kuralına dön
        changes["render_style"] = " ".join((changes["render_style"] or "").split()) or None
    visual = {k: v for k, v in changes.items()
              if k in ("description", "render_style", "invariants", "scale") and v != getattr(a, k)
              and not (k == "invariants" and v is None)}
    if visual:
        # Değişmezler tasarımı değiştirmez, korur (her istemde aynen; negatife zıtları): kilitli varlıkta da yazılır,
        # hareket testi geçerli kalır; sürüm artar (çekimler yeniden üretilince uygulanır).
        guard_only = set(visual) <= {"invariants", "scale"}     # ölçek de tasarımı değiştirmez, korur
        if not guard_only:
            ensure_editable(a)
        for key, value in visual.items():
            setattr(a, key, value)
        a.version += 1
        series.on_version_bump(s, a, old_version, old_embedding)     # dizi varlığı: üretilmiş bölümler eski sürümde kalır
        if not guard_only:
            a.motion_test_variation_id = None
            if a.status == "motion_tested":
                a.status = "still_approved"
    if "voice_id" in changes:
        ch = s.scalar(select(Character).where(Character.asset_id == a.id))
        if ch is None:
            raise HTTPException(422, "Ses kimliği yalnızca karakter varlıklarında tutulur")
        ch.voice_id = changes["voice_id"]
    if changes.get("character_kind"):                   # kimlik kontrolünün türü; görüntüyü değiştirmez (sürüm artmaz)
        ch = s.scalar(select(Character).where(Character.asset_id == a.id))
        if ch is None:
            raise HTTPException(422, "Karakter türü yalnızca karakter varlıklarında seçilir")
        ch.kind = changes["character_kind"]
    return _changed(s, a, "edit")


# ---------- referanslar ----------

def _precheck(asset_id: str, kind: str) -> None:
    with session_scope() as s:
        a = asset_or_404(s, asset_id)
        if kind not in references.STATE_REF_KINDS:      # durum referansı kilitli varlığa da eklenir
            ensure_editable(a)
        if references.REFERENCE_KINDS[kind][2] and a.type != "character":
            raise HTTPException(422, f"{references.REFERENCE_KINDS[kind][1]} yalnızca karakterler içindir")


def attach_reference(asset_id: str, kind: str, path: str, filename: str, content_type: str | None) -> dict:
    """Dosyayı işler ve varlığa bağlar. Görsel referans değişince sürüm artar, varlık taslağa döner, hareket testi
    düşer; karakterde `reference_images` yenilenir; dönüş videosu vektörleri, durağan görsel de ücretli karenin kaynağını (Kontext)
    değiştirdiği için embedding_version artar ve çekim yeniden üretilir.
    Aynı dosyayı yeniden yüklemek hiçbir şeyi değiştirmez."""
    try:
        entry = references.process(asset_id, kind, path, filename, content_type)
    except references.InvalidReference as e:
        raise HTTPException(422, str(e))
    with session_scope() as s:
        a = asset_or_404(s, asset_id, lock=True)
        if kind not in references.STATE_REF_KINDS:
            ensure_editable(a)
        ensure_episode_open(s, a)
        old_version, old_embedding = a.version, embedding_of(s, a)
        refs = dict(a.references or {})
        same = (refs.get(kind) or {}).get("sha") == entry["sha"]
        if not same:
            if refs.get(kind):                          # eski referans silinmez: sürüm geçmişine
                a.reference_history = [*(a.reference_history or []),
                                       {"kind": kind, "version": a.version, "replaced_at": time.time(), "entry": refs[kind]}]
            refs[kind] = entry
            a.references = refs
            if kind in references.VISUAL_KINDS:
                a.version += 1
                a.status, a.motion_test_variation_id = "draft", None
                ch = s.scalar(select(Character).where(Character.asset_id == a.id))
                if ch is not None:
                    ch.reference_images = references.visual_keys(refs)
                    if kind in ("turnaround", "still"):     # dönüş videosu vektörleri; durağan görsel ücretli karenin kaynağı
                        ch.embedding_version += 1
                series.on_version_bump(s, a, old_version, old_embedding)
        out = _changed(s, a, f"reference:{kind}") if not same else asset_out(s, a)
    return out


@router.post("/assets/{asset_id}/references", status_code=201)
@curated("asset_reference")
async def upload_reference(asset_id: AssetId, request: Request, kind: str = Query(...),
                           filename: str = Query(..., min_length=1, max_length=200)):
    """Referans dosyasını istek gövdesi olarak alır (multipart değil): `?kind=turnaround&filename=kerem.mp4`.
    Türler: still, turnaround (360° dönüş videosu), expressions (ifade sayfası), voice_sample (ses örneği)."""
    if kind not in references.REFERENCE_KINDS:
        raise HTTPException(422, f"Bilinmeyen referans türü: {kind} (geçerli: {', '.join(references.REFERENCE_KINDS)})")
    await run_in_threadpool(_precheck, asset_id, kind)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "upload" + os.path.splitext(filename)[1].lower())
        size = 0
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > references.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Dosya çok büyük (en çok 200 MB)")
                f.write(chunk)
        if size == 0:
            raise HTTPException(422, "Boş dosya")
        return await run_in_threadpool(attach_reference, asset_id, kind, path, filename,
                                       request.headers.get("content-type"))


@router.post("/assets/{asset_id}/references/placeholder", status_code=201)
@curated("asset_reference")
def placeholder_reference(asset_id: AssetId, kind: str | None = Query(None), project_id: str | None = Query(None),
                          s: Session = Depends(get_session)):
    """Referans üretir. kind: still (durağan görsel) | turnaround (karakterin dönüş videosu; isteğe bağlı ikinci adım).
    Ücretli modda varsayılan durağan görseldir: RunPod'da Qwen-Image-2512 (karakter 1328x1328, düz arka plan, tam boy,
    yüz net; mekân 1664x928). Dönüş videosu MiniMax H3'ten, durağan görselden başlar. Ücretsiz modda prosedürel taslaktır
    (karakterde varsayılan dönüş videosu) ve ücretli karşılığının sanal fiyatı bütçeden düşer."""
    a = asset_or_404(s, asset_id)
    ensure_editable(a)
    if kind is not None and kind not in references.reference_kinds(a.type):
        raise HTTPException(422, f"{a.name} için üretilebilen referanslar: {', '.join(references.reference_kinds(a.type))}")
    pid = context_project(s, a, project_id)                # dizi varlığı: bedel panelin açık olduğu bölüme
    ensure_not_frozen(pid)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        if settings.dry_run:
            local = kind or ("turnaround" if a.type == "character" else "still")
            label, estimate = references.reference_estimate(a.type, local)
            try:
                with killswitch.spend(pid, estimate, label, asset_id=a.id) as charge:
                    kind, path, filename = references.placeholder(a, tmp, local)
                    charge.actual = estimate
            except killswitch.ProjectFrozen:
                raise HTTPException(423, "Bütçe limiti doldu ya da proje donduruldu")
        else:
            ch = s.scalar(select(Character).where(Character.asset_id == a.id))
            lora = {"loras": [{"path": ch.lora_url, "scale": ch.lora_scale}], "lora_triggers": [ch.lora_trigger]} \
                if ch is not None and ch.lora_url else {}
            with paid_errors():
                kind, path, filename = references.paid_reference(pid, a, bible.active(s, pid), lora, tmp, kind or "still")
        s.close()
        content_type = "video/mp4" if kind == "turnaround" else ("image/jpeg" if filename.endswith(".jpg") else "image/png")
        return attach_reference(asset_id, kind, path, filename, content_type)


# ---------- onaylar ----------

@router.post("/assets/{asset_id}/approve-still")
@curated("asset_approve_still")
def approve_still(asset_id: AssetId, s: Session = Depends(get_session)):
    a = asset_or_404(s, asset_id, lock=True)
    if a.status != "draft":
        return asset_out(s, a)                          # zaten onaylı: değişiklik yok
    if not any((a.references or {}).get(k) for k in references.VISUAL_KINDS):
        raise HTTPException(409, f"{a.name} için önce bir görsel referans yükle (durağan görsel ya da dönüş videosu).")
    a.status = "still_approved"
    return _changed(s, a, "approve_still")


def motion_tests_enabled() -> bool:
    """Hareket testi yalnızca ücretli modda üretilir (ya da yerel test açıkça açılmışsa)."""
    return not settings.dry_run or settings.local_motion_test


@router.post("/assets/{asset_id}/motion-test", status_code=202)
@curated("motion_test")
def start_motion_test(asset_id: AssetId, project_id: str | None = Query(None), s: Session = Depends(get_session)):
    """Kısa, düşük çözünürlüklü hareket testi (worker'da). Onaylanmış bir test varsa yenisi onu geçersiz kılar."""
    a = asset_or_404(s, asset_id, lock=True)
    pid = context_project(s, a, project_id)
    ensure_not_frozen(pid)
    ensure_editable(a)
    if a.status == "draft":
        raise HTTPException(409, f"{a.name}: önce durağan referansı onayla.")
    if not motion_tests_enabled():
        raise HTTPException(409, f"{a.name}: ücretsiz modda hareket testi üretilmez (yerel çizim referansı kullanmaz); "
                                 "durağan onaylı varlığı doğrudan kilitleyebilirsin.")
    current = s.get(Variation, a.motion_test_variation_id) if a.motion_test_variation_id else None
    if (current is not None and current.status in ("queued", "running")
            and current.created_at > dt.datetime.now(dt.timezone.utc) - STALE_TEST):
        raise HTTPException(409, f"{a.name} için hareket testi zaten sürüyor.")
    vid = str(motion_test.create(s, a, project_settings(s.get(Project, pid).settings), pid).id)
    a.status = "still_approved"                         # yeni test yönetmen onayını bekler
    _changed(s, a, "motion_test")
    from app.worker.tasks import motion_test as task    # tasks -> celery_app döngüsünü önlemek için geç import
    try:
        task.delay(pid, vid)
    except Exception:                                   # kuyruğa verilemedi: test "sürüyor" görünüp kalmasın
        with session_scope() as s2:
            s2.get(Variation, vid).status = "failed"
        raise
    s.expire_all()
    return {"variation_id": vid, "asset": asset_out(s, asset_or_404(s, asset_id))}


@router.post("/assets/{asset_id}/approve-motion")
@curated("asset_approve_motion")
def approve_motion(asset_id: AssetId, s: Session = Depends(get_session)):
    a = asset_or_404(s, asset_id, lock=True)
    if a.status in ("motion_tested", "locked"):
        return asset_out(s, a)
    if a.status == "draft":
        raise HTTPException(409, f"{a.name}: önce durağan referansı onayla.")
    v = s.get(Variation, a.motion_test_variation_id) if a.motion_test_variation_id else None
    if v is None:
        raise HTTPException(409, f"{a.name}: önce hareket testi üret.")
    if (v.params or {}).get("asset_version") != a.version:
        raise HTTPException(409, f"{a.name}: hareket testi eski bir referansa ait; yeniden üret.")
    if v.status in ("queued", "running"):
        raise HTTPException(409, f"{a.name}: hareket testi henüz bitmedi.")
    if v.status != "passed":
        found = ", ".join(sorted({x["type"] for x in (v.anomalies or [])})) or "üretilemedi"
        raise HTTPException(409, f"{a.name}: hareket testinde kusur var ({found}); yeniden üret.")
    a.status = "motion_tested"
    return _changed(s, a, "approve_motion")


@router.post("/assets/{asset_id}/lock")
@curated("asset_lock")
def lock_asset(asset_id: AssetId, project_id: str | None = Query(None), s: Session = Depends(get_session)):
    """Varlığı kilitler. Engeli kalkan sahneler bildirilir; üretim, sahne yeniden render edilince başlar."""
    a = asset_or_404(s, asset_id, lock=True)
    if a.status != "locked":
        if a.status != "motion_tested" and not (a.status == "still_approved" and not motion_tests_enabled()):
            raise HTTPException(409, f"{a.name}: hareket testi onaylanmadan varlık kilitlenemez.")
        a.status = "locked"
        _changed(s, a, "lock")
    pid = context_project(s, a, project_id)
    cfg = project_settings(s.get(Project, pid).settings)
    blocked = s.scalars(select(Scene).where(Scene.project_id == pid, Scene.status == "blocked")
                        .order_by(Scene.seq_order)).all()
    unblocked = [sc.scene_code for sc in blocked if lint.check_scene(s, sc.id, cfg).ok]
    return {**asset_out(s, a), "unblocked": unblocked}


@router.post("/assets/{asset_id}/unlock")
@curated("asset_unlock")
def unlock_asset(asset_id: AssetId, s: Session = Depends(get_session)):
    a = asset_or_404(s, asset_id, lock=True)
    if a.status != "locked":
        return asset_out(s, a)
    a.status = "motion_tested" if a.motion_test_variation_id else "still_approved"     # testsiz kilit: durağan onaya döner
    return _changed(s, a, "unlock")


# ---------- sahne bağları ----------

@router.post("/scenes/{scene_id}/assets", status_code=201)
@curated("asset_link")
def link_asset(scene_id: SceneId, body: SceneAssetIn, s: Session = Depends(get_session)):
    """Sahneye mekân ya da nesne bağlar (panelden). Görsel özet değişir: sahne yeniden üretilince geçerli olur."""
    sc = scene_or_404(s, scene_id)
    if body.asset_id:
        a = s.get(Asset, body.asset_id)
        if a is None or not s.scalar(series.episode_assets_q(sc.project_id).where(Asset.id == a.id)
                                     .with_only_columns(Asset.id)):
            raise HTTPException(404, "Bu projede böyle bir varlık yok")
        if a.type == "character":
            raise HTTPException(422, "Karakterler sahneye senaryodaki kadrodan bağlanır")
    else:
        a = AssetBook(s, str(sc.project_id)).get(body.type, normalize_name(body.name))
        s.flush()
    s.execute(insert(SceneAsset).values(scene_id=sc.id, asset_id=a.id, role=a.type, source="manual")
              .on_conflict_do_update(index_elements=["scene_id", "asset_id"], set_={"source": "manual"}))
    s.commit()
    return {"scene_id": scene_id, "asset": asset_out(s, a), "hint": "Değişikliği uygulamak için sahneyi yeniden üret."}


@router.delete("/scenes/{scene_id}/assets/{asset_id}")
@curated("asset_unlink")
def unlink_asset(scene_id: SceneId, asset_id: AssetId, s: Session = Depends(get_session)):
    """Bağı kaldırır. Senaryodan gelen bir bağ, senaryo yeniden yüklenince geri gelir."""
    scene_or_404(s, scene_id)
    link = s.get(SceneAsset, (scene_id, asset_id))
    if link is None:
        raise HTTPException(404, "Bu sahnede böyle bir varlık bağı yok")
    s.delete(link)
    s.commit()
    return {"ok": True, "hint": "Değişikliği uygulamak için sahneyi yeniden üret."}
