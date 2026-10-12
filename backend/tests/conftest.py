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

# Testler her zaman ücretsiz modda koşar (değişmez kural 2): panelden ücretli moda geçilmiş olsa ve konteyner .env'i
# DRY_RUN=0 ile okusa bile. Ücretli kodun testleri ayarı kendi içinde, sahte istemcilerle açar. Panelin .env'e yazması da
# testlerde kapalıdır: ayar testleri geçici bir dosya kullanır.
settings.dry_run = True
settings.config_env_file = ""

# Testler geliştirme .env'inden bağımsızdır: konteyner .env'i ortam değişkeni olarak yükler (ör. H3_ALLOW_NORDIC=true,
# RUNPOD_DATACENTER=EUR-NO-1, anahtarlar). Panelden düzenlenen bütün ayarlar varsayılana döner ve ortamdan silinir; böylece
# Settings() da varsayılanları görür. Ücretli testler sahte anahtarlarını kendileri verir (paid fikstürü).
from app import runtime_config as _rc  # noqa: E402
for _f in _rc.FIELDS:
    os.environ.pop(_f.env, None)
    if _f.kind != "mode":
        setattr(settings, _f.attr, type(settings).model_fields[_f.attr].default)
# H3 (RunPod) dönemi testleri: video fal.ai'ye taşınmadan önceki davranışı sınar (motor h3, kareler RunPod'da, platform
# runpod). Kod yolu duruyor ve seçilebilir; fal.ai varsayılanlarını (H3 Max taslak, Seedance 2.5 final, kareler fal'de,
# RunPod kapalı) `fal_defaults` fikstürü açar (tests/test_fal_engines.py).
LEGACY_VIDEO = {"h3_platform": "runpod", "keyframe_platform": "runpod", "video_draft_engine": "h3",
                "video_input_mode": "keyframe", "scene_render_mode": "per_shot"}
for _name, _value in LEGACY_VIDEO.items():
    setattr(settings, _name, _value)
# Prompt çevirisi testlerde kapalı: ücretli modu sahteyle açan testler yerel çeviri modelini yüklemesin (CI'da model yok).
# Çeviri testleri ayarı kendi içinde açar (tests/test_translate.py).
settings.prompt_translation = "off"

# Aynı nedenle nesne deposu da ayrı bir kova kullanır (geliştirme kovasındaki medya silinmesin).
settings.s3_bucket = "hellgrind-test"

# Konteyner dışında (ör. CI) varsayılan /app/pricing.yaml yoktur; repodaki dosyaya düş.
if not pathlib.Path(settings.pricing_file).exists():
    settings.pricing_file = str(pathlib.Path(__file__).resolve().parents[1] / "pricing.yaml")

# Bölüm A testleri varlık kütüphanesinden (Faz 12) önceki davranışı sınar: üretim öncesi referans kontrolü kapalıdır.
# Faz 12 testleri lint_strictness="block" (proje varsayılanı) ile açıkça çalışır.
BOLUM_A = {"lint_strictness": "off"}

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
        # Servis geçmişi proje silinince kalır (gerçek harcama kaydıdır); testlerinkiler burada silinir.
        conn.execute(text("DELETE FROM service_calls WHERE project_title LIKE 'pytest-%'"))


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
        cfg = {**BOLUM_A, **cfg}
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
            s.execute(text("DELETE FROM service_calls WHERE project_id = :p"), {"p": pid})
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


