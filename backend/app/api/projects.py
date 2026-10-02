from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, project_or_404
from app.budget import killswitch
from app.config import project_settings
from app.db import get_session
from app.engine.providers import route_engine
from app.hermes.ingest import AssetBook, ensure_character
from app.models import Character, Project, Scene, SceneCharacter, Sequence
from app.schemas import CharacterIn, ProjectIn, SceneIn

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
    }


@router.get("/projects")
def list_projects(s: Session = Depends(get_session)):
    rows = s.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [{"id": str(p.id), "title": p.title, "status": p.status, "spent_usd": float(p.spent_usd),
             "budget_limit_usd": float(p.budget_limit_usd)} for p in rows]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, s: Session = Depends(get_session)):
    p = Project(title=body.title, budget_limit_usd=body.budget_limit_usd, settings=body.settings, status="draft")
    s.add(p)
    s.commit()
    return project_out(p, s)


@router.get("/projects/{project_id}")
def get_project(project_id: ProjectId, s: Session = Depends(get_session)):
    return project_out(project_or_404(s, project_id), s)


@router.post("/projects/{project_id}/characters", status_code=201)
def create_character(project_id: ProjectId, body: CharacterIn, s: Session = Depends(get_session)):
    """Karakter ve ona bağlı taslak karakter varlığı (referansları Varlıklar sekmesinden yüklenir)."""
    project_or_404(s, project_id)
    if s.scalar(select(Character.id).where(Character.project_id == project_id, Character.name == body.name)):
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
def create_scene(project_id: ProjectId, body: SceneIn, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    wanted = set(body.character_ids)
    known = set(s.scalars(select(Character.id).where(Character.project_id == project_id, Character.id.in_(wanted))))
    if wanted - known:
        raise HTTPException(422, f"Sahnedeki karakterlerden biri bu projeye ait değil: {sorted(map(str, wanted - known))}")
    seq = _sequence_for(s, project_id, body.sequence_id)
    data = body.model_dump(exclude={"character_ids", "engine", "camera", "prompt_spec", "sequence_id"})
    sc = Scene(project_id=project_id, sequence_id=seq.id, camera=body.camera.model_dump(),
               prompt_spec=body.prompt_spec.model_dump(), **data)
    sc.engine = body.engine or route_engine(sc)        # görsel özet motoru içerir: baştan yazılır
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
