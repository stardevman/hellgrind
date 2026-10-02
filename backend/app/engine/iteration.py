"""Kademeli İterasyon.

1. Test turu: probe_n adet düşük çözünürlüklü varyasyon.
2. Ortalama skor ve geçme oranı eşiği geçmezse yeni seed'lerle tekrar (en fazla max_correction_rounds).
   HERMES_MODE=llm ise burada LangGraph düzeltme döngüsü promptu revize eder (ücretli, Kademe 2).
3. Tam tur: wave_size'lık dalgalar, full_n üst sınırına kadar; refined_k adet "mükemmel" aday bulununca durur.
4. Rafine Havuz: en iyi refined_k varyasyon kalır, gerisi imha edilir.
"""
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import mean

from sqlalchemy import select

from app import storage
from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.core.hashing import seed_for, visual_fingerprint
from app.db import session_scope
from app.engine.providers import GenRequest, TransientError, get_provider, route_engine
from app.models import Character, Project, Scene, SceneCharacter, Variation
from app.quality import gate

log = logging.getLogger("hellgrind.iteration")
MAX_PROVIDER_RETRIES = 3


@dataclass
class VarResult:
    id: str
    passed: bool
    composite: float | None


@dataclass
class Ctx:
    project_id: str
    scene_id: str
    provider: object
    cfg: dict
    duration: float
    prompt_spec: dict
    rev: int
    salt: str
    render: dict


def project_cfg(project: Project) -> dict:
    return {**DEFAULT_PROJECT_SETTINGS, **(project.settings or {})}


def load_characters(s, scene_id):
    return s.scalars(select(Character).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                     .where(SceneCharacter.scene_id == scene_id).order_by(Character.name)).all()


def _set_scene(scene_id, **fields):
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        for k, v in fields.items():
            setattr(scene, k, v)


def make_thumb(video_path: str, key: str) -> str | None:
    thumb = video_path.rsplit(".", 1)[0] + ".jpg"
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "2", "-i", video_path,
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", thumb])
    if r.returncode != 0 or not os.path.exists(thumb):
        return None
    storage.put_file(thumb, key, "image/jpeg")
    os.remove(thumb)
    return key


def run_one(c: Ctx, tier: str, wave_no: int, idx: int, resolution: str) -> VarResult:
    if killswitch.is_frozen(c.project_id):
        raise killswitch.ProjectFrozen(c.project_id)
    seed = seed_for(c.scene_id, c.salt, tier, wave_no, idx)    # görsel girdi değişince yeni seed'ler
    req = GenRequest(prompt=c.prompt_spec.get("prompt", ""), negative=c.prompt_spec.get("negative", ""),
                     seed=seed, resolution=resolution, duration_s=c.duration,
                     extra={**c.render, "label": f"{c.render.get('label', '')}  {tier} {resolution} #{seed % 10000}"})
    with session_scope() as s:
        v = Variation(scene_id=c.scene_id, tier=tier, wave_no=wave_no, engine=c.provider.engine, seed=seed,
                      prompt_revision=c.rev, params={"resolution": resolution, "duration_s": c.duration,
                                                     "prompt": req.prompt}, status="running")
        s.add(v)
        s.flush()
        vid = v.id

    estimate = c.provider.estimate(req)
    result = None
    for attempt in range(1, MAX_PROVIDER_RETRIES + 1):
        try:
            with killswitch.spend(c.project_id, estimate, f"{c.provider.engine}:{resolution}",
                                  scene_id=c.scene_id, variation_id=vid) as charge:
                result = c.provider.generate(req)
                charge.actual = result.cost_usd
            break
        except TransientError as e:
            log.warning("geçici hata (deneme %d/%d): %s", attempt, MAX_PROVIDER_RETRIES, e)
        except Exception:
            _mark_failed(vid)
            raise
    if result is None:
        _mark_failed(vid)
        return VarResult(str(vid), False, None)

    key = f"variations/{c.scene_id}/{vid}.mp4"
    storage.put_file(result.local_path, key)
    thumb_key = make_thumb(result.local_path, f"thumbs/{c.scene_id}/{vid}.jpg")
    score = gate.score_video(result.local_path, seed, c.cfg)
    os.remove(result.local_path)

    quarantine_key = None
    if not score.passed:
        quarantine_key = storage.purge(key)          # eşiği geçemeyen anında imha / karantina
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.external_request_id = result.request_id
        v.cost_usd = result.cost_usd
        v.keyframe_key = thumb_key
        v.clip_score, v.face_score, v.composite_score = score.clip, score.face, score.composite
        v.anomalies = score.anomalies
        if score.passed:
            v.status, v.video_key = "passed", key
        else:
            v.status, v.video_key = "purged", None
            v.params = {**v.params, "quarantine_key": quarantine_key, "purge_reason": "quality_gate"}
    return VarResult(str(vid), score.passed, score.composite)


def _mark_failed(vid):
    with session_scope() as s:
        s.get(Variation, vid).status = "failed"


