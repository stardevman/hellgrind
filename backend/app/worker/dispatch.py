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
    dağıtımda kare zinciri ayrıldığında açıkça verilir). batch: zincirin ilk ücretli görevi (storyboard, yoksa görüntü)
    bitince toplu dağıtıma haber verir; sıradaki model grubu ona göre kuyruğa girer.
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
        report = {"batch": batch} if batch and Stage.STORYBOARD not in stages else {}
        signatures.append(tasks.generate_scene.si(**kwargs, **({"mode": mode} if mode != "full" else {}), **report))
    if Stage.LIPSYNC in stages:
        signatures.append(tasks.lipsync_scene.si(**kwargs))
    if Stage.GRADE in stages:
        signatures.append(tasks.grade_scene.si(**kwargs))
    if Stage.MUX in stages:
        signatures.append(tasks.mux_scene.si(**kwargs))
    return chain(*signatures) if signatures else None


# ---------- toplu dağıtım: model türüne göre gruplu kuyruk ----------
# Aynı RunPod worker'ında model değişimi (Qwen-Image / Qwen-Image-Edit ilk kare -> H3 FL2VA -> H3 Ref2VA) 20-40 GB modeli
# yeniden yükler (~2-3 dk, faturalanır). Birden çok sahne birlikte işlenirken kuyruk sıraya girer: önce bekleyen bütün
# kareler (metinden çizilenler Qwen-Image, karakterliler Qwen-Image-Edit: ayrı gruplar), sonra bütün FL2VA (ve T2V)
# videoları, sonra bütün Ref2VA videoları; her grup bir öncekinin işleri bitince kuyruğa girer.
# Celery'de sonuç deposu yok (akor kullanılamaz): bariyer Redis'tedir.

BATCH = "hg:batch:{}:{}"            # (kimlik, pending | stage | deferred:<grup> | project | flushed:<grup>)
BATCHES = "hg:batches:{}"           # proje -> açık toplu dağıtımlar
BATCH_TTL_S = 6 * 3600              # Celery visibility_timeout ile aynı: en uzun iş
GROUPS = ("board_qwen", "board_edit", "fl2va", "ref2va")  # kuyruk sırası; model: Qwen-Image, Qwen-Image-Edit, H3 DiT'leri


def _s(value) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


class Batch:
    """Çok sahneli planın kuyruğa verilmesi. `add` planları (ve çekimin H3 modelini) toplar; `launch` ilk boş olmayan
    grubu hemen, kalanları bariyerin arkasında kuyruğa verir. Tek grup varsa zincirler doğrudan kuyruğa girer."""

    def __init__(self, project_id: str):
        self.project_id = str(project_id)
        self.items: list[tuple[str, Stage, str]] = []
        self.models: dict[str, str] = {}
        self.boards: dict[str, str] = {}

    def add(self, scene_id, stages: Stage, mode: str = "full", model: str = "fl2va", board: str = "qwen") -> None:
        """model: çekimin H3 DiT'i (fl2va | ref2va); board: ilk kare modeli (qwen | qwen_edit)."""
        self.items.append((str(scene_id), stages, mode))
        self.models[str(scene_id)] = model if model in GROUPS else "fl2va"
        self.boards[str(scene_id)] = "board_edit" if board == "qwen_edit" else "board_qwen"

    def plan(self) -> tuple[list, dict[str, dict[str, str]]]:
        """(hemen kuyruğa girecek zincirler, grup -> {sahne: zincir}) — kareler ilk kare modelinin grubunda."""
        now, groups = [], {g: {} for g in GROUPS}
        for sid, stages, mode in self.items:
            rest = stages & ~Stage.STORYBOARD
            model, board = self.models.get(sid, "fl2va"), self.boards.get(sid, "board_qwen")
            if Stage.STORYBOARD in stages and Stage.VISUAL in rest:
                groups[board][sid] = json.dumps({"stages": Stage.STORYBOARD.value, "mode": mode, "standalone": False})
                groups[model][sid] = json.dumps({"stages": rest.value, "mode": mode})
            elif Stage.STORYBOARD in stages:
                groups[board][sid] = json.dumps({"stages": stages.value, "mode": mode})
            elif Stage.VISUAL in stages:
                groups[model][sid] = json.dumps({"stages": stages.value, "mode": mode})
            else:
                now.append((sid, stages, mode))
        return now, groups

    def launch(self) -> str | None:
        """Dönen değer toplu dağıtımın kimliği (bariyer kurulduysa)."""
        pid = self.project_id
        now, groups = self.plan()
        active = [g for g in GROUPS if groups[g]]
        if len(self.items) < 2 or len(active) < 2:
            for sid, stages, mode in self.items:
                build_chain(pid, sid, stages, mode=mode).apply_async()
            return None
        bid = uuid.uuid4().hex[:12]
        first = active[0]
        pipe = killswitch.r().pipeline()
        pipe.set(BATCH.format(bid, "project"), pid)
        pipe.set(BATCH.format(bid, "stage"), first)
        pipe.sadd(BATCH.format(bid, "pending"), *groups[first])
        keys = ["project", "stage", "pending"]
        for g in active[1:]:
            pipe.hset(BATCH.format(bid, f"deferred:{g}"), mapping=groups[g])
            keys.append(f"deferred:{g}")
        for name in keys:
            pipe.expire(BATCH.format(bid, name), BATCH_TTL_S)
        pipe.sadd(BATCHES.format(pid), bid)
        pipe.expire(BATCHES.format(pid), BATCH_TTL_S)
        for g in active[1:]:                 # sonraki grupları bekleyen sahneler ikinci kez kuyruğa verilmesin
            for sid in groups[g]:
                if Stage.VISUAL.value & json.loads(groups[g][sid])["stages"]:
                    pipe.set(DISPATCH_LOCK.format(sid), bid, ex=BATCH_TTL_S)
        pipe.execute()                       # bariyer, ilk iş bitmeden önce kurulu olmalı
        for sid, stages, mode in now:
            build_chain(pid, sid, stages, mode=mode).apply_async()
        _dispatch_group(pid, bid, groups[first])
        counts = {g: len(groups[g]) for g in GROUPS}
        killswitch.event(pid, "batched", batch=bid, boards=counts["board_qwen"] + counts["board_edit"],
                         edit_boards=counts["board_edit"], videos=counts["fl2va"] + counts["ref2va"],
                         fl2va=counts["fl2va"], ref2va=counts["ref2va"])
        return bid


