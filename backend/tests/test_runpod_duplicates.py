"""Yinelenen RunPod gönderimi (2026-10-09 ücretli koşusu): worker süreci bellekten öldürülünce Celery görevi yeniden teslim
etti; görev baştan çalışıp aynı işleri yeniden gönderdi, /status'un 404'ü "uç nokta yok" sanıldı. Burada:
- aynı iş anahtarı için bekleyen iş varken yenisi gönderilmez (yeniden teslim aynı işi bekler);
- durum okunamazsa (404, zaman aşımı) iş yeniden gönderilmez; UNKNOWN_LIMIT sorgudan sonra çekim hata notuyla durur;
- projede aynı anda en çok runpod_max_pending_jobs iş bekler;
- Kill-Switch RunPod'daki bekleyen işleri iptal eder, rezervasyonlar iade edilir;
- çok kez yeniden teslim edilen görev bir daha çalışmaz.
Sahte RunPod (ağ yok)."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.budget import killswitch, pricing
from app.config import settings
from app.db import session_scope
from app.engine import paid
from app.live import runpod_h3
from tests.test_runpod import request, runpod  # noqa: F401  (sahte RunPod fikstürü)


THREADS: list[threading.Thread] = []


def until(cond, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("koşul zamanında sağlanmadı")
        time.sleep(0.02)


def background(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run():
        try:
            out["result"] = fn()
        except BaseException as e:            # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    THREADS.append(t)
    return t, out


def reserved(pid) -> float:
    with session_scope() as s:
        return float(s.execute(text("SELECT reserved_usd FROM projects WHERE id = :p"), {"p": pid}).scalar())


@pytest.fixture
def slow(runpod, monkeypatch):  # noqa: F811
    """İşler kuyrukta bekler (bitmez) ve durum sorgusu hızlıdır."""
    monkeypatch.setattr(runpod_h3, "POLL_S", 0.01)
    runpod.polls_to_finish = 10 ** 9
    yield runpod
    runpod.polls_to_finish = 1                       # artakalan iş parçacıkları sahte RunPod bağlıyken bitsin
    for t in THREADS:
        t.join(30)
    alive = [t for t in THREADS if t.is_alive()]
    THREADS.clear()
    assert not alive, "test bittiğinde RunPod'u sorgulayan iş parçacığı kaldı"


def test_a_redelivered_task_waits_for_the_same_job_instead_of_resubmitting(slow, mk_project):
    pid = mk_project(10.0)
    req = request(extra={"project_id": pid})
    first, a = background(lambda: runpod_h3.get_live_provider().generate(req))
    until(lambda: len(slow.runs) == 1)
    again, b = background(lambda: runpod_h3.get_live_provider().generate(req))      # yeniden teslim: yeni süreç
    time.sleep(0.3)
    assert len(slow.runs) == 1 and list(runpod_h3.pending(pid)) == ["job-1-e1"]
    slow.polls_to_finish = 1
    first.join(30), again.join(30)
    assert "error" not in a and "error" not in b
    assert a["result"].request_id == b["result"].request_id == "job-1-e1" and len(slow.runs) == 1
    assert runpod_h3.pending(pid) == {}


def test_the_status_query_uses_the_job_id_verbatim(runpod):  # noqa: F811
    runpod_h3.get_live_provider().generate(request())
    polled = [str(q.url) for q in runpod.requests if "/status/" in str(q.url)]
    assert polled and all(u.endswith("/status/job-1-e1") for u in polled)


def test_an_unknown_job_is_never_resubmitted_and_the_shot_stops_with_a_note(runpod, mk_project, monkeypatch):  # noqa: F811
    """404 (RunPod işi tanımıyor: kuyruk boşaltıldı) "uç nokta yok" sayılmaz; iş yeniden gönderilmez."""
    monkeypatch.setattr(runpod_h3, "POLL_S", 0)
    pid = mk_project(10.0)
    runpod.expired.add("job-1-e1")
    req = request(extra={"project_id": pid})
    with pytest.raises(runpod_h3.JobLost, match="12 sorguda okunamadı .*404"):
        paid.call(pid, pricing.estimate_video("h3", "480p", 4), "h3:480p", "runpod_api_key",
                  lambda: runpod_h3.get_live_provider().generate(req))
    statuses = [q for q in runpod.requests if "/status/" in str(q.url)]
    assert len(runpod.runs) == 1 and len(statuses) == runpod_h3.UNKNOWN_LIMIT       # paid.call yeniden denemedi
    assert runpod_h3.pending(pid) == {} and reserved(pid) == 0                    # rezervasyon iade edildi


def test_a_status_timeout_is_waited_out_without_resubmitting(runpod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runpod_h3, "POLL_S", 0)
    real, flaky = runpod_h3.status, {"n": 3}

    def status(c, job_id):
        if flaky["n"]:
            flaky["n"] -= 1
            raise httpx.ReadTimeout("zaman aşımı")
        return real(c, job_id)
    monkeypatch.setattr(runpod_h3, "status", status)
    result = runpod_h3.get_live_provider().generate(request())
    assert result.request_id == "job-1-e1" and len(runpod.runs) == 1


def test_a_project_never_has_more_than_the_pending_limit(slow, mk_project, monkeypatch):
    monkeypatch.setattr(settings, "runpod_max_pending_jobs", 2)
    pid = mk_project(10.0)
    threads = [background(lambda seed=seed: runpod_h3.get_live_provider().generate(
        request(seed=seed, extra={"project_id": pid}))) for seed in (1, 2, 3, 4)]
    until(lambda: len(slow.runs) == 2)
    time.sleep(0.3)
    assert len(slow.runs) == 2 and len(runpod_h3.pending(pid)) == 2
    kinds = [json.loads(x)["kind"] for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    assert "runpod_queue_full" in kinds
    slow.polls_to_finish = 1                                         # işler bitince sıradakiler gönderilir
    for t, out in threads:
        t.join(30)
        assert "error" not in out
    assert len(slow.runs) == 4 and runpod_h3.pending(pid) == {}


def test_the_kill_switch_cancels_pending_runpod_jobs_and_releases_reservations(slow, mk_project):
    pid = mk_project(10.0)
    threads = [background(lambda seed=seed: paid.call(
        pid, pricing.estimate_video("h3", "480p", 4), "h3:480p", "runpod_api_key",
        lambda: runpod_h3.get_live_provider().generate(request(seed=seed, extra={"project_id": pid}))))
        for seed in (1, 2, 3)]
    until(lambda: len(runpod_h3.pending(pid)) == 3)
    assert reserved(pid) > 0
    killswitch.freeze(pid, "panelden durduruldu")
    assert sorted(slow.cancelled) == ["job-1-e1", "job-2-e1", "job-3-e1"]
    for t, out in threads:
        t.join(30)
        assert isinstance(out.get("error"), killswitch.ProjectFrozen)
    assert len(slow.runs) == 3 and runpod_h3.pending(pid) == {} and reserved(pid) == 0


def test_cancel_is_free_mode_safe(mk_project, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    assert killswitch.cancel_remote(mk_project(1.0)) == 0               # ücretsiz modda RunPod'a hiçbir istek gitmez


# ---------- yeniden teslim ----------

def test_a_task_killed_again_and_again_is_abandoned_and_its_reservations_are_released(sample_scene):
    from app.models import Scene
    from app.worker import tasks
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    rid = killswitch.reserve(pid, 0.05, "h3:480p", scene_id=sid)          # yarıda kesilen denemenin rezervasyonu
    task = SimpleNamespace(request=SimpleNamespace(id=f"t-{sid}"), name="hg.generate_scene")
    calls = []
    assert tasks._guard(task, pid, sid, lambda: calls.append(1) or "refined") == "refined"
    assert reserved(pid) == pytest.approx(0.05)
    assert tasks._guard(task, pid, sid, lambda: calls.append(1) or "refined") == "refined"   # 2. teslim
    assert reserved(pid) == 0                                              # açık rezervasyon iade edildi
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM cost_ledger WHERE reservation = :r AND kind = 'release'"),
                         {"r": rid}).scalar() == 1
    tasks._guard(task, pid, sid, lambda: calls.append(1) or "refined")
    with pytest.raises(tasks.TaskAbandoned):
        tasks._guard(task, pid, sid, lambda: calls.append(1) or "refined")
    assert len(calls) == 3
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.status == "failed" and "3 kez yarıda kesildi" in sc.last_error
