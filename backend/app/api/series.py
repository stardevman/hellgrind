"""Dizi hiyerarşisi uç noktaları (docs/dizi-hiyerarsisi.md): Dizi › Sezon › Bölüm; dizi kütüphanesi ve bölüme bağlama;
sabit sürüm ve yeni sürüme geçiş (önce etkisi); teslim edilmiş bölümün kilidi ("sürüm sabit")."""
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin import require_admin
from app.api.deps import AssetId, ProjectId, project_or_404
from app.budget import killswitch
from app.core import series
from app.curation import curated
from app.db import get_session
from app.hermes.rules import tr_title
from app.models import Asset, Character, EpisodeAsset, FilmVersion, Project, Scene, Season, Series
from app.schemas import EpisodeIn, EpisodePlaceIn, SeasonIn, SeriesAssetIn, SeriesIn, SeriesUpgradeIn, UnlockIn

router = APIRouter(tags=["series"])
UNLOCK_PHRASE = "kilidi aç"


def series_or_404(s: Session, series_id) -> Series:
    row = s.get(Series, series_id)
    if row is None:
        raise HTTPException(404, "Dizi bulunamadı")
    return row


def season_or_404(s: Session, season_id) -> Season:
    row = s.get(Season, season_id)
    if row is None:
        raise HTTPException(404, "Sezon bulunamadı")
    return row


def episode_out(p: Project, labels: dict, statuses: dict) -> dict:
    from app.api.projects import display_status
    return {"id": str(p.id), "title": p.title, "episode_no": p.episode_no,
            "status": display_status(p.status, statuses.get(p.id, [])),
            "locked": p.locked_at is not None, "locked_label": labels.get(p.locked_version_id)}


def tree(s: Session) -> list[dict]:
    """Bütün diziler, sezonları ve bölümleri (bölüm numarasıyla sıralı)."""
    labels = dict(s.execute(select(FilmVersion.id, FilmVersion.label)).all())
    seasons = list(s.scalars(select(Season).order_by(Season.number)))
    episodes = list(s.scalars(select(Project).where(Project.season_id.is_not(None))
                              .order_by(Project.episode_no.nulls_last(), Project.created_at)))
    statuses: dict = {}
    for pid, st in s.execute(select(Scene.project_id, Scene.status).where(Scene.project_id.in_([p.id for p in episodes]))):
        statuses.setdefault(pid, []).append(st)
    out = []
    for q in s.scalars(select(Series).order_by(Series.created_at)):
        out.append({"id": str(q.id), "title": q.title,
                    "library": s.scalar(select(func.count()).select_from(Asset).where(Asset.series_id == q.id)),
                    "seasons": [{"id": str(z.id), "number": z.number, "title": z.title,
                                 "episodes": [episode_out(p, labels, statuses) for p in episodes if p.season_id == z.id]}
                                for z in seasons if z.series_id == q.id]})
    return out


@router.get("/series")
def list_series(s: Session = Depends(get_session)):
    return tree(s)


@router.post("/series", status_code=201)                # yapı, karar değil: proje yok, kürasyon kaydı bölüm eylemlerinde
def create_series(body: SeriesIn, s: Session = Depends(get_session)):
    q = Series(title=body.title.strip())
    s.add(q)
    s.flush()
    s.add(Season(series_id=q.id, number=1, title=None))            # ilk sezon hazır gelir
    s.commit()
    return next(x for x in tree(s) if x["id"] == str(q.id))


@router.post("/series/{series_id}/seasons", status_code=201)
def create_season(series_id: uuid.UUID, body: SeasonIn, s: Session = Depends(get_session)):
    series_or_404(s, series_id)
    number = body.number or (s.scalar(select(func.max(Season.number)).where(Season.series_id == series_id)) or 0) + 1
    if s.scalar(select(Season.id).where(Season.series_id == series_id, Season.number == number)):
        raise HTTPException(409, f"Bu dizide {number}. sezon zaten var.")
    z = Season(series_id=series_id, number=number, title=body.title)
    s.add(z)
    s.commit()
    return {"id": str(z.id), "number": z.number, "title": z.title, "episodes": []}


def _place(s: Session, p: Project, season_id, episode_no: int | None) -> None:
    if season_id is None:
        p.season_id, p.episode_no = None, None
        return
    season_or_404(s, season_id)
    taken = set(s.scalars(select(Project.episode_no).where(Project.season_id == season_id, Project.id != p.id)))
    number = episode_no or (max([n for n in taken if n] or [0]) + 1)
    if number in taken:
        raise HTTPException(409, f"Bu sezonda {number}. bölüm zaten var.")
    p.season_id, p.episode_no = season_id, number


