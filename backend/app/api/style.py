"""Stil kılavuzu uç noktaları (Faz 13): sürümler, etki önizlemesi ve etkinleştirme.

Akış: yeni sürüm kaydedilir (etkin değil) -> önizleme hangi çekimlerin yeniden üretileceğini ve sanal maliyeti gösterir ->
yönetmen etkinleştirir (isteğe bağlı olarak etkilenen çekimleri hemen üretir). Onay olmadan hiçbir şey üretilmez.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, ensure_not_frozen, project_or_404
from app.api.plan import dispatch_all
from app.budget import killswitch
from app.curation import curated
from app.db import get_session
from app.engine.fal_plan import MEDIUM_SENTENCE, style_line
from app.models import StyleBible
from app.schemas import StyleActivateIn, StyleIn, StyleVersionIn
from app.style import bible
from app.style.composer import STYLE_TEXT, style_prefix

router = APIRouter(tags=["style"])


def style_out(b: StyleBible) -> dict:
    return {"version": b.version, "is_active": b.is_active, "lighting_prefix": b.lighting_prefix,
            "motion_style": b.motion_style, "rules": {**bible.default_rules(b), **(b.rules or {})}, "negative": b.negative,
            "palette": b.palette, "notes": b.notes, "created_at": b.created_at.timestamp() if b.created_at else None,
            "prefix": style_prefix(b), "medium": bible.medium_of(b), "video_line": style_line(b)}


def version_or_404(s: Session, project_id: str, version: int) -> StyleBible:
    row = bible.get_version(s, project_id, version)
    if row is None:
        raise HTTPException(404, f"Stil kılavuzunun {version}. sürümü yok")
    return row


@router.get("/projects/{project_id}/style")
def get_style(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    rows = bible.versions(s, project_id)
    active = next((r for r in rows if r.is_active), None)
    return {"active": style_out(active) if active else None, "versions": [style_out(r) for r in rows],
            "defaults": bible.DEFAULT_RULES_2D, "render_styles": STYLE_TEXT, "example": bible.EXAMPLE_2D,
            # çizim türüne göre varsayılan kurallar ve örnek (formda 2D / 3D seçilince değişir); yeni sürüm 2D başlar
            "defaults_by_medium": {"2d": bible.DEFAULT_RULES_2D, "3d": bible.DEFAULT_RULES},
            "examples": {"2d": bible.EXAMPLE_2D, "3d": bible.EXAMPLE}, "media": bible.MEDIUM_TR,
            "medium_default": "2d", "video_lines": MEDIUM_SENTENCE}


@router.post("/projects/{project_id}/style", status_code=201)
@curated("style_create")
def create_style(project_id: ProjectId, body: StyleIn, s: Session = Depends(get_session)):
    """Yeni sürüm kaydeder; etkin sürüm değişmez, hiçbir çekim etkilenmez."""
    project_or_404(s, project_id)
    row = bible.create(s, project_id, **body.model_dump())
    s.commit()
    return {**style_out(row), "hint": "Etkisini önizle, sonra etkinleştir."}


@router.post("/projects/{project_id}/style/preview")
def preview_style(project_id: ProjectId, body: StyleVersionIn, s: Session = Depends(get_session)):
    """Sürüm etkinleşirse yeniden üretilecek çekimler ve tahmini sanal maliyet. Hiçbir şey yazmaz ve üretmez."""
    project_or_404(s, project_id)
    version_or_404(s, project_id, body.version)
    return bible.impact(s, project_id, body.version)


@router.post("/projects/{project_id}/style/activate")
@curated("style_activate")
def activate_style(project_id: ProjectId, body: StyleActivateIn, s: Session = Depends(get_session)):
    """Yönetmen onayı: sürümü etkinleştirir. render=true ise etkilenen çekimler hemen kuyruğa girer."""
    project_or_404(s, project_id)
    version_or_404(s, project_id, body.version)
    if body.render:
        ensure_not_frozen(project_id)               # 423 dönen istek etkin sürümü değiştirmiş olmamalı
    impact = bible.impact(s, project_id, body.version)
    previous = bible.active_version(s, project_id)
    bible.activate(s, project_id, body.version)
    s.flush()
    stale = bible.mark_stale(s, project_id, previous, body.version)   # tamamlanmış çekimler yalnızca işaretlenir
    s.commit()
    killswitch.event(project_id, "style_activated", version=body.version, affected=len(impact["affected"]),
                     stale=stale)
    result = {"active_version": body.version, "impact": impact, "style_stale": stale}
    if body.render:                                 # yönetmenin açık isteği: eski stilli çekimler de yeniden üretilir
        s.expire_all()
        result.update(dispatch_all(s, project_id, restyle=True))
    return result
