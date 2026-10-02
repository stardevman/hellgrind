import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import migrate, storage
from app.api import assets, budget, panel, projects, scenes
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
PANEL_DIR = pathlib.Path(__file__).resolve().parent / "panel"


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi", lifespan=lifespan)
for module in (projects, scenes, assets, budget, panel):
    app.include_router(module.router, prefix="/api")
app.mount("/panel", StaticFiles(directory=PANEL_DIR, html=True), name="panel")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel/")


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run}
