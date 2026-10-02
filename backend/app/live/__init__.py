"""ÜCRETLİ entegrasyonlar.

Buradaki hiçbir kod DRY_RUN=1 iken çalışmaz; her modül çağrıldığında önce guard() ile bunu doğrular.
Testler ve duman testi bu klasörü hiç içe aktarmaz. Bağımlılıklar: requirements-live.txt
"""
from app.config import settings


class PaidCallBlocked(RuntimeError):
    pass


def guard(key_name: str) -> str:
    if settings.dry_run:
        raise PaidCallBlocked("DRY_RUN=1: ücretli çağrı engellendi.")
    key = getattr(settings, key_name, "")
    if not key:
        raise PaidCallBlocked(f"{key_name.upper()} tanımlı değil; ücretli çağrı yapılmadı.")
    return key
