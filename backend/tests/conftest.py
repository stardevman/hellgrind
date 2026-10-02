import json
import os
import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import settings  # noqa: E402

TEST_DB = "hellgrind_test"
# Testler geliştirme veritabanına asla dokunmaz: hız bekçisi ve bayat rezervasyon toplayıcı tüm projeleri tarar.
# app.db içe aktarılmadan önce ayarlanmalı; bu yüzden conftest yüklenirken yapılır.
settings.database_url = make_url(settings.database_url).set(database=TEST_DB).render_as_string(hide_password=False)

# Aynı nedenle nesne deposu da ayrı bir kova kullanır (geliştirme kovasındaki medya silinmesin).
settings.s3_bucket = "hellgrind-test"

# Konteyner dışında (ör. CI) varsayılan /app/pricing.yaml yoktur; repodaki dosyaya düş.
if not pathlib.Path(settings.pricing_file).exists():
    settings.pricing_file = str(pathlib.Path(__file__).resolve().parents[1] / "pricing.yaml")

INIT_SQL_CANDIDATES = (
    pathlib.Path(__file__).resolve().parents[2] / "db" / "init" / "001_core.sql",
    pathlib.Path("/db/init/001_core.sql"),
)


@pytest.fixture(scope="session")
def pg():
    """Test veritabanını hazırlar (yoksa oluşturur, şemayı kurar). Ulaşılamıyorsa DB testleri atlanır."""
    import psycopg
    import redis
    from sqlalchemy import text

    url = make_url(settings.database_url)
    admin_dsn = url.set(database="postgres", drivername="postgresql").render_as_string(hide_password=False)
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                c.execute(f'CREATE DATABASE "{TEST_DB}"')
        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3).ping()
    except (psycopg.OperationalError, redis.exceptions.RedisError) as e:
        pytest.skip(f"PostgreSQL/Redis erişilemiyor, DB testleri atlandı: {e}")

    from app import migrate
    from app.db import engine
    with engine.begin() as conn:
        if conn.execute(text("SELECT to_regclass('public.projects')")).scalar() is None:
            init_sql = next((p for p in INIT_SQL_CANDIDATES if p.exists()), None)
            if init_sql is None:
                pytest.skip("db/init/001_core.sql bulunamadı (konteynerde -v ./db:/db:ro ile bağla)")
            conn.exec_driver_sql(init_sql.read_text())
    migrate.run()
    yield
    with engine.begin() as conn:             # yalnızca test veritabanı: reddedilmesi gerekirken oluşan artıklar dahil
        conn.execute(text("DELETE FROM projects WHERE title LIKE 'pytest-%'"))


@pytest.fixture(scope="session")
def s3(pg):
    """Test kovasını hazırlar; oturum sonunda içindeki nesneleri siler (yalnızca test kovası)."""
    from botocore.exceptions import BotoCoreError, ClientError

    from app import storage

    try:
        storage.ensure_bucket()
    except (BotoCoreError, ClientError) as e:
        pytest.skip(f"Nesne deposu (MinIO) erişilemiyor, depo testleri atlandı: {e}")
    yield
    client = storage.client()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": objects})


@pytest.fixture
def mk_project(pg):
    """Test projesi üretir; test bitince projeyi (cascade ile sahne ve defter kayıtlarını) ve Redis anahtarlarını siler."""
    import json

    from sqlalchemy import text

    from app.budget import killswitch as ks
    from app.db import session_scope

    created: list[str] = []

    def make(limit: float = 100.0, status: str = "producing", **cfg) -> str:
        pid = str(uuid.uuid4())
        with session_scope() as s:
            s.execute(text("""INSERT INTO projects (id, title, status, budget_limit_usd, settings)
                              VALUES (:i, :t, CAST(:st AS project_status), :l, CAST(:cfg AS jsonb))"""),
                      {"i": pid, "t": f"pytest-{pid[:8]}", "st": status, "l": limit, "cfg": json.dumps(cfg)})
        created.append(pid)
        return pid

    yield make

    for pid in created:
        ks.r().delete(*(k.format(pid) for k in (ks.FROZEN, ks.SOFT, ks.TASKS, ks.EVENTS, ks.RESUMED_AT)))
        with session_scope() as s:
            s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


@pytest.fixture
def mk_scene(pg):
    from sqlalchemy import text

    from app.db import session_scope

    counter = iter(range(1, 1000))

    def make(project_id: str, status: str = "generating", duration_s: float = 5.0, prompt: str = "test") -> str:
        sid, n = str(uuid.uuid4()), next(counter)
        with session_scope() as s:
            s.execute(text("""INSERT INTO scenes (id, project_id, scene_code, seq_order, status, duration_s, prompt_spec)
                              VALUES (:i, :p, :c, :n, CAST(:st AS scene_status), :d, CAST(:ps AS jsonb))"""),
                      {"i": sid, "p": project_id, "c": f"S{n:03d}_SH01", "n": n, "st": status, "d": duration_s,
                       "ps": json.dumps({"prompt": prompt})})
        return sid

    return make
