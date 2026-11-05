"""Panel asistanı uç noktaları: sohbet oturumları, mesaj gönderme (yanıt arka planda yazılır, panel oturumu yoklar) ve
asistanın önerdiği eylemlere onay. Onay kartı: "İzin ver" (yalnızca bu eylem), "Bu oturum için tümüne izin ver" (bu
sohbetteki sonraki eylemler sormadan başlar) ve "Vazgeç"."""
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import UUID_PATTERN, ProjectId, project_or_404
from app.assistant import actions, core
from app.config import settings
from app.db import get_session, session_scope
from app.models import AssistantAction, AssistantSession

router = APIRouter(tags=["assistant"])

SessionId = Annotated[str, Path(pattern=UUID_PATTERN)]
ActionId = Annotated[str, Path(pattern=UUID_PATTERN)]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=core.MAX_USER_CHARS)
    page: str | None = Field(None, max_length=40)
    scene_id: str | None = Field(None, pattern=UUID_PATTERN)


class ApproveIn(BaseModel):
    scope: Literal["once", "session"] = "once"


def _status() -> dict:
    """Asistan Claude'a bağlanabiliyor mu (anahtarın değeri okunmaz)."""
    from app.live import key_state
    if settings.dry_run:
        return {"available": False, "reason": "Ücretsiz mod: asistan Claude'u çağırmaz, yalnızca yerel durum özeti verir."}
    if not key_state("anthropic_api_key")["set"]:
        return {"available": False, "reason": "ANTHROPIC_API_KEY tanımlı değil (Ayarlar › Ücretli servisler)."}
    return {"available": True, "reason": None}


def _session_or_404(s: Session, session_id: str) -> AssistantSession:
    row = s.get(AssistantSession, session_id)
    if row is None:
        raise HTTPException(404, "Sohbet bulunamadı")
    return row


@router.get("/projects/{project_id}/assistant")
def assistant_state(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    rows = s.scalars(select(AssistantSession).where(AssistantSession.project_id == project_id)
                     .order_by(AssistantSession.updated_at.desc()).limit(30))
    return {**_status(), "model": settings.assistant_model,
            "sessions": [{"id": str(r.id), "title": r.title or "Yeni sohbet", "busy": r.busy,
                          "updated_at": r.updated_at.timestamp() if r.updated_at else None} for r in rows]}


@router.post("/projects/{project_id}/assistant/sessions")
def new_session(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    row = AssistantSession(id=uuid.uuid4(), project_id=project_id)
    s.add(row)
    s.commit()
    return core.view(s, str(row.id))


@router.get("/assistant/sessions/{session_id}")
def get_session_view(session_id: SessionId, s: Session = Depends(get_session)):
    _session_or_404(s, session_id)
    return core.view(s, session_id)


@router.post("/assistant/sessions/{session_id}/messages")
def send_message(session_id: SessionId, body: MessageIn, tasks: BackgroundTasks, s: Session = Depends(get_session)):
    """Mesajı kaydeder; yanıt arka planda yazılır (panel GET ile yoklar). Yanıt yazılırken 409."""
    _session_or_404(s, session_id)
    s.close()
    if not core.start(session_id, body.text, {"page": body.page, "scene_id": body.scene_id}):
        raise HTTPException(409, "Asistan önceki mesaja yanıt yazıyor; bitince yeniden gönder.")
    tasks.add_task(core.run, session_id)
    with session_scope() as fresh:
        return core.view(fresh, session_id)


@router.delete("/assistant/sessions/{session_id}")
def delete_session(session_id: SessionId, s: Session = Depends(get_session)):
    row = _session_or_404(s, session_id)
    if row.busy:
        raise HTTPException(409, "Asistan yanıt yazarken sohbet silinemez.")
    s.delete(row)
    s.commit()
    return {"ok": True}


def _pending(s: Session, action_id: str) -> AssistantAction:
    row = s.get(AssistantAction, action_id, with_for_update=True)
    if row is None:
        raise HTTPException(404, "Eylem bulunamadı")
    if row.status != "pending":
        raise HTTPException(409, "Bu eylem için karar zaten verildi.")
    return row


@router.post("/assistant/actions/{action_id}/approve")
def approve(action_id: ActionId, body: ApproveIn | None = None, s: Session = Depends(get_session),
            x_hg_session: str | None = Header(None, max_length=80)):
    """Yönetmenin onayı: eylem panelin kendi uç noktasıyla başlatılır. scope=session: bu sohbetteki sonraki eylemler de
    sormadan başlar (yönetmen istediği an kapatabilir)."""
    from app.curation import record
    row = _pending(s, action_id)
    scope = (body or ApproveIn()).scope
    if scope == "session":
        sess = s.get(AssistantSession, row.session_id)
        sess.allow_all = True
    record(str(row.project_id), "assistant_approve", str(row.scene_id) if row.scene_id else None,
           payload={"tool": row.tool, "summary": row.summary, "input": row.input, "scope": scope,
                    "estimate_usd": float(row.estimate_usd) if row.estimate_usd is not None else None})
    out = actions.execute(s, row, by="session" if scope == "session" else "approve", panel_session=x_hg_session)
    return {"action": core.action_view(s.get(AssistantAction, action_id)), "result": out}


@router.post("/assistant/actions/{action_id}/decline")
def decline(action_id: ActionId, s: Session = Depends(get_session)):
    row = _pending(s, action_id)
    actions.decline(s, row)
    return {"action": core.action_view(row)}


@router.post("/assistant/sessions/{session_id}/allow-all")
def set_allow_all(session_id: SessionId, on: bool = False, s: Session = Depends(get_session)):
    """"Tümüne izin ver"i kapatır (ya da açar): kapalıyken her ücretli eylem yeniden onay ister."""
    row = _session_or_404(s, session_id)
    row.allow_all = bool(on)
    s.commit()
    return {"allow_all": row.allow_all}
