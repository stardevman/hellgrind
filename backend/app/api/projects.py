from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, project_or_404
from app.budget import killswitch
from app.config import project_settings
from app.core import series
from app.curation import curated
from app.db import get_session
from app.engine.providers import route_engine
from app.hermes.ingest import AssetBook, ensure_character
from app.models import Character, Project, Scene, SceneCharacter, Sequence
from app.schemas import CharacterIn, ProjectIn, ProjectPatch, SceneIn

router = APIRouter(tags=["projects"])


def project_out(p: Project, s: Session) -> dict:
    counts = dict(s.execute(select(Scene.status, func.count()).where(Scene.project_id == p.id)
                            .group_by(Scene.status)).all())
    pid = str(p.id)
    return {
        "id": pid, "title": p.title, "status": p.status,
        "budget_limit_usd": float(p.budget_limit_usd), "spent_usd": float(p.spent_usd),
        "reserved_usd": float(p.reserved_usd), "frozen": killswitch.is_frozen(pid),
        "soft_limited": killswitch.is_soft_limited(pid), "settings": project_settings(p.settings),
        "scene_status_counts": counts, "timeline_key": p.timeline_key,
        "script_source": p.script_source,                    # panelin senaryo sayfası yeniden düzenlemek için okur
        "episode": series.episode_info(s, p),                # dizi › sezon › bölüm ve teslim kilidi ("sürüm sabit")
    }


@router.get("/projects")
def list_projects(s: Session = Depends(get_session)):
    rows = s.scalars(select(Project).order_by(Project.created_at.desc())).all()
    statuses: dict = {}
    for pid, st in s.execute(select(Scene.project_id, Scene.status)):
        statuses.setdefault(pid, []).append(st)
    return [{"id": str(p.id), "title": p.title, "status": p.status, "display_status": display_status(p.status, statuses.get(p.id, [])),
             "spent_usd": float(p.spent_usd), "budget_limit_usd": float(p.budget_limit_usd),
             "season_id": str(p.season_id) if p.season_id else None, "episode_no": p.episode_no,
             "locked": p.locked_at is not None} for p in rows]


ACTIVE_SCENE = ("pending", "storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing")


def display_status(status: str, scene_statuses: list[str]) -> str:
    """Panelde görünen durum: "Üretimde" yalnızca gerçekten işlenen bir çekim varken. Proje durumu üretim bitince
    değişmediği için türetilir: bütün çekimler bittiyse completed, değilse idle (beklemede)."""
    if status != "producing":
        return status
    if any(st in ACTIVE_SCENE for st in scene_statuses):
        return "producing"
    return "completed" if scene_statuses and all(st == "done" for st in scene_statuses) else "idle"


@router.post("/projects", status_code=201)
@curated("project_create")
def create_project(body: ProjectIn, s: Session = Depends(get_session)):
    p = Project(title=body.title, budget_limit_usd=body.budget_limit_usd, settings=body.settings, status="draft")
    s.add(p)
    s.commit()
    return project_out(p, s)


@router.get("/projects/{project_id}")
def get_project(project_id: ProjectId, s: Session = Depends(get_session)):
    return project_out(project_or_404(s, project_id), s)


@router.patch("/projects/{project_id}")
@curated(lambda kw, r: "budget_change" if kw["body"].budget_limit_usd is not None else "project_settings")
def patch_project(project_id: ProjectId, body: ProjectPatch, s: Session = Depends(get_session)):
    """Proje adı, bütçesi ve ayarları (ör. {"settings": {"require_board_approval": true}}). Ayarlar birleştirilir.
    Bütçe harcanan + ayrılan toplamın altına indirilemez; değişiklik olay akışına ve kürasyon kaydına yazılır."""
    p = project_or_404(s, project_id)
    if body.budget_limit_usd is not None:
        used = float(p.spent_usd) + float(p.reserved_usd)
        if body.budget_limit_usd < used:
            raise HTTPException(422, f"Bütçe harcanan ve ayrılan toplamdan ({used:.2f} $) az olamaz.")
        old = float(p.budget_limit_usd)
        p.budget_limit_usd = body.budget_limit_usd
        killswitch.event(str(p.id), "budget_changed", old=old, new=float(body.budget_limit_usd))
    if body.title is not None:
        p.title = body.title.strip() or p.title
    if body.settings:
        p.settings = {**(p.settings or {}), **body.settings}
    s.commit()
    return project_out(p, s)


