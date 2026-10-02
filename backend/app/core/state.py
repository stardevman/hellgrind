"""Sahne durumu ve ilişkili satırlar için ortak veritabanı yardımcıları (motor ve post-prodüksiyon paylaşır)."""
from sqlalchemy import select

from app.budget import killswitch
from app.db import session_scope
from app.models import Character, Scene, SceneCharacter


def load_characters(s, scene_id):
    """Sahnedeki karakterler, ada göre sıralı."""
    return s.scalars(select(Character).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                     .where(SceneCharacter.scene_id == scene_id).order_by(Character.name)).all()


def advance_scene(project_id: str, scene_id: str, **fields) -> None:
    """Sahne alanlarını günceller; proje dondurulmuşsa yazmaz (freeze'in `frozen` damgasını ezmemek için) ve durur."""
    if killswitch.is_frozen(project_id):
        raise killswitch.ProjectFrozen(project_id)
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        for key, value in fields.items():
            setattr(scene, key, value)
