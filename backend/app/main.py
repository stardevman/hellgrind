import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import migrate, storage
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()
    storage.ensure_bucket()
    yield


app = FastAPI(title="Hell Grind — Kontrol Düzlemi", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "dry_run": settings.dry_run}
