"""Dizi hiyerarşisi (docs/dizi-hiyerarsisi.md): Dizi › Sezon › Bölüm › Sekans › Çekim. Bölüm, bugünkü projedir.

Dizi kütüphanesi: bir varlık (ve karakteri) ya bir bölüme ya da diziye aittir. Dizi varlığı bölüme kopyalanmaz,
bağlanır (episode_assets). Bölümün varlık kümesi = bölüme özel varlıklar ∪ bölüme bağlanmış dizi varlıkları; senaryo
işlenirken ve varlık listesinde bu küme kullanılır.

Sürüm sabitleme: bağın pinned_version alanı boşsa bölüm kütüphanedeki son sürümü kullanır, doluysa o sürüme sabittir.
Görsel özet (core/hashing.visual_fingerprint) sabit sürümü alır (load_assets / load_characters bunu uygular), böylece
dizi varlığının referansı yenilenince bütün bölümler kendiliğinden "değişmiş" görünmez:
- Sürüm artınca (`on_version_bump`), son sürümü izleyen ve bu varlıkla görüntüsü üretilmiş çekimi olan bölümler eski
  sürüme sabitlenir ve "yeni sürüm var" uyarısı alır. Yeni sürüme geçiş, stil önizlemesi gibi önce etkisini gösterir.
- Sabit sürümün gerisinde kalan varlıkla görüntü üretilmez (lint): referanslar yalnızca son sürümde durur; eski özetle yeni
  görüntü karışmasın. Yönetmen önce yeni sürüme geçer.
- Teslim edilmiş bölüm (Kabul kapısı onaylandı, `lock_episode`) bütün dizi varlıklarını o anki sürüme sabitler ve kilitlenir.
"""
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.models import (Asset, Character, EpisodeAsset, FilmVersion, Project, Scene, SceneAsset, SceneCharacter,
                        Season, Series)


class EpisodeLocked(HTTPException):
    """Teslim edilmiş bölüm ("sürüm sabit"): üretim ve görüntüyü değiştiren düzenleme yapılmaz."""

    def __init__(self, label: str | None = None):
        super().__init__(423, f"Bölüm teslim edildi ve sürümü sabit{f' ({label})' if label else ''}: üretim ve "
                              "düzenleme kapalı. Gerekirse yönetici kilidi açar.")


# ---------- küme ----------

def linked_ids(project_id):
    return select(EpisodeAsset.asset_id).where(EpisodeAsset.project_id == project_id)


def episode_assets_q(project_id):
    """Bölümün varlıkları: bölüme özel olanlar ve bölüme bağlanmış dizi varlıkları."""
    return select(Asset).where(or_(Asset.project_id == project_id, Asset.id.in_(linked_ids(project_id))))


def episode_characters_q(project_id):
    """Bölümün karakterleri: bölüme özel olanlar ve varlığı bölüme bağlanmış dizi karakterleri."""
    return select(Character).where(or_(Character.project_id == project_id,
                                       Character.asset_id.in_(linked_ids(project_id))))


def series_of(s, project_id) -> Series | None:
    return s.scalar(select(Series).join(Season, Season.series_id == Series.id).join(Project, Project.season_id == Season.id)
                    .where(Project.id == project_id))


def library(s, series_id) -> list[Asset]:
    return list(s.scalars(select(Asset).where(Asset.series_id == series_id)))


def pins(s, project_id) -> dict:
    return {e.asset_id: e for e in s.scalars(select(EpisodeAsset).where(EpisodeAsset.project_id == project_id))}


# ---------- sabit sürüm ----------

class Pinned:
    """Bölümün sabit sürümüyle görünen varlık ya da karakter: özellikler asıl satırdan, sürüm bağdan gelir."""

    def __init__(self, obj, **override):
        self._obj, self._override = obj, override

    def __getattr__(self, name):
        if name in self._override:
            return self._override[name]
        return getattr(self._obj, name)


def _project_of_scene(s, scene_id):
    return s.scalar(select(Scene.project_id).where(Scene.id == scene_id))


def pinned_assets(s, scene_id, assets: list) -> list:
    """Dizi varlıklarına bölümün sabit sürümünü uygular (görsel özet bu sürümü görür)."""
    if not any(getattr(a, "series_id", None) for a in assets):
        return assets
    links = pins(s, _project_of_scene(s, scene_id))
    out = []
    for a in assets:
        link = links.get(a.id) if a.series_id else None
        out.append(Pinned(a, version=link.pinned_version)
                   if link and link.pinned_version is not None and link.pinned_version != a.version else a)
    return out


def pinned_characters(s, scene_id, chars: list) -> list:
    if not any(getattr(c, "series_id", None) for c in chars):
        return chars
    links = pins(s, _project_of_scene(s, scene_id))
    out = []
    for c in chars:
        link = links.get(c.asset_id) if c.series_id else None
        out.append(Pinned(c, embedding_version=link.pinned_embedding)
                   if link and link.pinned_embedding is not None and link.pinned_embedding != c.embedding_version else c)
    return out


def scene_series_assets(s, scene_id) -> list[Asset]:
    linked = select(SceneAsset.asset_id).where(SceneAsset.scene_id == scene_id)
    cast = (select(Character.asset_id).join(SceneCharacter, SceneCharacter.character_id == Character.id)
            .where(SceneCharacter.scene_id == scene_id, Character.asset_id.is_not(None)))
    return list(s.scalars(select(Asset).where(Asset.id.in_(linked.union(cast)), Asset.series_id.is_not(None))))


