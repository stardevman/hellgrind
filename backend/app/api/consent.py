"""Ücretli ek işlemler için izin (app/budget/consent.py): bekleyen istekler, izin ver, oturum için tümüne izin ver, reddet.

Panel bekleyen istekleri birkaç saniyede bir okur ve izin penceresini açar. İstek içinde (eşzamanlı) yapılan çağrı izin
beklerse uç nokta 428 döner (gövdede `consent`); panel izni alınca isteği yineler. Kararlar kürasyon kaydına yazılır.
"""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.budget import consent
from app.config import settings

router = APIRouter(tags=["consent"])


class GrantIn(BaseModel):
    scope: Literal["once", "session"] = "once"
    session_id: str | None = Field(default=None, max_length=80)
    by: str = Field(default="panel", max_length=40)


class DenyIn(BaseModel):
    by: str = Field(default="panel", max_length=40)


class AllowIn(BaseModel):
    """Bekleyen istek olmadan önceden izin (ör. panel asistanı, eylemi başlatmadan hemen önce)."""
    project_id: str
    kind: Literal["lipsync", "image", "voice", "llm", "upscale", "other"]
    scene_id: str | None = None
    asset_id: str | None = None
    scope: Literal["once", "session"] = "once"
    session_id: str | None = Field(default=None, max_length=80)
    by: str = Field(default="panel", max_length=40)


@router.get("/consent")
def consent_state(project_id: str | None = None):
    """Bekleyen istekler (proje verilirse yalnızca onunkiler), oturum izninin durumu ve türlerin Türkçe adları."""
    return {"paid_mode": not settings.dry_run, "pending": consent.pending(project_id),
            "session": consent.session_state(), "kinds": consent.KINDS}


@router.post("/consent/{request_id}/grant")
def grant(request_id: str, body: GrantIn):
    if body.scope == "session" and not body.session_id:
        raise HTTPException(422, "Oturum izni için panel oturumunun kimliği gerekli.")
    if body.scope == "once" and consent.get(request_id) is None:
        raise HTTPException(404, "Bu izin isteği artık beklemiyor (süresi doldu ya da karar verildi).")
    return consent.grant([request_id], body.scope, body.session_id, body.by)


@router.post("/consent/{request_id}/deny")
def deny(request_id: str, body: DenyIn | None = None):
    if consent.get(request_id) is None:
        raise HTTPException(404, "Bu izin isteği artık beklemiyor (süresi doldu ya da karar verildi).")
    return consent.deny([request_id], (body or DenyIn()).by)


@router.post("/consent/allow")
def allow(body: AllowIn):
    if body.scope == "session" and not body.session_id:
        raise HTTPException(422, "Oturum izni için panel oturumunun kimliği gerekli.")
    target = f"scene:{body.scene_id}" if body.scene_id else f"asset:{body.asset_id}" if body.asset_id else "project"
    consent.allow(body.project_id, body.kind, target, body.scope, body.session_id, body.by)
    return {"allowed": True, "target": target, "scope": body.scope, "session": consent.session_state()}


@router.post("/consent/session/revoke")
def revoke_session():
    """Oturum için verilen toplu izni kaldırır: sonraki her ücretli ek işlem yeniden sorulur."""
    return {"revoked": consent.revoke_session(), "session": consent.session_state()}