@router.post("/seasons/{season_id}/episodes", status_code=201)
@curated("episode_create")
def create_episode(season_id: uuid.UUID, body: EpisodeIn, s: Session = Depends(get_session)):
    """Yeni bölüm = yeni proje (bütçe, ayarlar ve üretim proje başına), sezona numarasıyla yerleşir."""
    season_or_404(s, season_id)
    p = Project(title=body.title.strip(), budget_limit_usd=body.budget_limit_usd, settings=body.settings, status="draft")
    s.add(p)
    s.flush()
    _place(s, p, season_id, body.episode_no)
    s.commit()
    return {"id": str(p.id), "title": p.title, "season_id": str(season_id), "episode_no": p.episode_no,
            "project_id": str(p.id)}


@router.post("/projects/{project_id}/episode")
@curated("episode_place")
def place_episode(project_id: ProjectId, body: EpisodePlaceIn, s: Session = Depends(get_session)):
    """Var olan projeyi bir sezona bölüm olarak yerleştirir (season_id boşsa bağımsız filme döner)."""
    p = project_or_404(s, project_id)
    if p.season_id and body.season_id != p.season_id and \
            s.scalar(select(EpisodeAsset.asset_id).where(EpisodeAsset.project_id == p.id).limit(1)):
        old = series.series_of(s, p.id)
        new = s.get(Season, body.season_id).series_id if body.season_id else None
        if old is not None and new != old.id:
            raise HTTPException(409, "Bölüme bağlı dizi varlıkları var; başka diziye taşımadan önce bağları kaldırın.")
    _place(s, p, body.season_id, body.episode_no)
    s.commit()
    return {"project_id": project_id, "season_id": str(p.season_id) if p.season_id else None, "episode_no": p.episode_no}


# ---------- dizi kütüphanesi ----------

def _series_for(s: Session, project_id) -> Series:
    q = series.series_of(s, project_id)
    if q is None:
        raise HTTPException(409, "Bu proje bir diziye bağlı değil (bağımsız film): dizi kütüphanesi yok.")
    return q


@router.get("/projects/{project_id}/series-library")
def project_library(project_id: ProjectId, s: Session = Depends(get_session)):
    """Bölümün dizisinin kütüphanesi: her varlığın bu bölüme bağlı olup olmadığı, sabit ve son sürümü."""
    from app.api.assets import assets_out
    project_or_404(s, project_id)
    q = series.series_of(s, project_id)
    if q is None:
        return {"series": None, "assets": [], "updates": []}
    links = series.pins(s, project_id)
    order = {"character": 0, "location": 1, "prop": 2}
    rows = sorted(series.library(s, q.id), key=lambda a: (order[a.type], a.name))
    out = assets_out(s, rows, project_id)
    for row, a in zip(out, rows):
        link = links.get(a.id)
        row["link"] = None if link is None else {"pinned": link.pinned_version, "current": a.version,
                                                 "behind": link.pinned_version is not None and link.pinned_version != a.version}
    return {"series": {"id": str(q.id), "title": q.title}, "assets": out, "updates": series.updates(s, project_id)}


@router.post("/projects/{project_id}/series-library", status_code=201)
@curated("series_asset_create")
def create_series_asset(project_id: ProjectId, body: SeriesAssetIn, s: Session = Depends(get_session)):
    """Dizi kütüphanesine varlık ekler ve (link=true) bu bölüme bağlar. Karakterde ses alanları dizi karakterindedir."""
    from app.api.assets import asset_out
    project_or_404(s, project_id)
    q = _series_for(s, project_id)
    name = tr_title(" ".join(body.name.split()))
    if not name:
        raise HTTPException(422, "Varlık adı boş olamaz")
    if s.scalar(select(Asset.id).where(Asset.series_id == q.id, Asset.type == body.type, Asset.name == name)):
        raise HTTPException(409, f"Dizi kütüphanesinde '{name}' adlı bir varlık zaten var.")
    a = Asset(id=uuid.uuid4(), series_id=q.id, project_id=None, type=body.type, name=name, description=body.description,
              render_style=None, status="draft", version=1, references={})
    s.add(a)
    s.flush()
    if body.type == "character":
        cid = uuid.uuid4()
        s.add(Character(id=cid, series_id=q.id, project_id=None, name=name, description=body.description,
                        asset_id=a.id, pinecone_namespace=str(cid)))
    if body.link:
        s.add(EpisodeAsset(project_id=project_id, asset_id=a.id))
    s.commit()
    return {**asset_out(s, a, project_id), "asset_id": str(a.id)}


