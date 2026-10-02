import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.budget import killswitch
from app.api.deps import ProjectId
from app.db import get_session
from app.models import Project
from app.schemas import KillSwitchIn

router = APIRouter(tags=["budget"])


@router.get("/projects/{project_id}/budget")
def budget(project_id: ProjectId, s: Session = Depends(get_session)):
    p = s.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    by_provider = s.execute(text("""
        SELECT provider, kind, COUNT(*), SUM(amount_usd) FROM cost_ledger
        WHERE project_id = :p GROUP BY provider, kind ORDER BY provider, kind"""), {"p": project_id}).all()
    events = [json.loads(e) for e in killswitch.r().lrange(killswitch.EVENTS.format(project_id), 0, 49)]
    return {
        "limit": float(p.budget_limit_usd), "spent": float(p.spent_usd), "reserved": float(p.reserved_usd),
        "remaining": float(p.budget_limit_usd - p.spent_usd - p.reserved_usd),
        "status": p.status, "frozen": killswitch.is_frozen(project_id),
        "ledger": [{"provider": a, "kind": b, "count": c, "usd": float(d)} for a, b, c, d in by_provider],
        "recent_events": events,
    }


@router.post("/projects/{project_id}/killswitch")
def killswitch_action(project_id: ProjectId, body: KillSwitchIn, s: Session = Depends(get_session)):
    if not s.get(Project, project_id):
        raise HTTPException(404)
    if body.action == "freeze":
        killswitch.freeze(project_id, body.reason)
    else:
        killswitch.resume(project_id)
    return {"frozen": killswitch.is_frozen(project_id)}