@pytest.fixture
def fast(monkeypatch, tmp_path, s3):
    """Gerçek render hattı, ama küçük kareler: her klip ~0.1 sn. Varsayılan: kusur yok, CLIP 0.95 (hepsi geçer)."""
    from app.engine import providers

    monkeypatch.setattr(providers, "RESOLUTIONS", {"480p": (160, 90), "720p": (160, 90)})
    for name, value in dict(local_render_fps=8, work_dir=str(tmp_path), dry_run_defect_rate=0.0,
                            dry_run_fail_rate=0.0, dry_run_score_mean=0.95, dry_run_score_sd=0.0,
                            gen_concurrency=2, purge_mode="quarantine").items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def sample_scene(mk_project, fast):
    """Örnek senaryodan ingest edilmiş bir proje; (proje, sahne kodu -> sahne kimliği) döndüren üretici.
    Ayarlar küçük tutulur: probe 3, tam tur 6 (dalga 3), havuz 2."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.hermes import rules
    from app.hermes.ingest import ingest
    from app.models import Scene
    from tests.scene_utils import add_scene_music

    def make(limit: float = 1000.0, **cfg):
        settings_ = {**dict(probe_n=3, full_n=6, wave_size=3, refined_k=2, max_correction_rounds=1,
                            max_spend_per_10min=10_000.0), **cfg}
        pid = mk_project(limit, **settings_)
        ingest(pid, rules.SAMPLE_SCRIPT, auto_music=False)
        add_scene_music(pid)                     # Bölüm A: her çekimin kendi müziği (sekans müziği Faz 11 testlerinde)
        with session_scope() as s:
            codes = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        return pid, codes

    return make


@pytest.fixture
def api(pg):
    """Uygulamaya süreç içinden istek atar. Yaşam döngüsü (migrate/ensure_bucket) çalıştırılmaz: bunu test altyapısı yapar."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def dispatched(monkeypatch):
    """Celery'ye hiçbir şey göndermez; planlanan zincirleri kaydeder: [(proje, sahne, ["VISUAL", ...]), ...].
    Testler geliştirme worker'ının kuyruğuna görev bırakmaz."""
    from app.core.rerender import stage_names

    class Jobs(list):
        modes: dict = {}                  # sahne -> son zincirin modu (yalnızca taslak/tam ayrımı gereken testler için)

    jobs = Jobs()
    jobs.modes = {}

    class Job:
        def __init__(self, pid, sid, stages, mode):
            self.record, self.mode = (pid, sid, stage_names(stages)), mode

        def apply_async(self):
            jobs.append(self.record)
            jobs.modes[self.record[1]] = self.mode

    monkeypatch.setattr("app.api.plan.build_chain",
                        lambda pid, sid, stages, mode="full": Job(pid, sid, stages, mode) if stages else None)

    def launch(batch):                    # toplu dağıtımın sırası kendi testlerinde (test_batching.py) sınanır
        for sid, stages, mode in batch.items:
            Job(batch.project_id, sid, stages, mode).apply_async()

    monkeypatch.setattr("app.worker.dispatch.Batch.launch", launch)
    monkeypatch.setattr("app.worker.tasks.assemble_timeline.delay", lambda pid: jobs.append((pid, None, ["ASSEMBLE"])))
    return jobs


@pytest.fixture
def eager(monkeypatch):
    """Celery görevleri istek içinde, aynı süreçte ve eşzamanlı çalışır (uçtan uca testler için)."""
    from app.worker.celery_app import celery

    monkeypatch.setattr(celery.conf, "task_always_eager", True)
    monkeypatch.setattr(celery.conf, "task_eager_propagates", True)


@pytest.fixture
def api_project(api, pg):
    """API üzerinden proje oluşturur (başlık "pytest-" ile başlar); test bitince projeyi ve Redis anahtarlarını siler."""
    from sqlalchemy import text

    from app.budget import killswitch as ks
    from app.db import session_scope

    created: list[str] = []

    def make(limit: float = 60.0, **cfg) -> str:
        cfg = {**BOLUM_A, **cfg}
        r = api.post("/api/projects", json={"title": f"pytest-{uuid.uuid4().hex[:8]}", "budget_limit_usd": limit, "settings": cfg})
        assert r.status_code == 201, r.text
        created.append(r.json()["id"])
        return created[-1]

    yield make

    for pid in created:
        ks.r().delete(*(k.format(pid) for k in (ks.FROZEN, ks.SOFT, ks.TASKS, ks.EVENTS, ks.RESUMED_AT)))
        with session_scope() as s:
            s.execute(text("DELETE FROM service_calls WHERE project_id = :p"), {"p": pid})
            s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


@pytest.fixture
def no_network(monkeypatch):
    """Ücretli modül testlerinde yanlışlıkla genel internete bağlantı kurulursa test patlar (para harcanamaz).
    Yerel/özel ağ (Postgres, Redis, MinIO: Docker ağı, loopback) serbesttir; bunlar ücretli servis değildir."""
    import ipaddress
    import socket

    real_connect = socket.socket.connect

    def refuse(self, address, *a, **k):
        if isinstance(address, tuple):
            host = address[0]
            try:
                ip = ipaddress.ip_address(host)
                local = ip.is_private or ip.is_loopback or ip.is_link_local
            except ValueError:                                  # ad: yalnızca bilinen yerel servis adları
                local = host in {"postgres", "redis", "minio", "localhost"}
            if not local:
                raise AssertionError(f"Testte genel internete bağlantı denendi: {address}")
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def paid(monkeypatch, no_network):
    """Ücretli modun kilidini YALNIZCA BELLEKTEKİ ayar nesnesinde açar ve sahte anahtarlar verir. Ortam değişkeni DRY_RUN=1
    kalır, genel internete bağlantı yasaktır (no_network) ve ücretli sağlayıcı çağrıları testlerde sahtedir: gerçek servise
    hiçbir istek gitmez."""
    monkeypatch.setattr(settings, "dry_run", False)
    for name in ("runpod_api_key", "vast_api_key", "h3_api_key", "fal_key", "anthropic_api_key", "elevenlabs_api_key"):
        monkeypatch.setattr(settings, name, f"test-{name}")
    monkeypatch.setattr(settings, "vast_instance_id", "")       # makine yönetimi kapalı: testler vast.ai'yi sahteler


