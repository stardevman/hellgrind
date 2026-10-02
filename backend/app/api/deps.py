"""Ortak API bağımlılıkları: kimlik biçimi doğrulama, 404 yardımcıları ve Kill-Switch denetimi."""
from contextlib import contextmanager
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
AssetId = Annotated[str, Path(pattern=UUID_PATTERN)]
SequenceId = Annotated[str, Path(pattern=UUID_PATTERN)]
TrackId = Annotated[str, Path(pattern=UUID_PATTERN)]


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


@contextmanager
def paid_errors():
    """İstek içinde yapılan ücretli çağrının (Faz 18) hatalarını Türkçe HTTP yanıtlarına çevirir. Her durumda
    rezervasyon iade edilmiş ya da hiç alınmamıştır."""
    from app.engine.providers import TransientError
    from app.live import PaidCallBlocked                 # yalnızca kilit; ücretli paket yüklenmez
    try:
        yield
    except killswitch.ProjectFrozen:
        raise HTTPException(423, "Bütçe limiti doldu ya da proje donduruldu")
    except PaidCallBlocked as e:
        raise HTTPException(409, f"Ücretli çağrı engellendi: {e}")
    except TransientError as e:
        raise HTTPException(503, f"Ücretli servis geçici olarak yanıt vermedi; biraz sonra yeniden dene. ({e})")
