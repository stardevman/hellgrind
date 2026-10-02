from celery import chain

from app.core.rerender import Stage


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
