from celery import chain

from app.core.rerender import Stage


def build_chain(project_id: str, scene_id: str, stages: Stage):
    from app.worker import tasks
    kw = {"project_id": project_id, "scene_id": scene_id}
    sigs = []
    if Stage.VISUAL in stages:
        sigs.append(tasks.generate_scene.si(**kw))
    if Stage.LIPSYNC in stages:
        sigs.append(tasks.lipsync_scene.si(**kw))
    if Stage.MUX in stages:
        sigs.append(tasks.mux_scene.si(**kw))
    return chain(*sigs) if sigs else None
