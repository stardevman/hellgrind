"""Müşteri inceleme uygulaması: dışarıya açılan tek kapı.

Stüdyo uygulaması (app.main, 8000) giriş istemeyen uçlar taşır: proje ve bütçe verisi, render başlatma, üretimi
durdurma, onay kapısını panelden işaretleme. Müşteriye o port açılırsa belirteçli inceleme sayfası güvenli olsa bile
adres tahminiyle geri kalanına ulaşılır. Bu uygulama (8100) yalnızca şunları sunar:
  - /api/review/{token}… (belirteçli, salt okunur uçlar; bütçe, motor, maliyet döndürmez)
  - /panel/review/ sayfası ve onun statik dosyaları (_next/static, yazı tipleri)
Başka her yol 404'tür. Şema kurulumu ve servis geçmişi stüdyo uygulamasının işidir; burada yapılmaz.
"""
import pathlib

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from starlette._utils import get_route_path
from starlette.types import Receive, Scope, Send

from app import runtime_config
from app.api import review
from app.config import settings
from app.main import PanelFiles

PAGE = "review"
# Panelin statik dışa aktarımında inceleme sayfasının ihtiyaç duyduğu yollar (basePath /panel altında).
ALLOWED_PREFIXES = ("review/", "review.txt", "_next/static/", "favicon")


class ReviewFiles:
    """Statik panelden yalnızca inceleme sayfasını ve onun varlıklarını geçirir; stüdyo sayfaları 404 döner."""

    def __init__(self, web_dir: pathlib.Path):
        self.files = PanelFiles(directory=web_dir, html=True, check_dir=False) if (web_dir / PAGE).is_dir() else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = get_route_path(scope).lstrip("/")          # bağlama noktasından (/panel) sonrası
        allowed = (path.startswith(ALLOWED_PREFIXES) or path == PAGE) and ".." not in path.split("/")
        if self.files is None or not allowed:
            await Response("Bulunamadı", status_code=404, media_type="text/plain; charset=utf-8")(scope, receive, send)
            return
        await self.files(scope, receive, send)


app = FastAPI(title="Hell Grind — Müşteri incelemesi", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(review.public_router, prefix="/api")
app.mount("/panel", ReviewFiles(pathlib.Path(settings.panel_web_dir)), name="review-panel")


@app.middleware("http")
async def config_refresh(request, call_next):
    """Depo ve veritabanı ayarları .env'den tazelenir (stüdyo uygulamasıyla aynı)."""
    if request.url.path.startswith("/api"):
        await run_in_threadpool(runtime_config.refresh_if_changed)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel/review/")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
