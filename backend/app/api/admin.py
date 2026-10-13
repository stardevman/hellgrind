"""Yönetici girişi: panelin Kullanıcı / Yönetici anahtarı. Ayarlar ve servis geçmişi uçları `admin.require_admin`
bağımlılığıyla korunur (bkz. app.admin)."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field as PField

from app import admin

router = APIRouter(tags=["admin"])


class LoginIn(BaseModel):
    password: str = PField(max_length=400)


def _state(is_admin: bool) -> dict:
    return {"required": admin.required(), "admin": is_admin}


@router.get("/admin")
def status(request: Request):
    return _state(admin.is_admin(request))


@router.post("/admin/login")
def login(body: LoginIn, request: Request, response: Response):
    if not admin.required():
        return _state(True)
    client = request.client.host if request.client else "?"
    if admin.locked_out(client):
        raise HTTPException(429, "Çok fazla hatalı deneme: 15 dakika sonra yeniden deneyin.")
    if not admin.check_password(body.password):
        admin.record_failure(client)
        raise HTTPException(401, "Şifre yanlış.")
    admin.clear_failures(client)
    response.set_cookie(admin.COOKIE, admin.issue(), max_age=admin.TTL_S, path="/api", httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return _state(True)


@router.post("/admin/logout")
def logout(response: Response):
    response.delete_cookie(admin.COOKIE, path="/api")
    return _state(not admin.required())
