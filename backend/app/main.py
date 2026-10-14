import json
import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp

from app import migrate, runtime_config, storage
from app.api import (admin, assets, budget, delivery, export, finishing, grade, history, methods, panel, projects, render_plan, scenes,
                     sequences, review, services, storyboard, style, voice)
from app.budget import history as service_history, killswitch
from app.config import settings
from app.db import session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
PANEL_MISSING = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Hell Grind — Panel derlenmemiş</title>
<style>body{margin:0;padding:32px 16px;background:#2A2D31;color:#E6E1D8;font:15px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto}a{color:#86A5B8}code{background:#33373C;padding:1px 5px;border-radius:3px}</style></head>
<body><main><h1>Panel derlenmemiş</h1>
<p>Next.js paneli bu kurulumda bulunamadı. Docker imajı paneli kendisi derler: <code>docker compose up -d --build</code>.
Yerelde derlemek için <code>make web-build</code> (ardından <code>PANEL_WEB_DIR=frontend/out</code>), geliştirme sunucusu için
<code>make web-dev</code>. API bu arada çalışır: <a href="/docs">/docs</a>.</p></main></body></html>"""


class PanelFiles(StaticFiles):
    """Sayfalar (HTML ve RSC verisi) her açılışta yeniden doğrulanır: imaj yenilenince tarayıcı eski paneli göstermesin.
    _next/static altındaki dosyaların adı içeriklerinin özetidir; onlar süresiz önbelleğe alınır."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            immutable = path.startswith("_next/static/")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


def panel_app(web_dir: pathlib.Path) -> ASGIApp:
    """Next.js panelinin statik dışa aktarımı; derlenmemişse nasıl derleneceğini anlatan açıklama sayfası."""
    if (web_dir / "index.html").is_file():
        return PanelFiles(directory=web_dir, html=True)

    async def missing(scope, receive, send):
        await HTMLResponse(PANEL_MISSING, status_code=503)(scope, receive, send)
    return missing


def complete_service_history() -> None:
    """Servis geçmişi sisteminden önceki kayıtların modunu ayar değişiklik kaydından tamamlar (bir kez). Kayıt okunamazsa
    (Redis yok) sonraki açılışa kalır: yanlış modla doldurmaktansa beklemek iyidir."""
    try:
        entries = [json.loads(x) for x in killswitch.r().lrange(runtime_config.AUDIT_KEY, 0, -1)]
        with session_scope() as s:
            service_history.complete_backfill(s, entries)
    except Exception:                                           # noqa: BLE001 — açılışı engellemez
        logging.getLogger("hellgrind").warning("eski servis kayıtları tamamlanamadı", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()
    complete_service_history()
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi", lifespan=lifespan)
for module in (projects, scenes, render_plan, finishing, methods, grade, sequences, storyboard, assets, style, budget, export, services,
               history, panel, admin, voice, review, delivery):
    app.include_router(module.router, prefix="/api")
app.include_router(review.public_router, prefix="/api")     # müşterinin belirteçli uçları (dışarıya app.review_main açar)
app.mount("/panel", panel_app(pathlib.Path(settings.panel_web_dir)), name="panel")


@app.middleware("http")
async def config_refresh(request, call_next):
    """Ayarlar başka bir süreçte (panel, worker) değiştiyse istekten önce .env'den tazelenir."""
    if request.url.path.startswith("/api"):
        await run_in_threadpool(runtime_config.refresh_if_changed)
    return await call_next(request)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel/")


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run}
