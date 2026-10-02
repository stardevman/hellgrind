import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, project_or_404
from app.budget import killswitch
from app.config import project_settings
from app.db import get_session
from app.engine.providers import route_engine
from app.models import Character, Project, Scene, SceneCharacter
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
    project_or_404(s, project_id)
    cid = uuid.uuid4()
    s.add(Character(id=cid, project_id=project_id, pinecone_namespace=str(cid), **body.model_dump()))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, f"Bu projede '{body.name}' adlı bir karakter zaten var.")
    return {"id": str(cid), "name": body.name}


@router.post("/projects/{project_id}/scenes", status_code=201)
def create_scene(project_id: ProjectId, body: SceneIn, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    wanted = set(body.character_ids)
    known = set(s.scalars(select(Character.id).where(Character.project_id == project_id, Character.id.in_(wanted))))
    if wanted - known:
        raise HTTPException(422, f"Sahnedeki karakterlerden biri bu projeye ait değil: {sorted(map(str, wanted - known))}")
    data = body.model_dump(exclude={"character_ids", "engine", "camera", "prompt_spec"})
    sc = Scene(project_id=project_id, camera=body.camera.model_dump(), prompt_spec=body.prompt_spec.model_dump(), **data)
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
    return {"id": str(sc.id), "scene_code": sc.scene_code, "engine": sc.engine}
