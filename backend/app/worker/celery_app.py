from celery import Celery
from celery.signals import worker_process_init
from kombu import Queue

from app.config import settings

celery = Celery("hellgrind", broker=settings.redis_url, include=["app.worker.tasks"])

celery.conf.update(
    task_ignore_result=True,
    task_acks_late=True,                 # worker çökerse görev kuyruğa geri döner
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_queues=[Queue("gen"), Queue("post"), Queue("control")],
    task_default_queue="control",
    task_routes={
        "hg.generate_scene": {"queue": "gen"},
        "hg.motion_test": {"queue": "gen"},
        "hg.lipsync_scene": {"queue": "post"},
        "hg.mux_scene": {"queue": "post"},
        "hg.assemble_timeline": {"queue": "post"},
        "hg.*": {"queue": "control"},
    },
    broker_transport_options={"visibility_timeout": 6 * 3600},
    timezone="UTC",
    beat_schedule={
        "budget-watchdog": {"task": "hg.watchdog", "schedule": 30.0},
        "reap-stale-reservations": {"task": "hg.reap_stale", "schedule": 300.0},
    },
)


@worker_process_init.connect
def _reset_db_pool(**_):
    # Fork sonrası ebeveynden kalan bağlantıları paylaşma
    from app.db import engine
    engine.dispose(close=False)