def behind(s, scene_id) -> list[dict]:
    """Çekimin sabit sürümün gerisinde kalan dizi varlıkları: [{"asset_id", "type", "name", "pinned", "current"}]."""
    assets = scene_series_assets(s, scene_id)
    if not assets:
        return []
    links = pins(s, _project_of_scene(s, scene_id))
    out = []
    for a in assets:
        link = links.get(a.id)
        if link and link.pinned_version is not None and link.pinned_version != a.version:
            out.append({"asset_id": str(a.id), "type": a.type, "name": a.name, "pinned": link.pinned_version,
                        "current": a.version})
    return out


def _uses(s, project_id, asset_id):
    """Bölümde bu varlığı kullanan çekimler (bağlı varlık ya da kadrodaki karakter üzerinden)."""
    via_asset = select(SceneAsset.scene_id).where(SceneAsset.asset_id == asset_id)
    via_cast = (select(SceneCharacter.scene_id).join(Character, Character.id == SceneCharacter.character_id)
                .where(Character.asset_id == asset_id))
    return list(s.scalars(select(Scene).where(Scene.project_id == project_id, Scene.id.in_(via_asset.union(via_cast)))
                          .order_by(Scene.seq_order)))


def on_version_bump(s, asset: Asset, old_version: int, old_embedding: int | None) -> list[str]:
    """Dizi varlığının sürümü arttı: son sürümü izleyen ve bu varlıkla görüntüsü üretilmiş çekimi olan bölümler eski
    sürüme sabitlenir (kendiliğinden yeniden üretim yok). Dönen: sabitlenen bölümlerin kimlikleri."""
    if getattr(asset, "series_id", None) is None:
        return []
    held = []
    for link in s.scalars(select(EpisodeAsset).where(EpisodeAsset.asset_id == asset.id,
                                                     EpisodeAsset.pinned_version.is_(None))):
        if any(sc.visual_hash is not None for sc in _uses(s, link.project_id, asset.id)):
            link.pinned_version, link.pinned_embedding = old_version, old_embedding
            held.append(str(link.project_id))
    return held


def updates(s, project_id) -> list[dict]:
    """Bölümde yeni sürümü olan dizi varlıkları (sabit < son)."""
    out = []
    for link in s.scalars(select(EpisodeAsset).where(EpisodeAsset.project_id == project_id,
                                                     EpisodeAsset.pinned_version.is_not(None))):
        a = s.get(Asset, link.asset_id)
        if a is not None and a.version != link.pinned_version:
            out.append({"asset_id": str(a.id), "name": a.name, "type": a.type, "pinned": link.pinned_version,
                        "current": a.version})
    return out


def upgrade_impact(s, project_id, asset_id) -> dict:
    """Yeni sürüme geçerse bölümde yeniden üretilecek çekimler ve tahmini sanal maliyet (hiçbir şey yazmaz)."""
    from app.config import project_settings
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.providers import route_engine
    from app.style import bible
    link = s.get(EpisodeAsset, (project_id, asset_id))
    a = s.get(Asset, asset_id)
    if link is None or a is None:
        raise HTTPException(404, "Bu varlık bölüme bağlı değil")
    cfg = project_settings(s.get(Project, project_id).settings)
    style = bible.active_version(s, project_id)
    affected, expected, worst = [], 0.0, 0.0
    for sc in _uses(s, project_id, asset_id):
        if sc.visual_hash is None:
            continue
        assets = [x._obj if isinstance(x, Pinned) else x for x in load_assets(s, sc.id)]
        chars = [x._obj if isinstance(x, Pinned) else x for x in load_characters(s, sc.id)]
        if visual_fingerprint(sc, chars, assets, style) != sc.visual_hash:
            e, w = bible.visual_cost(cfg, sc.engine or route_engine(sc, cfg), sc.duration_s)
            expected, worst = expected + e, worst + w
            affected.append(sc.scene_code)
    return {"asset_id": str(asset_id), "name": a.name, "pinned": link.pinned_version, "current": a.version,
            "affected": affected, "expected_usd": round(expected, 4), "worst_usd": round(worst, 4)}


# ---------- kilit ----------

def ensure_unlocked(s, project_id) -> None:
    p = s.get(Project, project_id)
    if p is not None and p.locked_at is not None:
        v = s.get(FilmVersion, p.locked_version_id) if p.locked_version_id else None
        raise EpisodeLocked(v.label if v else None)


def lock_episode(s, project_id, version_id) -> list[str]:
    """Kabul kapısı onaylandı: bölüm o film sürümüne ve dizi varlıklarının şimdiki sürümlerine sabitlenir."""
    p = s.get(Project, project_id)
    p.locked_at, p.locked_version_id = dt.datetime.now(dt.timezone.utc), version_id
    pinned = []
    for link in s.scalars(select(EpisodeAsset).where(EpisodeAsset.project_id == project_id)):
        a = s.get(Asset, link.asset_id)
        if link.pinned_version is None and a is not None:
            ch = s.scalar(select(Character).where(Character.asset_id == a.id))
            link.pinned_version, link.pinned_embedding = a.version, ch.embedding_version if ch else None
            pinned.append(a.name)
    return pinned


# ---------- panel ----------

def episode_info(s, p: Project) -> dict:
    """Panelin kırıntısı ve rozeti için: dizi, sezon, bölüm numarası ve teslim kilidi (bağımsız filmde hepsi boş)."""
    z = s.get(Season, p.season_id) if p.season_id else None
    q = s.get(Series, z.series_id) if z else None
    v = s.get(FilmVersion, p.locked_version_id) if p.locked_version_id else None
    return {"series": {"id": str(q.id), "title": q.title} if q else None,
            "season": {"id": str(z.id), "number": z.number, "title": z.title} if z else None,
            "episode_no": p.episode_no,
            "locked": {"at": p.locked_at.isoformat(), "label": v.label if v else None} if p.locked_at else None}
