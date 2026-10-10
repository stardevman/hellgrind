import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import storage
from app.api import budget, projects, scenes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi (Kademe 1)", lifespan=lifespan)
for module in (projects, scenes, budget):
    app.include_router(module.router, prefix="/api")

# Stüdyo paneli: aynı /api uç noktalarını kullanan, derleme gerektirmeyen basit arayüz.
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")


@app.get("/healthz")
def healthz():
    from app.config import settings
    return {"ok": True, "dry_run": settings.dry_run}