def retire_previous_pool(scene_id) -> int:
    """Yeni üretim başlarken önceki üretimin havuzunu (ve açıkta kalmış geçen adayları) emekli eder.

    Görsel girdi değişince ya da sahne baştan üretilince eski videolar bayattır. Onları yerinde bırakmak
    'refined' ama sırasız satırlar ve depoda sahipsiz dosyalar üretirdi. purge_mode'a göre quarantine/ önekine
    taşınır (hard modda silinir) ve neden olarak 'superseded' yazılır; panelde yine izlenebilir."""
    with session_scope() as s:
        old = s.scalars(select(Variation).where(Variation.scene_id == scene_id,
                                                Variation.status.in_(("refined", "passed")))).all()
        keys = [(v.id, v.video_key) for v in old]
        for v in old:
            v.refined_rank = None
    for vid, key in keys:
        quarantined = storage.purge(key) if key else None
        with session_scope() as s:
            v = s.get(Variation, vid)
            v.status, v.video_key = "purged", None
            v.params = {**(v.params or {}), "quarantine_key": quarantined, "purge_reason": "superseded"}
    return len(keys)


def run_wave(c: Ctx, tier: str, wave_no: int, n: int, resolution: str) -> list[VarResult]:
    results, frozen = [], False
    with ThreadPoolExecutor(max_workers=settings.gen_concurrency) as ex:
        futures = [ex.submit(run_one, c, tier, wave_no, i, resolution) for i in range(n)]
        for f in futures:
            try:
                results.append(f.result())
            except killswitch.ProjectFrozen:
                frozen = True
    if frozen:
        raise killswitch.ProjectFrozen(c.project_id)
    return results


def summarize(results: list[VarResult]) -> dict:
    scored = [r.composite for r in results if r.composite is not None]
    return {
        "n": len(results),
        "mean_composite": round(mean(scored), 4) if scored else 0.0,
        "pass_rate": round(sum(r.passed for r in results) / len(results), 3) if results else 0.0,
    }


def run_tiered_generation(project_id: str, scene_id: str) -> str:
    """Bir sahnenin görsel aşamasını baştan sona yürütür. Dönen değer sahnenin son durumudur."""
    with session_scope() as s:
        project, scene = s.get(Project, project_id), s.get(Scene, scene_id)
        cfg = project_cfg(project)
        engine = route_engine(scene)
        scene.engine, scene.status, scene.last_error = engine, "probing", None
        if project.status == "draft":
            project.status = "producing"
        s.flush()
        chars = load_characters(s, scene_id)
        fp = visual_fingerprint(scene, chars)
        cam = scene.camera or {}
        ctx = Ctx(project_id=project_id, scene_id=scene_id, provider=get_provider(engine), cfg=cfg,
                  duration=scene.duration_s, prompt_spec=dict(scene.prompt_spec or {}), rev=scene.prompt_revision,
                  salt=fp, render={"emotion": scene.emotion, "camera": cam, "characters": [ch.name for ch in chars],
                                   "action": scene.action_complexity,
                                   "label": f"{scene.scene_code}  {cam.get('shot', '')} {cam.get('movement', '')}"})

    retire_previous_pool(scene_id)

    # 1-2) Test turu (+ yeniden deneme turları)
    accepted = False
    for rnd in range(cfg["max_correction_rounds"] + 1):
        rep = summarize(run_wave(ctx, "probe", rnd, cfg["probe_n"], cfg["probe_resolution"]))
        log.info("scene=%s test turu %d: %s", scene_id, rnd, rep)
        killswitch.event(project_id, "probe_wave", scene_id=scene_id, round=rnd, **rep)
        if rep["mean_composite"] >= cfg["clip_threshold"] and rep["pass_rate"] >= cfg["min_pass_rate"]:
            accepted = True
            break
        with session_scope() as s:
            sc = s.get(Scene, scene_id)
            sc.status, sc.correction_count = "correcting", sc.correction_count + 1

    if not accepted:
        _set_scene(scene_id, status="needs_review", last_error="test turu eşiği geçilemedi")
        return "needs_review"

    # 3) Tam tur: dalgalar halinde, erken durmalı
    _set_scene(scene_id, status="generating")
    produced, wave = 0, 0
    while produced < cfg["full_n"]:
        if wave > 0 and killswitch.is_soft_limited(project_id):
            killswitch.event(project_id, "soft_limit_stop", scene_id=scene_id, produced=produced)
            break
        n = min(max(1, cfg["wave_size"]), cfg["full_n"] - produced)    # wave_size <= 0 sonsuz döngüye yol açardı
        rep = summarize(run_wave(ctx, "full", wave, n, cfg["full_resolution"]))
        produced, wave = produced + n, wave + 1
        excellent = gate.refined_count(scene_id, cfg["excellent_threshold"])
        log.info("scene=%s tam dalga %d: %s, mükemmel=%d", scene_id, wave, rep, excellent)
        killswitch.event(project_id, "full_wave", scene_id=scene_id, wave=wave, excellent=excellent, **rep)
        if excellent >= cfg["refined_k"]:
            break

    # 4) Rafine Havuz
    winners = gate.finalize_refined_pool(scene_id, k=cfg["refined_k"])
    if not winners:
        _set_scene(scene_id, status="needs_review", last_error="tam turda eşiği geçen varyasyon yok")
        return "needs_review"
    _set_scene(scene_id, status="refined", visual_hash=fp)
    return "refined"
