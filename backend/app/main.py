import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp

from app import migrate, storage
from app.api import assets, budget, export, grade, methods, panel, projects, scenes, sequences, storyboard, style
from app.config import settings

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi", lifespan=lifespan)
for module in (projects, scenes, methods, grade, sequences, storyboard, assets, style, budget, export, panel):
    app.include_router(module.router, prefix="/api")
app.mount("/panel", panel_app(pathlib.Path(settings.panel_web_dir)), name="panel")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel/")


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run}
