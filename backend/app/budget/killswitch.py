"""Bütçe koruması.

Kural: hiçbir ücretli çağrı, bütçeden atomik bir rezervasyon almadan yapılmaz.
  reserve()  -> harcamadan ÖNCE tahmini tutarı ayırır (limit aşılacaksa reddeder ve projeyi dondurur)
  commit()   -> gerçek maliyeti yazar, rezervasyonu düşer
  release()  -> çağrı başarısızsa rezervasyonu iade eder
"""
import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

import redis
from sqlalchemy import text

from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.db import session_scope

log = logging.getLogger("hellgrind.killswitch")

FROZEN = "hg:frozen:{}"
SOFT = "hg:soft:{}"
TASKS = "hg:tasks:{}"
EVENTS = "hg:events:{}"
RESUMED_AT = "hg:resumed_at:{}"       # hız bekçisi yalnızca bu andan sonraki harcamayı sayar
VELOCITY_WINDOW_S = 600


class ProjectFrozen(Exception):
    """Proje dondurulmuş; yeni ücretli iş başlatılamaz."""


class BudgetExceeded(ProjectFrozen):
    """Rezervasyon bütçe limitini aşardı; Kill-Switch tetiklendi."""


@lru_cache(maxsize=1)
def r() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def event(project_id: str, kind: str, **data) -> None:
    key = EVENTS.format(project_id)
    r().lpush(key, json.dumps({"ts": time.time(), "kind": kind, **data}))
    r().ltrim(key, 0, 199)


# ---------- durum ----------

def is_frozen(project_id: str) -> bool:
    if r().exists(FROZEN.format(project_id)):
        return True
    with session_scope() as s:
        status = s.execute(text("SELECT status FROM projects WHERE id = :p"), {"p": project_id}).scalar()
    return status in ("paused", "killed")


def is_soft_limited(project_id: str) -> bool:
    return bool(r().exists(SOFT.format(project_id)))


def has_running_tasks(project_id: str) -> bool:
    """Projede şu an çalışan (başlamış, bitmemiş) görev var mı. Çöken worker'ın bıraktığı sahne durumunu ayırt eder."""
    return bool(r().scard(TASKS.format(project_id)))


def register_task(project_id: str, task_id: str | None) -> None:
    if task_id:
        r().sadd(TASKS.format(project_id), task_id)
        r().expire(TASKS.format(project_id), 86400)


def unregister_task(project_id: str, task_id: str | None) -> None:
    if task_id:
        r().srem(TASKS.format(project_id), task_id)


# ---------- dondur / devam ----------

def freeze(project_id: str, reason: str) -> None:
    """Kill-Switch: bayrağı kaldır, projeyi duraklat, kuyruktaki görevleri iptal et."""
    r().set(FROZEN.format(project_id), reason)
    with session_scope() as s:
        s.execute(text("UPDATE projects SET status = 'paused' WHERE id = :p AND status NOT IN ('killed','completed')"),
                  {"p": project_id})
        s.execute(text("""UPDATE scenes SET status = 'frozen'
                          WHERE project_id = :p AND status IN ('pending','probing','generating','correcting',
                                                               'audio','lipsync','muxing')"""),
                  {"p": project_id})
    task_ids = list(r().smembers(TASKS.format(project_id)))
    if task_ids:
        from app.worker.celery_app import celery  # döngüsel importu önlemek için geç import
        celery.control.revoke(task_ids)           # kuyrukta bekleyenler hiç başlamaz
    event(project_id, "frozen", reason=reason, revoked=len(task_ids))
    log.warning("KILL-SWITCH project=%s reason=%s revoked=%d", project_id, reason, len(task_ids))


def resume(project_id: str) -> None:
    """freeze'in tersi. Hız penceresi de sıfırlanır: yönetmenin bilinçli kararı, dondurmaya yol açan eski
    harcamayla ezilmez (yoksa bekçi 30 sn içinde aynı 10 dakikalık toplamı görüp yeniden dondururdu)."""
    r().delete(FROZEN.format(project_id), SOFT.format(project_id))
    r().set(RESUMED_AT.format(project_id), time.time(), ex=2 * VELOCITY_WINDOW_S)
    with session_scope() as s:
        s.execute(text("UPDATE projects SET status = 'producing' WHERE id = :p AND status = 'paused'"),
                  {"p": project_id})
        s.execute(text("UPDATE scenes SET status = 'pending' WHERE project_id = :p AND status = 'frozen'"),
                  {"p": project_id})
    event(project_id, "resumed")


# ---------- rezervasyon ----------

def reserve(project_id: str, amount: float, provider: str, scene_id=None, variation_id=None) -> uuid.UUID:
    if is_frozen(project_id):
        raise ProjectFrozen(project_id)
    rid = uuid.uuid4()
    with session_scope() as s:
        row = s.execute(text("""
            UPDATE projects SET reserved_usd = reserved_usd + :amt
            WHERE id = :p AND status NOT IN ('paused','killed')
              AND spent_usd + reserved_usd + :amt <= budget_limit_usd
            RETURNING spent_usd, reserved_usd, budget_limit_usd
        """), {"p": project_id, "amt": amount}).first()
        if row is not None:
            s.execute(text("""INSERT INTO cost_ledger (project_id, scene_id, variation_id, provider, kind, amount_usd, reservation)
                              VALUES (:p, :s, :v, :prov, 'reserve', :amt, :rid)"""),
                      {"p": project_id, "s": scene_id, "v": variation_id, "prov": provider, "amt": amount, "rid": rid})
    if row is None:
        freeze(project_id, f"budget_limit (istek {amount:.4f} USD reddedildi)")
        raise BudgetExceeded(project_id)
    return rid