@pytest.fixture
def h3(monkeypatch, tmp_path, paid, s3):
    """Ücretli mod + sahte MiniMax H3 sunucusu (httpx MockTransport; ağa çıkılmaz). Sunucu 4 sn'lik gerçek bir test
    videosu döndürür ve her iş için 42 sn GPU süresi bildirir."""
    from app.live import vast_h3
    from tests.h3_fake import FakeH3
    from tests.media_utils import make_video
    video = make_video(tmp_path / "h3_source.mp4", seconds=4.0, size="320x180", fps=24)
    fake = FakeH3(pathlib.Path(video).read_bytes())
    monkeypatch.setattr(vast_h3, "TRANSPORT", fake.transport())
    monkeypatch.setattr(vast_h3, "POLL_S", 0)
    monkeypatch.setattr(vast_h3, "RETRY_BASE_S", 0)
    monkeypatch.setattr(settings, "h3_platform", "vast")              # bu sahte vast.ai'nin H3 sunucusu (deneysel arka uç)
    monkeypatch.setattr(settings, "h3_endpoint_url", "https://h3.test")
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "")
    monkeypatch.setattr(settings, "h3_backend", "sglang")
    monkeypatch.setattr(settings, "h3_comfy_workflow_dir", str(tmp_path / "comfy"))
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    vast_h3._reference_uris.clear()
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:                                   # çıktı kaydı ve sertifika sabitlemesi testten teste taşınmasın
        s.execute(text("DELETE FROM h3_outputs"))
        s.execute(text("DELETE FROM h3_tls_pins"))
    from app.live import vast_box                                # önceki testin makine kaydı sızmasın
    for name in ("inflight", "last_activity", "last_project", "tick", "box", "starting", "proposal", "replacement",
                 "replacing", "retrieval", "tls_fallback", "tls_mismatch"):
        vast_box._r().delete(vast_box._k(name))
    return fake


@pytest.fixture
def give_keyframe(tmp_path):
    """Çekime yönetmenin ilk karesini verir (MiniMax H3 FL2VA'nın kompozisyon koşulu): gerçek, düz olmayan bir görsel."""
    def make(scene_id: str) -> str:
        import numpy as np
        from PIL import Image
        from app import storage
        from app.db import session_scope
        from app.models import Scene
        path = tmp_path / f"kf_{scene_id}.jpg"
        Image.fromarray(np.random.default_rng(3).integers(0, 255, (180, 320, 3), dtype=np.uint8)).save(path)
        key = f"keyframes/{scene_id}/test.jpg"
        storage.put_file(str(path), key, "image/jpeg")
        with session_scope() as s:
            sc = s.get(Scene, scene_id)
            sc.keyframe_key, sc.keyframe_hash = key, "test-keyframe"
        return key
    return make


@pytest.fixture
def refined(sample_scene):
    """Köprü sahnesi (diyalog + müzik), görsel aşaması bitmiş: (proje, sahne)."""
    from app.engine.iteration import run_tiered_generation

    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=2)
    sid = codes["S002_SH01"]
    assert run_tiered_generation(pid, sid) == "refined"
    return pid, sid


@pytest.fixture(autouse=True)
def _no_real_seam_models(monkeypatch):
    """Kadraj ölçeği, varış skoru ve durum sondası gerçek modelleri (OpenCLIP + DINOv2, ~3,6 GB) yükler: skorlayıcıyı "real" moda alan
    testlerde de varsayılan olarak ölçülmez (None). Bunları sınayan test kendi değerini monkeypatch ile verir."""
    from app.quality import scorer
    monkeypatch.setattr(scorer, "framing", lambda path, target: None)
    monkeypatch.setattr(scorer, "arrival", lambda path, key, target: None)
    monkeypatch.setattr(scorer, "state_probe_path", lambda path, probes: {})      # durum sondası (OpenCLIP + kırpım)
    monkeypatch.setattr(scorer, "state_probe", lambda image, probes: {})
    monkeypatch.setattr(scorer, "continuity", lambda path, previous: None)       # zincir karesi mekân benzerliği
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: None)    # jump cut (OpenCV homografi)
    monkeypatch.setattr(scorer, "clip_motion", lambda path, movement: None)      # klipte kamera hareketi
    monkeypatch.setattr(scorer, "face_counts", lambda path, step=0.5: None)    # videoda yüz sayımı (YuNet)
    monkeypatch.setattr(scorer, "size_ratio", lambda path, target, name: None)  # ölçek kapısı
    monkeypatch.setattr(scorer, "cut_in_view", lambda previous, path, rect=None: None)  # kırp + iyileştir

    def centre_box(image, target, names=()):                                      # kırp + iyileştir: karakter kutusu
        w, h = image.size
        return {"name": (list(names) or [""])[0], "kind": "patch", "box": (int(w * .36), int(h * .31), int(w * .64),
                int(h * .78)), "fill": 0.65, "coverage": 0.095}          # gerçek SH03 kutusu oranında (yakın plan ~2×)
    monkeypatch.setattr(scorer, "character_box", centre_box)
