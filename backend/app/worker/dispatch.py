import json
import logging
import uuid

from celery import chain

from app.budget import killswitch
from app.core.rerender import Stage

log = logging.getLogger("hellgrind.dispatch")

# Aynı sahne için görüntü aşamasının iki kez kuyruğa girmesini önleyen kısa kilit. Yalnızca "kuyruğa verildi ama görev
# henüz başlamadı" aralığını kapatır: görev başlayınca bırakılır, sonrasını durum + çalışan görev denetimi korur.
DISPATCH_LOCK = "hg:dispatch:{}"
DISPATCH_LOCK_TTL_S = 30


def claim_dispatch(scene_id) -> bool:
    return bool(killswitch.r().set(DISPATCH_LOCK.format(scene_id), "1", nx=True, ex=DISPATCH_LOCK_TTL_S))


def release_dispatch(scene_id) -> None:
    killswitch.r().delete(DISPATCH_LOCK.format(scene_id))


def batch_holding(scene_id) -> bool:
    """Sahnenin kilidi bir toplu dağıtımda mı (video kareler bitince kuyruğa girecek)."""
    value = killswitch.r().get(DISPATCH_LOCK.format(scene_id))
    return value is not None and (value.decode() if isinstance(value, bytes) else value) != "1"


def build_chain(project_id: str, scene_id: str, stages: Stage, mode: str = "full", batch: str | None = None,
                standalone: bool | None = None):
    """Planlanan aşamaların görevlerini sırayla zincirler; çalışacak aşama yoksa None.

    Halkalar `.si()` (immutable): önceki görevin dönüş değeri sonrakine geçmez, her görev girdisini
    veritabanından ve nesne deposundan okur. mode="draft": görsel aşama yalnızca test turunu çalıştırır (taslak geçiş).
    Zincirde yalnızca STORYBOARD varsa sahne kareyle birlikte onaya (`board_review`) bırakılır (standalone; toplu
    dağıtımda kare zinciri ayrıldığında açıkça verilir). batch: storyboard görevi bitince toplu dağıtıma haber verir.
    """
    from app.worker import tasks  # tasks -> celery_app döngüsünü önlemek için geç import
    kwargs = {"project_id": project_id, "scene_id": scene_id}
    signatures = []
    if Stage.STORYBOARD in stages:
        if standalone is None:
            standalone = not (stages & (Stage.VISUAL | Stage.LIPSYNC | Stage.MUX))
        signatures.append(tasks.storyboard_scene.si(**kwargs, **({"standalone": True} if standalone else {}),
                                                    **({"batch": batch} if batch else {})))
    if Stage.VISUAL in stages:
        signatures.append(tasks.generate_scene.si(**kwargs, **({"mode": mode} if mode != "full" else {})))
    if Stage.LIPSYNC in stages:
        signatures.append(tasks.lipsync_scene.si(**kwargs))
    if Stage.GRADE in stages:
        signatures.append(tasks.grade_scene.si(**kwargs))
    if Stage.MUX in stages:
        signatures.append(tasks.mux_scene.si(**kwargs))
    return chain(*signatures) if signatures else None


# ---------- toplu dağıtım: önce bütün kareler, sonra bütün videolar ----------
# Storyboard ilk kareleri H3 makinesindeki görsel modelle (Qwen-Image), videolar H3 ile üretilir. Çekim başına "kare, video,
# kare, video" sırası GPU'da her çekimde model değiştirir; birden çok sahne birlikte işlenirken önce bütün kareler çizilir,
# videolar ancak son kare bitince kuyruğa girer. Celery'de sonuç deposu yok (akor kullanılamaz): bariyer Redis'tedir.

BATCH = "hg:batch:{}:{}"            # (kimlik, pending | deferred | project | flushed)
BATCHES = "hg:batches:{}"           # proje -> açık toplu dağıtımlar
BATCH_TTL_S = 6 * 3600              # Celery visibility_timeout ile aynı: en uzun iş