def commit(project_id: str, rid: uuid.UUID, estimate: float, actual: float, provider: str,
           scene_id=None, variation_id=None) -> None:
    with session_scope() as s:
        row = s.execute(text("""
            UPDATE projects SET reserved_usd = GREATEST(reserved_usd - :est, 0), spent_usd = spent_usd + :act
            WHERE id = :p RETURNING spent_usd, budget_limit_usd, settings
        """), {"p": project_id, "est": estimate, "act": actual}).first()
        s.execute(text("""INSERT INTO cost_ledger (project_id, scene_id, variation_id, provider, kind, amount_usd, reservation)
                          VALUES (:p, :s, :v, :prov, 'commit', :amt, :rid)"""),
                  {"p": project_id, "s": scene_id, "v": variation_id, "prov": provider, "amt": actual, "rid": rid})
        if scene_id:
            s.execute(text("UPDATE scenes SET spent_usd = spent_usd + :a WHERE id = :s"), {"a": actual, "s": scene_id})
    spent, limit, cfg = float(row[0]), float(row[1]), {**DEFAULT_PROJECT_SETTINGS, **(row[2] or {})}
    if spent >= limit * cfg["soft_budget_ratio"] and not is_soft_limited(project_id):
        r().set(SOFT.format(project_id), "1")
        event(project_id, "soft_limit", spent=spent, limit=limit)
    if spent >= limit:
        freeze(project_id, "budget_exhausted")


def release(project_id: str, rid: uuid.UUID, estimate: float, provider: str, scene_id=None, variation_id=None) -> None:
    with session_scope() as s:
        s.execute(text("UPDATE projects SET reserved_usd = GREATEST(reserved_usd - :est, 0) WHERE id = :p"),
                  {"p": project_id, "est": estimate})
        s.execute(text("""INSERT INTO cost_ledger (project_id, scene_id, variation_id, provider, kind, amount_usd, reservation)
                          VALUES (:p, :s, :v, :prov, 'release', :amt, :rid)"""),
                  {"p": project_id, "s": scene_id, "v": variation_id, "prov": provider, "amt": estimate, "rid": rid})


@dataclass
class Charge:
    estimate: float
    actual: float | None = None


@contextmanager
def spend(project_id: str, estimate: float, provider: str, scene_id=None, variation_id=None):
    """Kullanım:
        with spend(pid, 0.40, "fal_wan:720p") as charge:
            result = provider.generate(...)
            charge.actual = result.cost_usd
    """
    rid = reserve(project_id, estimate, provider, scene_id, variation_id)
    charge = Charge(estimate)
    try:
        yield charge
    except BaseException:
        release(project_id, rid, estimate, provider, scene_id, variation_id)
        raise
    commit(project_id, rid, estimate, charge.actual if charge.actual is not None else estimate,
           provider, scene_id, variation_id)


# ---------- bekçiler (Celery beat) ----------

def watchdog() -> list[str]:
    """Harcama hızı denetimi: 10 dakikada izin verilenden fazla harcayan projeyi dondurur.
    Pencere, projenin son `resume` anından önceki harcamayı saymaz."""
    tripped = []
    with session_scope() as s:
        projects = s.execute(text("SELECT id, settings FROM projects WHERE status = 'producing'")).all()
        recent = {}
        for pid, _ in projects:
            resumed_at = r().get(RESUMED_AT.format(pid))
            recent[pid] = float(s.execute(text("""
                SELECT COALESCE(SUM(amount_usd), 0) FROM cost_ledger
                WHERE project_id = :p AND kind = 'commit'
                  AND created_at > GREATEST(now() - make_interval(secs => :w),
                                            COALESCE(to_timestamp(CAST(:from AS double precision)), '-infinity'::timestamptz))
            """), {"p": pid, "w": VELOCITY_WINDOW_S, "from": float(resumed_at) if resumed_at else None}).scalar())
    for pid, cfg in projects:
        limit = {**DEFAULT_PROJECT_SETTINGS, **(cfg or {})}["max_spend_per_10min"]
        if recent[pid] > limit:
            freeze(str(pid), f"velocity ({recent[pid]:.2f} USD / 10 dk > {limit})")
            tripped.append(str(pid))
    return tripped


def reap_stale_reservations(max_age_minutes: int = 60) -> int:
    """Çöken worker'ların bıraktığı açık rezervasyonları iade eder."""
    with session_scope() as s:
        stale = s.execute(text("""
            SELECT l.project_id, l.reservation, l.amount_usd, l.provider, l.scene_id
            FROM cost_ledger l
            WHERE l.kind = 'reserve' AND l.created_at < now() - make_interval(mins => :m)
              AND NOT EXISTS (SELECT 1 FROM cost_ledger x
                              WHERE x.reservation = l.reservation AND x.kind IN ('commit','release'))
        """), {"m": max_age_minutes}).all()
    for pid, rid, amt, prov, sid in stale:
        release(str(pid), rid, float(amt), prov, sid)
    return len(stale)
