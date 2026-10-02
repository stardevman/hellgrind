import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import migrate, storage
from app.api import budget, projects, scenes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi (Kademe 1)", lifespan=lifespan)
for module in (projects, scenes, budget):
    app.include_router(module.router, prefix="/api")


@app.get("/healthz")
def healthz():
    from app.config import settings
    return {"ok": True, "dry_run": settings.dry_run}
