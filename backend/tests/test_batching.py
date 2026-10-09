"""Toplu dağıtım: birden çok sahne birlikte işlenirken önce bütün storyboard kareleri (H3 makinesinde görsel modeli), sonra
bütün videolar (H3) kuyruğa girer; GPU'da model her çekimde değil bir kez değişir. Bariyer Redis'tedir."""
import uuid

import pytest
from app.budget import killswitch
from app.core.rerender import Stage, stage_names
from app.db import session_scope
from app.models import Scene
from app.worker import dispatch


@pytest.fixture
def queued(monkeypatch):
    """Celery'ye hiçbir şey gönderilmez; kuyruğa verilen zincirler sırasıyla kaydedilir."""
    chains = []

    class Chain:
        def __init__(self, pid, sid, stages, mode, batch, standalone):
            self.record = {"scene": sid, "stages": stage_names(stages), "batch": batch, "standalone": standalone, "mode": mode}

        def apply_async(self):
            chains.append(self.record)

    def build(pid, sid, stages, mode="full", batch=None, standalone=None):
        return Chain(pid, sid, stages, mode, batch, standalone) if stages else None

    monkeypatch.setattr("app.worker.dispatch.build_chain", build)
    monkeypatch.setattr("app.api.plan.build_chain", lambda pid, sid, stages, mode="full": build(pid, sid, stages, mode))
    yield chains


def batch_of(chains) -> str:
    (bid,) = {c["batch"] for c in chains if c["batch"]}
    return bid


def test_render_all_queues_every_storyboard_before_any_video(sample_scene, queued):
    from app.api.plan import dispatch_all
    pid, codes = sample_scene()
    with session_scope() as s:
        result = dispatch_all(s, pid)
    assert result["batched"] is True
    assert [c["stages"] for c in queued] == [["STORYBOARD"]] * 4
    assert all(c["standalone"] is False for c in queued)              # video sonra gelecek: kare onaya bırakılmaz
    bid = batch_of(queued)
    for sid in codes.values():                                         # sahneler toplu dağıtımın kilidinde
        assert dispatch.batch_holding(sid)
    for i, sid in enumerate(codes.values()):
        dispatch.batch_done(bid, sid, True)
        if i < 3:
            assert len(queued) == 4                                    # son kare bitmeden video yok
    videos = queued[4:]
    assert sorted(c["scene"] for c in videos) == sorted(codes.values())
    assert all(c["stages"][0] == "VISUAL" and "STORYBOARD" not in c["stages"] for c in videos)
    dispatch.batch_done(bid, list(codes.values())[0], True)            # geç gelen kayıt: ikinci kez kuyruğa verilmez
    assert len(queued) == 8


def test_a_failed_storyboard_drops_only_its_own_video(sample_scene, queued):
    from app.api.plan import dispatch_all
    pid, codes = sample_scene()
    with session_scope() as s:
        dispatch_all(s, pid)
    bid, ids = batch_of(queued), list(codes.values())
    dispatch.batch_done(bid, ids[0], False)
    assert not dispatch.batch_holding(ids[0])
    for sid in ids[1:]:
        dispatch.batch_done(bid, sid, True)
    assert sorted(c["scene"] for c in queued[4:]) == sorted(ids[1:])


def test_a_deferred_scene_cannot_be_queued_twice(sample_scene, queued):
    from app.api.plan import Busy, dispatch_all, plan_and_dispatch
    pid, codes = sample_scene()
    with session_scope() as s:
        dispatch_all(s, pid)
        sc = s.get(Scene, codes["S001_SH01"])
        sc.status = "pending"
        s.commit()
        with pytest.raises(Busy, match="toplu üretimde"):
            plan_and_dispatch(s, sc, force_visual=True)


def test_freezing_drops_the_batch_so_no_video_is_queued_after_it(sample_scene, queued):
    from app.api.plan import dispatch_all
    pid, codes = sample_scene()
    with session_scope() as s:
        dispatch_all(s, pid)
    bid, ids = batch_of(queued), list(codes.values())
    killswitch.freeze(pid, "test")
    assert all(not dispatch.batch_holding(sid) for sid in ids)
    for sid in ids:
        dispatch.batch_done(bid, sid, True)                            # dondurmadan önce başlamış kareler bitse de
    assert len(queued) == 4


def test_without_storyboard_work_chains_go_straight_to_the_queue(queued):
    b = dispatch.Batch(str(uuid.uuid4()))
    b.add("s1", Stage.VISUAL | Stage.MUX)
    b.add("s2", Stage.MUX)
    assert b.launch() is None
    assert [c["stages"] for c in queued] == [["VISUAL", "MUX"], ["MUX"]] and all(c["batch"] is None for c in queued)


def test_videos_wait_even_for_scenes_whose_board_is_already_drawn(queued, mk_project, mk_scene):
    pid = mk_project()
    s1, s2, s3, s4 = (mk_scene(pid, status="pending") for _ in range(4))
    b = dispatch.Batch(pid)
    b.add(s1, Stage.STORYBOARD | Stage.VISUAL | Stage.MUX)
    b.add(s2, Stage.VISUAL | Stage.MUX)                                # karesi güncel: yine de karelerden sonra
    b.add(s3, Stage.MUX)                                               # yalnızca montaj: beklemez
    b.add(s4, Stage.STORYBOARD)                                        # onay bekleyen kare: kare aşamasının parçası
    bid = b.launch()
    assert [(c["scene"], c["stages"]) for c in queued] == [(s3, ["MUX"]), (s1, ["STORYBOARD"]), (s4, ["STORYBOARD"])]
    assert queued[2]["batch"] == bid and queued[1]["standalone"] is False and queued[2]["standalone"] is None
    dispatch.batch_done(bid, s1, True)
    assert len(queued) == 3 and dispatch.batch_holding(s2)
    dispatch.batch_done(bid, s4, True)
    assert sorted((c["scene"], tuple(c["stages"])) for c in queued[3:]) == sorted([(s1, ("VISUAL", "MUX")), (s2, ("VISUAL", "MUX"))])


