"""Basit migration çalıştırıcı: backend/migrations/*.sql dosyalarını sırayla ve bir kez uygular.

001 şeması PostgreSQL ilk açılışında db/init üzerinden kurulur; sonraki değişiklikler buradan gelir,
böylece mevcut veritabanını silmeden güncelleyebilirsin.

Her dosya kendi işleminde uygulanır (oturum düzeyinde advisory lock altında): `ALTER TYPE ... ADD VALUE` ile eklenen
bir enum değeri, eklendiği işlem kesinleşmeden kullanılamaz; sonraki bir migration onu güvenle kullanabilsin.
"""
import logging
import pathlib

from sqlalchemy import text

from app.db import engine

log = logging.getLogger("hellgrind.migrate")
DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 424242


def run() -> list[str]:
    applied = []
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_ID})
        conn.commit()
        try:
            conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                              "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
            done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
            conn.commit()
            for f in sorted(DIR.glob("*.sql")):
                if f.name in done:
                    continue
                conn.exec_driver_sql(f.read_text())
                conn.execute(text("INSERT INTO schema_migrations (name) VALUES (:n)"), {"n": f.name})
                conn.commit()
                applied.append(f.name)
                log.info("migration uygulandı: %s", f.name)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
            conn.commit()
    return applied
