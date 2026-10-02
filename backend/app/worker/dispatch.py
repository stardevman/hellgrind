from celery import chain

from app.budget import killswitch
from app.core.rerender import Stage

# Aynı sahne için görüntü aşamasının iki kez kuyruğa girmesini önleyen kısa kilit. Yalnızca "kuyruğa verildi ama görev
# henüz başlamadı" aralığını kapatır: görev başlayınca bırakılır, sonrasını durum + çalışan görev denetimi korur.
DISPATCH_LOCK = "hg:dispatch:{}"
DISPATCH_LOCK_TTL_S = 30


def claim_dispatch(scene_id) -> bool:
    return bool(killswitch.r().set(DISPATCH_LOCK.format(scene_id), "1", nx=True, ex=DISPATCH_LOCK_TTL_S))


def release_dispatch(scene_id) -> None:
    killswitch.r().delete(DISPATCH_LOCK.format(scene_id))


def build_chain(project_id: str, scene_id: str, stages: Stage):
    """Planlanan aşamaların görevlerini sırayla zincirler; çalışacak aşama yoksa None.

    Halkalar `.si()` (immutable): önceki görevin dönüş değeri sonrakine geçmez, her görev girdisini
    veritabanından ve nesne deposundan okur.
    """
    from app.worker import tasks  # tasks -> celery_app döngüsünü önlemek için geç import
    kwargs = {"project_id": project_id, "scene_id": scene_id}
    signatures = []
    if Stage.VISUAL in stages:
        signatures.append(tasks.generate_scene.si(**kwargs))
    if Stage.LIPSYNC in stages:
        signatures.append(tasks.lipsync_scene.si(**kwargs))
    if Stage.MUX in stages:
        signatures.append(tasks.mux_scene.si(**kwargs))
    return chain(*signatures) if signatures else None
