"""Ortak API bağımlılıkları: kimlik doğrulama (biçim) ve Kill-Switch denetimi."""
from typing import Annotated

from fastapi import HTTPException, Path

from app.budget import killswitch

UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Biçimi bozuk kimlik veritabanına gitmeden 422 olur (aksi halde sürücü hatası 500 döner).
ProjectId = Annotated[str, Path(pattern=UUID_PATTERN)]
SceneId = Annotated[str, Path(pattern=UUID_PATTERN)]
VariationId = Annotated[str, Path(pattern=UUID_PATTERN)]


def ensure_not_frozen(project_id: str) -> None:
    """Dondurulmuş projede yeni iş başlatılamaz. Veri DEĞİŞTİRİLMEDEN önce çağrılmalı: 423 dönen istek durumu bozmamalı."""
    if killswitch.is_frozen(project_id):
        raise HTTPException(423, "Proje Kill-Switch ile dondurulmuş. Önce /killswitch ile devam ettir.")