@router.post("/projects/{project_id}/series-assets/{asset_id}", status_code=201)
@curated("series_asset_link")
def link_asset(project_id: ProjectId, asset_id: AssetId, s: Session = Depends(get_session)):
    """Dizi varlığını bölüme bağlar (kopyalamaz). Son sürümü izler."""
    project_or_404(s, project_id)
    series.ensure_unlocked(s, project_id)
    q = _series_for(s, project_id)
    a = s.get(Asset, asset_id)
    if a is None or a.series_id != q.id:
        raise HTTPException(404, "Varlık bu bölümün dizi kütüphanesinde yok")
    if s.scalar(select(Asset.id).where(Asset.project_id == project_id, Asset.type == a.type, Asset.name == a.name)):
        raise HTTPException(409, f"Bölümde '{a.name}' adlı bölüme özel bir varlık var; aynı adla bağlanamaz.")
    if s.get(EpisodeAsset, (uuid.UUID(project_id), a.id)) is None:
        s.add(EpisodeAsset(project_id=project_id, asset_id=a.id))
    s.commit()
    return {"project_id": project_id, "asset_id": asset_id, "linked": True}


@router.delete("/projects/{project_id}/series-assets/{asset_id}")
@curated("series_asset_unlink")
def unlink_asset(project_id: ProjectId, asset_id: AssetId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    series.ensure_unlocked(s, project_id)
    link = s.get(EpisodeAsset, (uuid.UUID(project_id), uuid.UUID(asset_id)))
    if link is None:
        raise HTTPException(404, "Varlık bu bölüme bağlı değil")
    used = [sc.scene_code for sc in series._uses(s, project_id, asset_id)]
    if used:
        raise HTTPException(409, f"Varlık bu bölümde kullanılıyor ({', '.join(used)}); önce çekimlerden kaldırın.")
    s.delete(link)
    s.commit()
    return {"project_id": project_id, "asset_id": asset_id, "linked": False}


@router.get("/projects/{project_id}/series-assets/{asset_id}/upgrade")
def preview_upgrade(project_id: ProjectId, asset_id: AssetId, s: Session = Depends(get_session)):
    """Yeni sürüme geçişin etkisi: yeniden üretilecek çekimler ve tahmini sanal maliyet. Hiçbir şey yazmaz."""
    project_or_404(s, project_id)
    return series.upgrade_impact(s, uuid.UUID(project_id), uuid.UUID(asset_id))


@router.post("/projects/{project_id}/series-assets/{asset_id}/upgrade")
@curated("series_asset_upgrade")
def apply_upgrade(project_id: ProjectId, asset_id: AssetId, body: SeriesUpgradeIn, s: Session = Depends(get_session)):
    """Yönetmen onayı: bölüm dizi varlığının son sürümüne geçer (ve izlemeye devam eder). render=true ise etkilenen
    çekimler hemen kuyruğa girer; değilse bir sonraki üretimde yenilenir."""
    project_or_404(s, project_id)
    series.ensure_unlocked(s, project_id)
    impact = series.upgrade_impact(s, uuid.UUID(project_id), uuid.UUID(asset_id))
    link = s.get(EpisodeAsset, (uuid.UUID(project_id), uuid.UUID(asset_id)))
    link.pinned_version, link.pinned_embedding = None, None
    s.commit()
    killswitch.event(project_id, "series_asset_upgrade", name=impact["name"], version=impact["current"],
                     affected=impact["affected"])
    result = {"impact": impact, "planned": {}}
    if body.render and impact["affected"]:
        from app.api.plan import dispatch_result
        for code in impact["affected"]:
            sc = s.scalar(select(Scene).where(Scene.project_id == project_id, Scene.scene_code == code))
            result["planned"][code] = dispatch_result(s, sc).get("stages", [])
    return result


# ---------- kilit ----------

@router.post("/projects/{project_id}/unlock", dependencies=[Depends(require_admin)])
@curated("episode_unlock")
def unlock_episode(project_id: ProjectId, body: UnlockIn, s: Session = Depends(get_session)):
    """Teslim edilmiş bölümün kilidini açar (yalnızca yönetici, onay ifadesiyle). Dizi varlıklarının sabit sürümleri
    kalır: kilit açılınca bölüm kendiliğinden yeniden üretilmez."""
    p = project_or_404(s, project_id)
    if body.confirm.strip().lower() != UNLOCK_PHRASE:
        raise HTTPException(422, f"Kilidi açmak için onay ifadesini yazın: \"{UNLOCK_PHRASE}\".")
    if p.locked_at is None:
        raise HTTPException(409, "Bölüm kilitli değil.")
    label = s.get(FilmVersion, p.locked_version_id).label if p.locked_version_id else None
    p.locked_at, p.locked_version_id = None, None
    s.commit()
    killswitch.event(project_id, "episode_unlocked", label=label)
    return {"project_id": project_id, "locked": False, "was": label}


def lock_after_acceptance(s: Session, project_id, version_id) -> None:
    """Kabul kapısı onaylanınca çağrılır (inceleme bağlantısı ya da panel)."""
    p = s.get(Project, project_id)
    if p is None or p.locked_at is not None:
        return
    pinned = series.lock_episode(s, project_id, version_id)
    label = s.get(FilmVersion, version_id).label if version_id else None
    killswitch.event(str(project_id), "episode_locked", label=label, pinned=pinned,
                     at=dt.datetime.now(dt.timezone.utc).isoformat())
