from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS
from app.db import get_session
from app.models import Character, Project, Scene, SceneCharacter
from app.schemas import CharacterIn, ProjectIn, SceneIn

router = APIRouter(tags=["projects"])


def _project_out(p: Project, s: Session) -> dict:
    counts = dict(s.execute(select(Scene.status, func.count()).where(Scene.project_id == p.id)
                            .group_by(Scene.status)).all())
    return {
        "id": str(p.id), "title": p.title, "status": p.status,
        "budget_limit_usd": float(p.budget_limit_usd), "spent_usd": float(p.spent_usd),
        "reserved_usd": float(p.reserved_usd), "frozen": killswitch.is_frozen(str(p.id)),
        "soft_limited": killswitch.is_soft_limited(str(p.id)),
        "settings": {**DEFAULT_PROJECT_SETTINGS, **(p.settings or {})}, "scene_status_counts": counts,
    }


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, s: Session = Depends(get_session)):
    unknown = set(body.settings) - set(DEFAULT_PROJECT_SETTINGS)
    if unknown:
        raise HTTPException(422, f"Bilinmeyen ayar: {sorted(unknown)}")
    p = Project(title=body.title, budget_limit_usd=body.budget_limit_usd, settings=body.settings,
                status="draft", spent_usd=0, reserved_usd=0)
    s.add(p)
    s.commit()
    return _project_out(p, s)


@router.get("/projects/{project_id}")
def get_project(project_id: str, s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    return _project_out(p, s)


@router.post("/projects/{project_id}/characters", status_code=201)
def create_character(project_id: str, body: CharacterIn, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    c = Character(project_id=project_id, pinecone_namespace=project_id, reference_images=[],
                  style_constraints={}, embedding_version=1, **body.model_dump())
    s.add(c)
    s.commit()
    return {"id": str(c.id), "name": c.name}


@router.post("/projects/{project_id}/scenes", status_code=201)
def create_scene(project_id: str, body: SceneIn, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    data = body.model_dump(exclude={"character_ids"})
    sc = Scene(project_id=project_id, status="pending", prompt_revision=0, correction_count=0, spent_usd=0, **data)
    s.add(sc)
    s.flush()
    for cid in body.character_ids:
        s.add(SceneCharacter(scene_id=sc.id, character_id=cid, role="primary", dialogue=[]))
    s.commit()
    return {"id": str(sc.id), "scene_code": sc.scene_code}


@router.get("/projects/{project_id}/scenes")
def list_scenes(project_id: str, s: Session = Depends(get_session)):
    rows = s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)).all()
    return [{"id": str(r.id), "scene_code": r.scene_code, "status": r.status, "engine": r.engine,
             "spent_usd": float(r.spent_usd), "final_video_key": r.final_video_key} for r in rows]
