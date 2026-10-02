"""Ortak API bağımlılıkları: kimlik biçimi doğrulama, 404 yardımcıları ve Kill-Switch denetimi."""
from typing import Annotated

from fastapi import HTTPException, Path
from sqlalchemy.orm import Session

from app.budget import killswitch
from app.models import Project, Scene

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Biçimi bozuk kimlik veritabanına gitmeden 422 olur (aksi halde sürücü hatası 500 döner).
ProjectId = Annotated[str, Path(pattern=UUID_PATTERN)]
SceneId = Annotated[str, Path(pattern=UUID_PATTERN)]
VariationId = Annotated[str, Path(pattern=UUID_PATTERN)]


def project_or_404(s: Session, project_id: str) -> Project:
    p = s.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "Proje bulunamadı")
    return p


def scene_or_404(s: Session, scene_id: str) -> Scene:
    sc = s.get(Scene, scene_id)
    if sc is None:
        raise HTTPException(404, "Sahne bulunamadı")
    return sc


def ensure_not_frozen(project_id: str) -> None:
    """Dondurulmuş projede yeni iş başlatılamaz. Veri DEĞİŞTİRİLMEDEN önce çağrılmalı: 423 dönen istek durumu bozmamalı."""
    if killswitch.is_frozen(project_id):
        raise HTTPException(423, "Proje Kill-Switch ile dondurulmuş. Önce üretime devam ettir.")