def test_a_frozen_or_failed_scene_is_not_queued_when_the_boards_finish(queued, mk_project, mk_scene):
    pid = mk_project()
    s1, s2 = mk_scene(pid, status="pending"), mk_scene(pid, status="failed")
    b = dispatch.Batch(pid)
    b.add(s1, Stage.STORYBOARD | Stage.VISUAL)
    b.add(s2, Stage.VISUAL)
    bid = b.launch()
    dispatch.batch_done(bid, s1, True)
    assert [c["scene"] for c in queued[1:]] == [s1] and not dispatch.batch_holding(s2)


def test_the_storyboard_task_reports_to_the_barrier_even_when_it_fails(pg, monkeypatch):
    from app.worker import tasks
    seen = []
    monkeypatch.setattr("app.worker.dispatch.batch_done", lambda batch, sid, ok: seen.append((batch, sid, ok)))
    monkeypatch.setattr("app.engine.storyboard.make_board", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kare")))
    pid, sid = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        tasks.storyboard_scene(pid, sid, batch="b1")
    monkeypatch.setattr("app.engine.storyboard.make_board", lambda *a, **k: "key")
    tasks.storyboard_scene(pid, sid, batch="b1")
    assert seen == [("b1", sid, False), ("b1", sid, True)]


def test_end_to_end_every_board_is_drawn_before_the_first_video(sample_scene, eager, monkeypatch):
    """Görevler aynı süreçte çalışır: çizim ve üretim sırası kaydedilir (gerçek render yerine)."""
    order = []
    monkeypatch.setattr("app.engine.storyboard.make_board", lambda pid, sid, standalone=False, **k: order.append(("board", sid)))
    monkeypatch.setattr("app.engine.iteration.run_tiered_generation", lambda pid, sid, mode="full": order.append(("video", sid)))
    for name in ("voice_and_lipsync", "grade_scene", "mux_scene"):
        monkeypatch.setattr(f"app.post.pipeline.{name}", lambda sid: None)
    monkeypatch.setattr("app.worker.tasks.maybe_assemble_draft", lambda pid, sid: None)
    monkeypatch.setattr("app.worker.tasks.maybe_refresh_continuity", lambda pid, sid: None)
    from app.api.plan import dispatch_all
    pid, codes = sample_scene()
    with session_scope() as s:
        dispatch_all(s, pid)
    assert [k for k, _ in order] == ["board"] * 4 + ["video"] * 4
    assert {sid for _, sid in order} == set(codes.values())


def test_videos_are_grouped_by_model_fl2va_then_ref2va(queued, mk_project, mk_scene):
    """Aynı worker'da model değişimi (Qwen -> FL2VA -> Ref2VA) her seferinde modeli yeniden yükler: kuyruk gruplanır."""
    pid = mk_project()
    a, b, c, d = (mk_scene(pid, status="pending") for _ in range(4))
    batch = dispatch.Batch(pid)
    batch.add(a, Stage.VISUAL | Stage.MUX, model="ref2va")
    batch.add(b, Stage.STORYBOARD | Stage.VISUAL | Stage.MUX, model="fl2va")
    batch.add(c, Stage.VISUAL | Stage.MUX, model="fl2va")
    batch.add(d, Stage.VISUAL | Stage.MUX, model="ref2va")
    bid = batch.launch()
    assert [(x["scene"], x["stages"]) for x in queued] == [(b, ["STORYBOARD"])]          # önce kareler
    dispatch.batch_done(bid, b, True)
    fl2va = queued[1:]
    assert sorted(x["scene"] for x in fl2va) == sorted([b, c]) and all(x["batch"] == bid for x in fl2va)
    assert dispatch.batch_holding(a) and dispatch.batch_holding(d)                        # Ref2VA bekliyor
    dispatch.batch_done(bid, b, True)
    assert len(queued) == 3                                                              # c bitmedi
    dispatch.batch_done(bid, c, True)
    assert sorted(x["scene"] for x in queued[3:]) == sorted([a, d])                      # sonra Ref2VA
    dispatch.batch_done(bid, a, True)
    dispatch.batch_done(bid, d, True)
    assert len(queued) == 5 and not killswitch.r().exists(dispatch.BATCH.format(bid, "project"))


def test_only_ref2va_videos_need_no_barrier(queued, mk_project, mk_scene):
    pid = mk_project()
    a, b = mk_scene(pid, status="pending"), mk_scene(pid, status="pending")
    batch = dispatch.Batch(pid)
    batch.add(a, Stage.VISUAL | Stage.MUX, model="ref2va")
    batch.add(b, Stage.VISUAL | Stage.MUX, model="ref2va")
    assert batch.launch() is None and len(queued) == 2 and all(x["batch"] is None for x in queued)
