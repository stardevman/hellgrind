"""Yönetici modu: Ayarlar (ücretli servisler, çalışma modu, fiyat tablosu) ve servis geçmişi yalnızca yöneticiye açıktır.

Şifre `.env`'deki ADMIN_PASSWORD'dür (değişmez kural 8: gizli değerler yalnızca .env'de durur); panelden yazılamaz. Boşsa
yönetici modu şifresiz açılır (yerel geliştirme). Giriş, süreli ve şifreye bağlı bir HMAC belirteciyle HttpOnly çereze
yazılır: sunucu oturum tutmaz, şifre değişince bütün oturumlar düşer. Şifre, panelin düzenlediği .env değiştikçe dosyadan
yeniden okunur; değiştirmek için yeniden başlatma gerekmez. Hatalı denemeler istemci adresi başına Redis'te sayılır.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

from app import runtime_config as rc
from app.config import settings

ENV = "ADMIN_PASSWORD"
COOKIE = "hg_admin"
TTL_S = 12 * 3600
FAIL_LIMIT = 10
FAIL_WINDOW_S = 15 * 60
FAIL_KEY = "hg:admin:fail:{}"

_cache: tuple[str, float, str] | None = None


def password() -> str:
    """Geçerli şifre: panelin düzenlediği .env okunabiliyorsa oradan (dosya değiştikçe), değilse ortamdan."""
    global _cache
    path = rc.env_path()
    if not path:
        return settings.admin_password
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return settings.admin_password
    if _cache is None or _cache[:2] != (path, mtime):
        _cache = (path, mtime, rc.read_env(path).get(ENV, ""))
    return _cache[2]


def required() -> bool:
    return bool(password())


def _sign(pw: str, exp: int) -> str:
    return hmac.new(pw.encode(), f"hg-admin:{exp}".encode(), hashlib.sha256).hexdigest()


def issue(now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + TTL_S)
    return f"{exp}.{_sign(password(), exp)}"


def valid(token: str | None, now: float | None = None) -> bool:
    pw = password()
    if not pw:
        return True
    exp, _, sig = (token or "").partition(".")
    if not exp.isdigit() or int(exp) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig, _sign(pw, int(exp)))


def check_password(given: str) -> bool:
    pw = password()
    return bool(pw) and hmac.compare_digest(given.encode(), pw.encode())


def is_admin(request: Request) -> bool:
    return valid(request.cookies.get(COOKIE))


def require_admin(request: Request) -> None:
    """Yönetici bölümlerinin (Ayarlar, servis geçmişi) yönlendirici bağımlılığı."""
    if not is_admin(request):
        raise HTTPException(401, "Bu bölüm yönetici modunda açılır: önce yönetici girişi yapın.")


def _redis():
    from app.budget import killswitch
    return killswitch.r()


def locked_out(client: str) -> bool:
    return int(_redis().get(FAIL_KEY.format(client)) or 0) >= FAIL_LIMIT


def record_failure(client: str) -> None:
    key = FAIL_KEY.format(client)
    pipe = _redis().pipeline()
    pipe.incr(key)
    pipe.expire(key, FAIL_WINDOW_S)
    pipe.execute()


def clear_failures(client: str) -> None:
    _redis().delete(FAIL_KEY.format(client))
