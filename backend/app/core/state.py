"""Sahne durumu ve ilişkili satırlar için ortak veritabanı yardımcıları (motor ve post-prodüksiyon paylaşır)."""
from sqlalchemy import select

from app.budget import killswitch
from app.db import session_scope
from app.models import Asset, Character, Scene, SceneAsset, SceneCharacter


def load_characters(s, scene_id):
    """Sahnedeki karakterler, ada göre sıralı. Dizi karakterlerinde bölümün sabit vektör sürümü (core/series.py)."""
    from app.core.series import pinned_characters
    rows = s.scalars(select(Character).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                     .where(SceneCharacter.scene_id == scene_id).order_by(Character.name)).all()
    return pinned_characters(s, scene_id, list(rows))


def load_assets(s, scene_id):
    """Sahnenin görüntüsünü belirleyen varlıklar: bağlı mekân ve nesneler ile kadrodaki karakterlerin varlıkları. Dizi
    varlıklarında bölümün sabit sürümü (görsel özet onu görür; core/series.py)."""
    from app.core.series import pinned_assets
    linked = select(SceneAsset.asset_id).where(SceneAsset.scene_id == scene_id)
    cast = (select(Character.asset_id).join(SceneCharacter, SceneCharacter.character_id == Character.id)
            .where(SceneCharacter.scene_id == scene_id, Character.asset_id.is_not(None)))
    rows = s.scalars(select(Asset).where(Asset.id.in_(linked.union(cast))).order_by(Asset.id)).all()
    return pinned_assets(s, scene_id, list(rows))


def advance_scene(project_id: str, scene_id: str, **fields) -> None:
    """Sahne alanlarını günceller; proje dondurulmuşsa yazmaz (freeze'in `frozen` damgasını ezmemek için) ve durur."""
    if killswitch.is_frozen(project_id):
        raise killswitch.ProjectFrozen(project_id)
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        for key, value in fields.items():
            setattr(scene, key, value)
