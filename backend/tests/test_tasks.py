"""Görev sarmalayıcısı (_guard), beat bağlantısı ve özetlerin veritabanı gidiş-dönüşünde kararlılığı."""
import uuid
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text

from app.budget import killswitch as ks
from app.core.rerender import ALL, Stage, plan_rerender
from app.db import session_scope
from app.models import AudioTrack, Character, Scene, SceneCharacter


def scene_row(sid):
    with session_scope() as s:
        return s.execute(text("SELECT status, last_error FROM scenes WHERE id = :s"), {"s": sid}).one()


def fake_task(task_id="task-123"):
    from app.budget import killswitch
    from app.worker.tasks import DELIVERIES
    killswitch.r().delete(DELIVERIES.format(task_id))       # her test görevi ilk kez teslim edilmiş sayılır
    return NS(request=NS(id=task_id))


# ---------- kayıt ----------

def test_tasks_are_registered_under_the_names_the_routes_use():
    from app.worker import tasks  # noqa: F401  (görevleri kaydeder)
    from app.worker.celery_app import celery

    assert {"hg.generate_scene", "hg.lipsync_scene", "hg.mux_scene", "hg.assemble_timeline",
            "hg.watchdog", "hg.reap_stale"} <= set(celery.tasks)


def test_beat_schedule_runs_watchdog_every_30s_and_stale_reaper_every_5min():
    from app.worker import tasks  # noqa: F401
    from app.worker.celery_app import celery

    schedule = celery.conf.beat_schedule
    assert schedule["budget-watchdog"] == {"task": "hg.watchdog", "schedule": 30.0}
    assert schedule["reap-stale-reservations"] == {"task": "hg.reap_stale", "schedule": 300.0}
    assert {v["task"] for v in schedule.values()} <= set(celery.tasks)


# ---------- _guard ----------

def test_guard_does_not_start_work_for_a_frozen_project(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid, "pending")
    ks.freeze(pid, "test")
    calls = []

    assert _guard(fake_task(), pid, sid, lambda: calls.append(1)) == "frozen"
    assert calls == []
    assert scene_row(sid)[0] == "frozen"


def test_guard_registers_the_task_while_it_runs_and_returns_its_result(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)
    seen = []

    def work():
        seen.append(ks.r().sismember(ks.TASKS.format(pid), "task-123"))
        return "ok"

    assert _guard(fake_task(), pid, sid, work) == "ok"
    assert seen == [True]
    assert not ks.r().sismember(ks.TASKS.format(pid), "task-123")


def test_guard_records_last_error_fails_the_scene_and_reraises(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)

    def boom():
        raise ValueError("ffmpeg çöktü")

    with pytest.raises(ValueError):
        _guard(fake_task(), pid, sid, boom)
    status, last_error = scene_row(sid)
    assert status == "failed" and last_error.startswith("ValueError: ffmpeg çöktü")
    assert not ks.r().sismember(ks.TASKS.format(pid), "task-123")


def test_guard_turns_a_budget_stop_into_a_frozen_scene_not_a_failure(mk_project, mk_scene):
    from app.worker.tasks import _guard
    pid = mk_project()
    sid = mk_scene(pid)

    def over_budget():
        raise ks.BudgetExceeded(pid)

    assert _guard(fake_task(), pid, sid, over_budget) == "frozen"
    status, last_error = scene_row(sid)
    assert status == "frozen" and last_error is None


def test_stage_tasks_pass_only_ids_to_the_work_modules(monkeypatch, mk_project, mk_scene):
    """Görev gövdeleri iş modüllerini görev içinde yükler ve yalnızca kimlikle çağırır."""
    import sys
    import types

    from app.worker import tasks
    pid = mk_project()
    sid = mk_scene(pid)
    calls = []
    iteration = types.ModuleType("app.engine.iteration")
    iteration.run_tiered_generation = lambda p, s, mode="full": calls.append(("generate", p, s)) or "generated"
    pipeline = types.ModuleType("app.post.pipeline")
    pipeline.voice_and_lipsync = lambda s: calls.append(("lipsync", s)) or "lipsynced"
    pipeline.mux_scene = lambda s: calls.append(("mux", s)) or "muxed"
    for name, module in (("app.engine", types.ModuleType("app.engine")), ("app.engine.iteration", iteration),
                         ("app.post", types.ModuleType("app.post")), ("app.post.pipeline", pipeline)):
        monkeypatch.setitem(sys.modules, name, module)

    assert tasks.generate_scene.apply(kwargs={"project_id": pid, "scene_id": sid}).get() == "generated"
    assert tasks.lipsync_scene.apply(kwargs={"project_id": pid, "scene_id": sid}).get() == "lipsynced"
    assert tasks.mux_scene.apply(kwargs={"project_id": pid, "scene_id": sid}).get() == "muxed"
    assert calls == [("generate", pid, sid), ("lipsync", sid), ("mux", sid)]


# ---------- özetler veritabanından geri okununca ----------

def test_fingerprints_survive_a_database_round_trip(mk_project):
    """JSONB anahtar sırası, float biçimi ve UUID'ler yüzünden "değişmemiş" sahne değişmiş görünmemeli."""
    pid = mk_project()
    cid, sid = uuid.uuid4(), uuid.uuid4()
    with session_scope() as s:
        s.add(Character(id=cid, project_id=pid, name="Kerem", pinecone_namespace=f"pytest-{cid}",
                        lora_url="https://x/lora.safetensors", lora_scale=0.85, embedding_version=3))
        s.add(Scene(id=sid, project_id=pid, scene_code="S001_SH01", seq_order=1, engine="seedance",
                    duration_s=4.5, camera={"shot": "wide", "movement": "pan", "angle": "eye", "lens_mm": 24},
                    prompt_spec={"prompt": "köprü", "negative": "bulanık", "layers": [1, 2]}))
        s.flush()
        s.add(SceneCharacter(scene_id=sid, character_id=cid))
        s.add_all([
            AudioTrack(scene_id=sid, character_id=cid, kind="dialogue", source="espeak", text="Merhaba",
                       content_hash="d1", start_offset_s=0.0, gain_db=0.0),
            AudioTrack(scene_id=sid, kind="music", source="procedural", content_hash="m1",
                       start_offset_s=0.0, gain_db=-8.0),
        ])

    def load():
        with session_scope() as s:
            sc = s.get(Scene, sid)
            chars = list(s.query(Character).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                         .filter(SceneCharacter.scene_id == sid))
            tracks = list(s.query(AudioTrack).filter(AudioTrack.scene_id == sid))
            s.expunge_all()
            return sc, chars, tracks

    sc, chars, tracks = load()
    stages, fp = plan_rerender(sc, chars, tracks)
    assert stages == ALL
    with session_scope() as s:
        s.execute(text("""UPDATE scenes SET visual_hash = :v, dialogue_hash = :d, bed_hash = :b,
                          lipsync_video_key = 'l.mp4', final_video_key = 'f.mp4' WHERE id = :s"""),
                  {"v": fp.visual, "d": fp.dialogue, "b": fp.bed, "s": sid})

    sc, chars, tracks = load()
    assert plan_rerender(sc, chars, tracks)[0] == Stage.NONE

    with session_scope() as s:
        s.execute(text("UPDATE characters SET embedding_version = 4 WHERE id = :c"), {"c": cid})
    sc, chars, tracks = load()
    assert plan_rerender(sc, chars, tracks)[0] == ALL