class Batch:
    """Çok sahneli planın kuyruğa verilmesi. `add` planları toplar, `launch` kareleri hemen, videoları bariyerin
    arkasında kuyruğa verir. Kare işi yoksa (ya da video yoksa) zincirler eskisi gibi doğrudan kuyruğa girer."""

    def __init__(self, project_id: str):
        self.project_id = str(project_id)
        self.items: list[tuple[str, Stage, str]] = []

    def add(self, scene_id, stages: Stage, mode: str = "full") -> None:
        self.items.append((str(scene_id), stages, mode))

    def launch(self) -> str | None:
        """Dönen değer toplu dağıtımın kimliği (bariyer kurulduysa)."""
        pid = self.project_id
        boards = [sid for sid, stages, _ in self.items if Stage.STORYBOARD in stages]
        videos = [sid for sid, stages, _ in self.items if Stage.VISUAL in stages]
        if len(self.items) < 2 or not boards or not videos:
            for sid, stages, mode in self.items:
                build_chain(pid, sid, stages, mode=mode).apply_async()
            return None
        bid = uuid.uuid4().hex[:12]
        now, deferred = [], {}
        for sid, stages, mode in self.items:
            rest = stages & ~Stage.STORYBOARD
            if Stage.STORYBOARD in stages and Stage.VISUAL in rest:
                now.append(build_chain(pid, sid, Stage.STORYBOARD, batch=bid, standalone=False))
                deferred[sid] = json.dumps({"stages": rest.value, "mode": mode})
            elif Stage.STORYBOARD in stages:
                now.append(build_chain(pid, sid, stages, mode=mode, batch=bid))
            elif Stage.VISUAL in stages:
                deferred[sid] = json.dumps({"stages": stages.value, "mode": mode})
            else:
                now.append(build_chain(pid, sid, stages, mode=mode))
        pipe = killswitch.r().pipeline()
        pipe.sadd(BATCH.format(bid, "pending"), *boards)
        pipe.hset(BATCH.format(bid, "deferred"), mapping=deferred)
        pipe.set(BATCH.format(bid, "project"), pid)
        for name in ("pending", "deferred", "project"):
            pipe.expire(BATCH.format(bid, name), BATCH_TTL_S)
        pipe.sadd(BATCHES.format(pid), bid)
        pipe.expire(BATCHES.format(pid), BATCH_TTL_S)
        for sid in deferred:                 # kareler sürerken aynı sahne ikinci kez kuyruğa verilmesin
            pipe.set(DISPATCH_LOCK.format(sid), bid, ex=BATCH_TTL_S)
        pipe.execute()                       # bariyer, ilk kare bitmeden önce kurulu olmalı
        for c in now:
            c.apply_async()
        killswitch.event(pid, "batched", batch=bid, boards=len(boards), videos=len(deferred))
        return bid


def batch_done(batch: str, scene_id: str, ok: bool) -> None:
    """Storyboard görevi bitti (başarılı ya da değil). Son kare bitince ertelenen videolar kuyruğa girer. Karesi
    başarısız sahnenin videosu düşer (zincirde olduğu gibi)."""
    r = killswitch.r()
    pending, deferred = BATCH.format(batch, "pending"), BATCH.format(batch, "deferred")
    if not r.exists(BATCH.format(batch, "project")):
        return                               # dondurmada düşürüldü ya da süresi doldu
    r.srem(pending, str(scene_id))
    if not ok and r.hdel(deferred, str(scene_id)):
        release_dispatch(scene_id)
    if r.scard(pending) == 0 and r.set(BATCH.format(batch, "flushed"), "1", nx=True, ex=BATCH_TTL_S):
        flush(batch)


def flush(batch: str) -> int:
    """Ertelenen video zincirlerini kuyruğa verir (proje donmuşsa hiçbirini). Dönen değer kuyruğa verilen sahne sayısı."""
    from app.db import session_scope
    from app.models import Scene
    r = killswitch.r()
    raw_pid = r.get(BATCH.format(batch, "project"))
    if raw_pid is None:
        return 0
    pid = raw_pid.decode() if isinstance(raw_pid, bytes) else raw_pid
    deferred = {(k.decode() if isinstance(k, bytes) else k): json.loads(v)
                for k, v in r.hgetall(BATCH.format(batch, "deferred")).items()}
    drop(pid, batch)
    if killswitch.is_frozen(pid):
        return 0
    with session_scope() as s:
        statuses = {sid: getattr(s.get(Scene, sid), "status", None) for sid in deferred}
    n = 0
    for sid, spec in deferred.items():
        if statuses.get(sid) in (None, "frozen", "failed", "blocked"):
            release_dispatch(sid)
            continue
        killswitch.r().set(DISPATCH_LOCK.format(sid), batch, ex=DISPATCH_LOCK_TTL_S)
        build_chain(pid, sid, Stage(spec["stages"]), mode=spec.get("mode") or "full").apply_async()
        n += 1
    killswitch.event(pid, "batch_videos", batch=batch, videos=n)
    log.info("toplu dağıtım %s: kareler bitti, %d video zinciri kuyruğa verildi", batch, n)
    return n


def drop(project_id: str, batch: str) -> None:
    r = killswitch.r()
    r.delete(*(BATCH.format(batch, name) for name in ("pending", "deferred", "project")))
    r.srem(BATCHES.format(project_id), batch)


def drop_batches(project_id: str) -> int:
    """Dondurmada: açık toplu dağıtımlar düşer (ertelenen sahneler `frozen` olur; devamda yeniden üretilir)."""
    r = killswitch.r()
    batches = [b.decode() if isinstance(b, bytes) else b for b in r.smembers(BATCHES.format(project_id))]
    for b in batches:
        for sid in r.hkeys(BATCH.format(b, "deferred")):
            sid = sid.decode() if isinstance(sid, bytes) else sid
            lock = r.get(DISPATCH_LOCK.format(sid))
            if lock is not None and (lock.decode() if isinstance(lock, bytes) else lock) == b:
                release_dispatch(sid)
        drop(project_id, b)
    return len(batches)