@router.delete("/projects/{project_id}")
def delete_project(project_id: ProjectId, s: Session = Depends(get_session)):
    """Projeyi ve tüm sahne, varyasyon, kanal ve defter kayıtlarını siler (veritabanında CASCADE).
    Kürasyon olayı yazılmaz: kayıt projeyle birlikte silinir. Nesne deposundaki dosyalara dokunulmaz; anahtarlar
    içerik-adreslidir ve başka projelerle paylaşılıyor olabilir. Üretim sürerken reddedilir."""
    p = project_or_404(s, project_id)
    pid = str(p.id)
    if killswitch.has_running_tasks(pid):
        raise HTTPException(409, "Üretim sürüyor; önce 'Üretimi durdur' ile durdurun, sonra silin.")
    title = p.title
    s.delete(p)
    s.commit()
    r = killswitch.r()
    r.delete(*(k.format(pid) for k in (killswitch.FROZEN, killswitch.SOFT, killswitch.TASKS, killswitch.EVENTS,
                                       killswitch.RESUMED_AT)))
    return {"deleted": pid, "title": title}


@router.post("/projects/{project_id}/characters", status_code=201)
@curated("character_create")
def create_character(project_id: ProjectId, body: CharacterIn, s: Session = Depends(get_session)):
    """Karakter ve ona bağlı taslak karakter varlığı (referansları Varlıklar sekmesinden yüklenir)."""
    project_or_404(s, project_id)
    if s.scalar(series.episode_characters_q(project_id).where(Character.name == body.name).with_only_columns(Character.id)):
        raise HTTPException(409, f"Bu projede '{body.name}' adlı bir karakter zaten var.")
    ch = ensure_character(s, AssetBook(s, project_id), project_id, body.name, {}, **body.model_dump(exclude={"name"}))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Bu projede '{body.name}' adlı bir karakter zaten var.")
    return {"id": str(ch.id), "name": body.name, "asset_id": str(ch.asset_id)}


def _sequence_for(s: Session, project_id: str, sequence_id) -> Sequence:
    """Elle eklenen sahnenin sekansı: verilen (bu projeden olmalı), yoksa son sekans, o da yoksa SEQ01 açılır."""
    if sequence_id:
        seq = s.get(Sequence, sequence_id)
        if seq is None or str(seq.project_id) != project_id:
            raise HTTPException(422, "Sekans bu projeye ait değil")
        return seq
    seq = s.scalar(select(Sequence).where(Sequence.project_id == project_id).order_by(Sequence.seq_order.desc()).limit(1))
    if seq is None:
        seq = Sequence(project_id=project_id, code="SEQ01", title="Sekans 1", seq_order=1, status="active", settings={})
        s.add(seq)
        s.flush()
    return seq


@router.post("/projects/{project_id}/scenes", status_code=201)
@curated("scene_create")
def create_scene(project_id: ProjectId, body: SceneIn, s: Session = Depends(get_session)):
    project = project_or_404(s, project_id)
    wanted = set(body.character_ids)
    known = set(s.scalars(series.episode_characters_q(project_id).where(Character.id.in_(wanted))
                          .with_only_columns(Character.id)))
    if wanted - known:
        raise HTTPException(422, f"Sahnedeki karakterlerden biri bu projeye ait değil: {sorted(map(str, wanted - known))}")
    seq = _sequence_for(s, project_id, body.sequence_id)
    data = body.model_dump(exclude={"character_ids", "engine", "camera", "prompt_spec", "sequence_id"})
    sc = Scene(project_id=project_id, sequence_id=seq.id, camera=body.camera.model_dump(),
               prompt_spec=body.prompt_spec.model_dump(), **data)
    sc.engine = body.engine or route_engine(sc, project_settings(project.settings))   # görsel özet motoru içerir
    s.add(sc)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Bu projede '{body.scene_code}' kodlu bir sahne zaten var.")
    for cid in wanted:
        s.add(SceneCharacter(scene_id=sc.id, character_id=cid, role="primary", dialogue=[]))
    s.commit()
    return {"id": str(sc.id), "scene_code": sc.scene_code, "engine": sc.engine, "sequence_id": str(seq.id)}
