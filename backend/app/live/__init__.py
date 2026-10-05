"""Ücretli entegrasyonların kilidi.

Ücretli kod yalnızca bu pakette durur ve yalnızca fonksiyon içinde, tembel import ile yüklenir. Her giriş noktası
önce `guard(anahtar_adı)` çağırır: DRY_RUN=1 iken ya da anahtar boşken hiçbir şey çalışmaz.
"""
import importlib.util
import sys

from app.config import settings

PAID_KEYS = ("fal_key", "anthropic_api_key", "elevenlabs_api_key")
# Servis başına ücretli paketler (requirements-live.txt). ElevenLabs yalnızca httpx kullanır.
PACKAGES = {"fal": ("fal_client",), "anthropic": ("langgraph", "langchain_anthropic"), "elevenlabs": ()}


class PaidCallBlocked(RuntimeError):
    """Ücretli çağrı engellendi: ücretsiz mod açık ya da anahtar tanımlı değil."""


def guard(key_name: str) -> str:
    """Anahtarı yalnızca ücretli mod açıkken ve anahtar doluyken verir; aksi halde PaidCallBlocked fırlatır.

    Ayarlar çağrı anında okunur. DRY_RUN=1 iken anahtar tanımlı olsa bile engellenir (değişmez kural 1)."""
    if key_name not in PAID_KEYS:
        raise ValueError(f"Bilinmeyen ücretli anahtar: {key_name!r} (geçerli: {', '.join(PAID_KEYS)})")
    if settings.dry_run:
        raise PaidCallBlocked(f"{key_name}: DRY_RUN=1 iken ücretli çağrı yapılmaz.")
    value = getattr(settings, key_name)
    if not value:
        raise PaidCallBlocked(f"{key_name.upper()} tanımlı değil: .env dosyasına ekleyin.")
    return value


def key_state(key_name: str) -> dict:
    """Panel için anahtarın durumu: tanımlı mı ve maskeli hâli (son 4 hane). Değerin kendisi asla dönmez."""
    if key_name not in PAID_KEYS:
        raise ValueError(f"Bilinmeyen ücretli anahtar: {key_name!r}")
    value = getattr(settings, key_name) or ""
    return {"set": bool(value), "masked": ("••••" + value[-4:] if len(value) > 8 else "••••") if value else None}


def probe_key(key_name: str) -> str:
    """Yalnızca bağlantı sınaması (app.live.probe) için: anahtarı DRY_RUN'dan bağımsız verir. Sınama ücretsiz, salt okunur
    uçlara gider ve üretim yapmaz; ücretli çağrılar her zaman guard() üzerinden geçer."""
    if key_name not in PAID_KEYS:
        raise ValueError(f"Bilinmeyen ücretli anahtar: {key_name!r}")
    return getattr(settings, key_name) or ""


def redact(message: str) -> str:
    """Kayda yazılacak metindeki anahtar değerlerini maskeler (servis geçmişindeki hata metinleri için)."""
    for key_name in PAID_KEYS:
        value = getattr(settings, key_name) or ""
        if len(value) >= 8:
            message = message.replace(value, "••••")
    return message


def _installed(module: str) -> bool:
    return module in sys.modules or importlib.util.find_spec(module) is not None


def missing_packages(service: str) -> list[str]:
    """Servisin kurulu olmayan ücretli paketleri. Paketler yüklenmez, yalnızca bulunup bulunmadıkları bakılır."""
    return [m for m in PACKAGES[service] if not _installed(m)]


def require_package(module: str) -> None:
    """Ücretli paket kurulu değilse anlaşılır bir hatayla durur (ham ModuleNotFoundError yerine)."""
    if not _installed(module):
        raise PaidCallBlocked(f"Ücretli paket kurulu değil: {module}. İmajı yeniden derleyin (docker compose up -d --build) "
                              "ya da pip install -r requirements-live.txt çalıştırın.")
