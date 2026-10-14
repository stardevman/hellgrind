"""Yönetici modu: Ayarlar ve servis geçmişi uçları şifre tanımlıyken yalnızca yönetici çereziyle açılır."""
import os

import pytest

from app import admin
from app.config import settings

PASSWORD = "gizli-sifre-123"


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    admin.clear_failures("testclient")
    yield
    admin.clear_failures("testclient")


def test_without_a_password_admin_mode_is_open(api):
    assert api.get("/api/admin").json() == {"required": False, "admin": True}
    assert api.get("/api/settings").status_code == 200
    assert api.get("/api/history/summary").status_code == 200
    assert api.post("/api/admin/login", json={"password": ""}).json() == {"required": False, "admin": True}


def test_settings_and_history_need_the_admin_cookie(api, locked):
    assert api.get("/api/admin").json() == {"required": True, "admin": False}
    for path in ("/api/settings", "/api/settings/pricing", "/api/settings/runpod", "/api/history", "/api/history/summary",
                 "/api/history.csv"):
        r = api.get(path)
        assert r.status_code == 401, path
        assert "yönetici" in r.json()["detail"]
    assert api.patch("/api/settings", json={"values": {}}).status_code == 401
    assert api.post("/api/settings/mode", json={"paid": True, "confirm": "ücretli moda geç"}).status_code == 401
    # Kullanıcı modunun uçları şifreden etkilenmez
    assert api.get("/api/meta").status_code == 200
    assert api.get("/api/projects").status_code == 200


def test_login_opens_admin_mode_and_logout_closes_it(api, locked):
    wrong = api.post("/api/admin/login", json={"password": "yanlis"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Şifre yanlış."
    assert admin.COOKIE not in api.cookies

    r = api.post("/api/admin/login", json={"password": PASSWORD})
    assert r.json() == {"required": True, "admin": True}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
    assert PASSWORD not in r.headers["set-cookie"]
    assert api.get("/api/admin").json()["admin"] is True
    assert api.get("/api/settings").status_code == 200

    assert api.post("/api/admin/logout").json() == {"required": True, "admin": False}
    assert api.get("/api/settings").status_code == 401


def test_a_password_change_or_expiry_ends_the_session(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", PASSWORD)     # belirteç saf fonksiyondur: Redis gerekmez
    token = admin.issue(now=1000)
    assert admin.valid(token, now=1000 + admin.TTL_S - 1)
    assert not admin.valid(token, now=1000 + admin.TTL_S + 1)
    assert not admin.valid("", now=1000) and not admin.valid("abc.def", now=1000) and not admin.valid(None, now=1000)
    monkeypatch.setattr(settings, "admin_password", "yeni-sifre-456")
    assert not admin.valid(token, now=1000)


def test_password_is_read_from_the_edited_env_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("DRY_RUN=1\n")
    monkeypatch.setattr(settings, "config_env_file", str(env))
    monkeypatch.setattr(settings, "admin_password", "ortamdaki-eski-sifre")
    assert not admin.required()                         # dosya okunabiliyorsa tek kaynak odur
    env.write_text('DRY_RUN=1\nADMIN_PASSWORD="dosyadaki-sifre"\n')
    os.utime(env, (1, 1))                               # mtime değişsin (aynı saniyede iki yazım)
    assert admin.password() == "dosyadaki-sifre"


def test_repeated_failures_lock_the_login(api, locked):
    for _ in range(admin.FAIL_LIMIT):
        assert api.post("/api/admin/login", json={"password": "yanlis"}).status_code == 401
    r = api.post("/api/admin/login", json={"password": PASSWORD})
    assert r.status_code == 429 and admin.COOKIE not in api.cookies


def test_every_settings_and_history_route_is_admin_only(api, locked):
    import re

    from app.main import app

    guarded = [(method, re.sub(r"\{[^}]*\}", "x", path)) for path, ops in app.openapi()["paths"].items()
               if path.startswith(("/api/settings", "/api/history")) for method in ops]
    assert len(guarded) > 15
    for method, path in guarded:
        assert api.request(method.upper(), path).status_code == 401, (method, path)
