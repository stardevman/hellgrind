"""Google Flow ile çekim üretimi (köprü: app/live/flow_bridge.py, Mac'te tools/flow_bridge).

Çekimin Flow paketi (flow_pack.shot: istem, mod, süre, kareler ya da malzemeler) köprüye gider; dönen klip dış çekim
olarak girer (methods.register_external): görüntü özetinin yerine klibin içerik özeti geçer, ses, renk ve montaj
kendiliğinden çalışır. Önceki klipler silinmez.

Bütçe: Flow kredisi abonelikten düşer; her çekim kredi x FLOW_USD_PER_CREDIT tahminiyle rezervasyondan (Kill-Switch,
sekans tavanı, hız bekçisi) geçer. Ücretsiz modda Flow çağrılmaz: aynı sürede yerel prosedürel taslak üretilir ve tutar
sanal yazılır (değişmez kural 1 ve 3).
"""
import logging
import os
import tempfile

from app import storage
from app.budget import killswitch
from app.config import settings
from app.db import session_scope
from app.engine import flow_pack
from app.models import Scene, Variation

log = logging.getLogger("hellgrind.flow")
QUALITY_MODEL = {"lite": "veo-lite", "fast": "veo-fast", "quality": "veo-quality"}
MODEL_QUALITY = {v: k for k, v in QUALITY_MODEL.items()}
LABEL = "flow:video"


class FlowRejected(RuntimeError):
    """Flow'dan dönen klip kalite kontrolünden geçemedi (siyah ya da donmuş)."""


def quality_of(quality: str | None) -> str:
    return quality if quality in QUALITY_MODEL else MODEL_QUALITY.get(settings.flow_model, "fast")


def estimate_usd(quality: str) -> float:
    return round(flow_pack.FLOW_CREDITS[quality] * float(settings.flow_usd_per_credit), 4)


def _inputs(sh: flow_pack.FlowShot, tmp: str) -> dict[str, str]:
    """Köprüye gidecek görseller, yerel yollar olarak: start/end (Frames to Video) ya da ref1..ref3 (Ingredients)."""
    files = {}
    keys = [("start", sh.start_key), ("end", sh.end_key)] if sh.mode == "frames" else \
        [(f"ref{i}", ing["key"]) for i, ing in enumerate(sh.ingredients, start=1)]
    for name, key in keys:
        if key and storage.exists(key):
            ext = os.path.splitext(key)[1] or ".jpg"
            files[name] = storage.download(key, os.path.join(tmp, name + ext))
    return files


def _local_clip(scene: Scene, sh: flow_pack.FlowShot, path: str) -> None:
    """Ücretsiz mod: Flow yerine aynı sürede yerel prosedürel taslak."""
    from app.core.hashing import seed_for
    from app.engine.procedural import render_clip
    from app.engine.providers import RESOLUTIONS
    width, height = RESOLUTIONS["480p"]                 # yalnızca yer tutucu taslak
    render_clip(path, seed_for(str(scene.id), "flow", sh.prompt), width, height, float(sh.seconds), settings.local_render_fps,
                scene.emotion or "neutral", scene.camera or {}, [], float(scene.action_complexity or 0),
                f"{sh.code} · Flow")


def run_scene(project_id: str, scene_id: str, quality: str | None = None) -> str:
    """Çekimi Flow'da üretir ve dış çekim olarak bağlar. Dönen: "refined" (klip bağlandı, ses ve montaj kuyrukta)."""
    from app.api.plan import dispatch_result
    from app.core.hashing import visual_fingerprint
    from app.engine import methods
    from app.engine.references import sha_of
    from app.quality.anomaly import detect_anomalies
    quality = quality_of(quality)
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        sh = flow_pack.shot(s, scene, quality)
        reason = flow_pack.unsupported(sh, quality)
        if reason:
            raise FlowRejected(reason)
        scene.status, scene.last_error = "generating", None
        s.flush()
        s.expunge(scene)
    est = estimate_usd(quality)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        clip = os.path.join(tmp, f"{sh.code}.mp4")
        meta = {"model": QUALITY_MODEL[quality], "mode": sh.mode, "credits": sh.credits, "seconds": sh.seconds}
        if settings.dry_run:
            with killswitch.spend(project_id, est, LABEL, scene_id=scene_id, details={"flow": meta, "virtual": True}):
                _local_clip(scene, sh, clip)
        else:
            from app.engine import paid
            from app.live import flow_bridge
            files = _inputs(sh, tmp)

            def call():
                return flow_bridge.generate(sh.as_dict(), files, clip, model=QUALITY_MODEL[quality])
            out = paid.call(project_id, est, LABEL, "flow_bridge_token", call, scene_id=scene_id, attempts=1,
                            details={"flow": meta})
            meta.update({k: v for k, v in out.items() if v is not None})
        anomalies = detect_anomalies(clip)
        if anomalies:
            found = ", ".join(sorted({a["type"] for a in anomalies}))
            raise FlowRejected(f"Flow klibi kalite kontrolünden geçemedi ({found}); çekimi yeniden üret.")
        sha = sha_of(clip)
        key = f"external/{scene_id}/{sha}.mp4"
        if not storage.exists(key):
            storage.put_file(clip, key, "video/mp4")
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        sc.method, sc.external_key, sc.external_hash = "external", key, sha
        fingerprint = visual_fingerprint(sc, [], [])
    vid = methods.register_external(scene_id, fingerprint)
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.params = {**(v.params or {}), "source": "external", "flow": {**meta, "dry_run": settings.dry_run}}
        v.cost_usd = est
    killswitch.event(project_id, "flow_clip", scene_id=scene_id, code=sh.code, credits=sh.credits,
                     model=QUALITY_MODEL[quality], dry_run=settings.dry_run)
    with session_scope() as s:
        dispatch_result(s, s.get(Scene, scene_id))
    return "refined"