def _dispatch_group(pid: str, batch: str, specs: dict[str, str]) -> None:
    for sid, raw in specs.items():
        spec = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        build_chain(pid, sid, Stage(spec["stages"]), mode=spec.get("mode") or "full", batch=batch,
                    standalone=spec.get("standalone")).apply_async()


def batch_done(batch: str, scene_id: str, ok: bool) -> None:
    """Grubun bir işi bitti (storyboard ya da görüntü görevi; başarılı ya da değil). Grubun son işi bitince sıradaki grup
    kuyruğa girer. Karesi başarısız sahnenin videosu düşer (zincirde olduğu gibi)."""
    r = killswitch.r()
    if not r.exists(BATCH.format(batch, "project")):
        return                               # dondurmada düşürüldü ya da süresi doldu
    stage = _s(r.get(BATCH.format(batch, "stage"))) or GROUPS[0]
    r.srem(BATCH.format(batch, "pending"), str(scene_id))
    if not ok:
        for g in GROUPS[GROUPS.index(stage) + 1:]:
            if r.hdel(BATCH.format(batch, f"deferred:{g}"), str(scene_id)):
                release_dispatch(scene_id)
    if r.scard(BATCH.format(batch, "pending")) == 0 \
            and r.set(BATCH.format(batch, f"flushed:{stage}"), "1", nx=True, ex=BATCH_TTL_S):
        flush(batch)


def flush(batch: str) -> int:
    """Sıradaki boş olmayan grubu kuyruğa verir (proje donmuşsa hiçbirini). Dönen değer kuyruğa verilen sahne sayısı."""
    from app.db import session_scope
    from app.models import Scene
    r = killswitch.r()
    pid = _s(r.get(BATCH.format(batch, "project")))
    if pid is None:
        return 0
    if killswitch.is_frozen(pid):
        drop(pid, batch)
        return 0
    stage = _s(r.get(BATCH.format(batch, "stage"))) or GROUPS[0]
    for g in GROUPS[GROUPS.index(stage) + 1:]:
        deferred = {_s(k): json.loads(v) for k, v in r.hgetall(BATCH.format(batch, f"deferred:{g}")).items()}
        r.delete(BATCH.format(batch, f"deferred:{g}"))
        if not deferred:
            continue
        with session_scope() as s:
            statuses = {sid: getattr(s.get(Scene, sid), "status", None) for sid in deferred}
        ready = {}
        for sid, spec in deferred.items():
            if statuses.get(sid) in (None, "frozen", "failed", "blocked"):
                release_dispatch(sid)
                continue
            r.set(DISPATCH_LOCK.format(sid), batch, ex=DISPATCH_LOCK_TTL_S)
            ready[sid] = spec
        if not ready:
            continue
        r.set(BATCH.format(batch, "stage"), g, ex=BATCH_TTL_S)
        r.sadd(BATCH.format(batch, "pending"), *ready)
        r.expire(BATCH.format(batch, "pending"), BATCH_TTL_S)
        _dispatch_group(pid, batch, ready)
        killswitch.event(pid, "batch_videos", batch=batch, group=g, videos=len(ready))
        log.info("toplu dağıtım %s: %s grubu kuyruğa verildi (%d sahne)", batch, g, len(ready))
        return len(ready)
    drop(pid, batch)
    return 0


def drop(project_id: str, batch: str) -> None:
    r = killswitch.r()
    r.delete(*(BATCH.format(batch, name) for name in ("pending", "stage", "project", *(f"deferred:{g}" for g in GROUPS))))
    r.srem(BATCHES.format(project_id), batch)


def drop_batches(project_id: str) -> int:
    """Dondurmada: açık toplu dağıtımlar düşer (ertelenen sahneler `frozen` olur; devamda yeniden üretilir)."""
    r = killswitch.r()
    batches = [_s(b) for b in r.smembers(BATCHES.format(project_id))]
    for b in batches:
        for g in GROUPS:
            for sid in r.hkeys(BATCH.format(b, f"deferred:{g}")):
                sid = _s(sid)
                if _s(r.get(DISPATCH_LOCK.format(sid))) == b:
                    release_dispatch(sid)
        drop(project_id, b)
    return len(batches)
