"""Basit migration çalıştırıcı: backend/migrations/*.sql dosyalarını sırayla ve bir kez uygular.

001 şeması PostgreSQL ilk açılışında db/init üzerinden kurulur; sonraki değişiklikler buradan gelir,
böylece mevcut veritabanını silmeden güncelleyebilirsin.
"""
import logging
import pathlib

from sqlalchemy import text

from app.db import engine

log = logging.getLogger("hellgrind.migrate")
DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def run() -> list[str]:
    applied = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(424242)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                          "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
        done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
        for f in sorted(DIR.glob("*.sql")):
            if f.name in done:
                continue
            conn.exec_driver_sql(f.read_text())
            conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": f.name})
            applied.append(f.name)
            log.info("migration uygulandı: %s", f.name)
    return applied
