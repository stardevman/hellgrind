"""Kademeli iterasyon: bir sahnenin görsel aşaması.

1. Test turu: `probe_n` düşük çözünürlüklü varyasyon. Ortalama birleşik skor ve geçme oranı eşiği geçmezse yeni
   seed'lerle tekrar (en fazla `max_correction_rounds`); yine olmazsa sahne `needs_review`.
2. Tam tur: `wave_size`'lık dalgalar, `full_n` üst sınırına kadar. `excellent_threshold` üstünde `refined_k` aday
   bulununca durur; yumuşak bütçe limiti bayrağı varken yeni dalga başlamaz.
3. Rafine Havuz: yalnızca tam turda geçenlerden en iyi `refined_k` kalır, gerisi imha edilir.

Taslak geçiş (mode="draft", Faz 14): yalnızca test turu çalışır; geçen ilk varyasyon tek başına havuza girer ve kaba
kurguda kullanılır. Tam tur hiç çalışmaz. Sonraki iyileştirme geçişi aynı seed'lerle başladığı için test turu klipleri
yeniden kullanılır (yeniden üretilmez, yeniden ücretlendirilmez).

Seed'ler girdilerden türetildiği için çalıştırma tekrarlanabilir: bir kesintiden (Kill-Switch, çökme) sonra aynı girdiyle
yeniden başlayan çalıştırma, tamamlanmış varyasyonları yeniden üretmez ve bütçeden yeniden düşmez.
"""
import logging
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from statistics import mean

from sqlalchemy import and_, or_, select

from app import storage
from app.budget import killswitch
from app.config import project_settings, settings
from app.core.hashing import seed_for, visual_fingerprint
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine.providers import GenRequest, TransientError, get_provider, route_engine
from app.hermes import lint
from app.style import bible
from app.style.composer import compose_prompt
from app.models import Project, Scene, Variation
from app.quality import gate

log = logging.getLogger("hellgrind.iteration")

MAX_ATTEMPTS = 3            # geçici sağlayıcı hatalarında varyasyon başına deneme
THUMB_WIDTH = 320


@dataclass
class VarResult:
    id: str
    passed: bool
    composite: float | None


@dataclass
class Ctx:
    project_id: str
    scene_id: str
    run_id: str
    provider: object
    cfg: dict
    duration: float
    prompt_spec: dict
    revision: int
    visual_hash: str
    render: dict


# ---------- yardımcılar ----------

def make_thumb(video_path: str, key: str) -> str | None:
    """320 px genişliğinde jpg; 2. saniyeden, klip daha kısaysa ilk kareden. Başarısızlık üretimi durdurmaz."""
    thumb = video_path + ".jpg"
    for at in ("2", "0"):
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", at, "-i", video_path,
                        "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "5", thumb], capture_output=True)
        if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            break
    else:
        return None
    try:
        storage.put_file(thumb, key, "image/jpeg")
    finally:
        os.remove(thumb)
    return key


def _set_variation(vid, **fields) -> None:
    with session_scope() as s:
        v = s.get(Variation, vid)
        for key, value in fields.items():
            setattr(v, key, value)


def _merge_params(vid, **extra) -> None:
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.params = {**(v.params or {}), **extra}


def _reusable(c: Ctx, seed: int, tier: str, resolution: str) -> Variation | None:
    """Aynı seed ile daha önce tamamlanmış varyasyon: videosu hâlâ depoda duran geçen ya da kalite kapısında elenen.
    Taslak geçişte havuza giren test klibi de (yalnızca test turunda) yeniden kullanılır: iyileştirme geçişi onu yeniden
    üretmez. Havuzdan emekli edilmiş (superseded / not_top_k) ya da hata vermiş olanlar yeniden kullanılmaz."""
    usable = ("passed", "refined") if tier == "probe" else ("passed",)
    with session_scope() as s:
        v = s.scalars(select(Variation).where(
            Variation.scene_id == c.scene_id, Variation.seed == seed, Variation.tier == tier,
            Variation.params["resolution"].astext == resolution,
            or_(Variation.status.in_(usable),
                and_(Variation.status == "purged", Variation.params["purge_reason"].astext == "quality_gate")),
        ).order_by(Variation.created_at.desc()).limit(1)).first()
        if v is None:
            return None
        if v.status != "purged" and not (v.video_key and storage.exists(v.video_key)):
            return None
        s.expunge(v)
        return v


# ---------- tek varyasyon ----------

def run_one(c: Ctx, tier: str, wave_no: int, idx: int, resolution: str) -> VarResult:
    if killswitch.is_frozen(c.project_id):                                             # 1. Frozen kontrolü
        raise killswitch.ProjectFrozen(c.project_id)
    seed_parts = [c.scene_id, c.visual_hash, tier, wave_no, idx]                       # 2. Seed
    seed = seed_for(*seed_parts, *([c.revision] if c.revision else []))                #    ("baştan üret" yeni seed'ler verir)

    done = _reusable(c, seed, tier, resolution)
    if done is not None:
        _merge_params(done.id, run=c.run_id)
        return VarResult(str(done.id), done.status in ("passed", "refined"), done.composite_score)

    label = f"{c.render.get('label', '')}  {tier} {resolution} #{seed % 10000}"
    req = GenRequest(prompt=c.prompt_spec.get("prompt", ""), negative=c.prompt_spec.get("negative", ""), seed=seed,
                     resolution=resolution, duration_s=c.duration, extra={**c.render, "label": label})
    with session_scope() as s:                                                         # 3. Variation satırı
        v = Variation(scene_id=c.scene_id, tier=tier, wave_no=wave_no, engine=c.provider.engine, seed=seed,
                      prompt_revision=c.revision, status="queued",
                      params={"resolution": resolution, "duration_s": c.duration, "prompt": req.prompt, "run": c.run_id})
        s.add(v)
        s.flush()
        vid = v.id
    try:
        result = _generate(c, req, vid)                                                # 4. spend() içinde üretim
        if result is None:
            _set_variation(vid, status="failed")
            return VarResult(str(vid), False, None)
        # Ödeme yapıldı: kimlik ve maliyet, yükleme çökse bile denetim için hemen kaydedilir.
        _set_variation(vid, status="generated", external_request_id=result.request_id, cost_usd=result.cost_usd)
        try:
            key = f"variations/{c.scene_id}/{vid}.mp4"                                 # 5. Depo ve küçük resim
            storage.put_file(result.local_path, key)
            thumb_key = make_thumb(result.local_path, f"thumbs/{c.scene_id}/{vid}.jpg")
            score = gate.score_video(result.local_path, seed, c.cfg)                   # 6. Skorlama
        finally:
            os.remove(result.local_path)
    except BaseException:
        _set_variation(vid, status="failed")                                           # "running" satırı asılı kalmasın
        raise

    extra = {"provider_meta": result.meta}
    if score.passed:
        fields = dict(status="passed", video_key=key)
    else:                                                                              # 7. Anında imha
        extra.update(quarantine_key=storage.purge(key), purge_reason="quality_gate")
        fields = dict(status="purged", video_key=None)
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.keyframe_key = thumb_key
        v.clip_score, v.face_score, v.composite_score = score.clip, score.face, score.composite
        v.anomalies = score.anomalies
        v.params = {**(v.params or {}), **extra}
        for field, value in fields.items():
            setattr(v, field, value)
    return VarResult(str(vid), score.passed, score.composite)


def _generate(c: Ctx, req: GenRequest, vid):
    """Geçici hatalarda en fazla MAX_ATTEMPTS deneme; her deneme kendi rezervasyonunu alır, başarısızsa iade eder."""
    estimate = c.provider.estimate(req)
    _set_variation(vid, status="running")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with killswitch.spend(c.project_id, estimate, f"{c.provider.engine}:{req.resolution}",
                                  scene_id=c.scene_id, variation_id=vid) as charge:
                result = c.provider.generate(req)
                charge.actual = result.cost_usd
            return result
        except TransientError as e:
            log.warning("geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
    return None


# ---------- dalga ----------

def run_wave(c: Ctx, tier: str, wave_no: int, n: int, resolution: str) -> list[VarResult]:
    """n varyasyonu `gen_concurrency` iş parçacığıyla üretir. Donma ya da başka hata, dalga bitince yükseltilir."""
    results, frozen, error = [], None, None
    with ThreadPoolExecutor(max_workers=max(1, settings.gen_concurrency)) as pool:
        futures = [pool.submit(run_one, c, tier, wave_no, i, resolution) for i in range(n)]
        for f in futures:
            try:
                results.append(f.result())
            except killswitch.ProjectFrozen as e:
                frozen = frozen or e
            except Exception as e:                       # noqa: BLE001 — diğer iş parçacıkları bitsin, sonra yükselt
                error = error or e
    if frozen:
        raise frozen
    if error:
        raise error
    return results


def summarize(results: list[VarResult]) -> dict:
    scored = [r.composite for r in results if r.composite is not None]
    return {"n": len(results),
            "mean_composite": round(mean(scored), 4) if scored else 0.0,
            "pass_rate": round(sum(r.passed for r in results) / len(results), 3) if results else 0.0}


# ---------- sahne ----------

def _prepare(project_id: str, scene_id: str, run_id: str) -> Ctx:
    with session_scope() as s:
        project, scene = s.get(Project, project_id), s.get(Scene, scene_id)
        cfg = project_settings(project.settings)
        if scene.engine is None:                          # yönlendirme normalde ingest/API'de yazılır
            scene.engine = route_engine(scene)
        scene.status, scene.last_error = "probing", None
        if project.status == "draft":
            project.status = "producing"
        s.flush()
        chars = load_characters(s, scene_id)
        assets = load_assets(s, scene_id)
        style = bible.active(s, project_id)
        camera = scene.camera or {}
        spec = dict(scene.prompt_spec or {})
        # Sağlayıcıya yalnızca bestecinin çıktısı gider: kılavuzun ışık ve hareket dili, çekim tarifi, varlıklar ve
        # "yalnızca listelenen karakterler" kısıtı. Hepsi görsel özetteki girdilerden türer.
        composed = compose_prompt({"prompt": spec.get("prompt", ""), "negative": spec.get("negative", ""),
                                   "camera": camera, "characters": [ch.name for ch in chars]}, assets, style)
        spec["prompt"], spec["negative"] = composed.prompt, composed.negative
        # Yarım kalmış önceki çalıştırmanın asılı satırları: kimse üstünde çalışmıyor.
        for v in s.scalars(select(Variation).where(Variation.scene_id == scene_id,
                                                   Variation.status.in_(("queued", "running", "generated")))):
            v.status = "failed"
            v.params = {**(v.params or {}), "purge_reason": "interrupted"}
        return Ctx(
            project_id=project_id, scene_id=scene_id, run_id=run_id, provider=get_provider(scene.engine), cfg=cfg,
            duration=scene.duration_s, prompt_spec=spec, revision=scene.prompt_revision,
            visual_hash=visual_fingerprint(scene, chars, assets, style.version if style else None),
            render={"emotion": scene.emotion, "camera": camera, "characters": [ch.name for ch in chars],
                    "lighting": style.lighting_prefix if style else "",
                    "action": scene.action_complexity,
                    "loras": [{"path": ch.lora_url, "scale": ch.lora_scale} for ch in chars if ch.lora_url],
                    "lora_triggers": [ch.lora_trigger for ch in chars if ch.lora_trigger],
                    "label": f"{scene.scene_code}  {camera.get('shot', '')} {camera.get('movement', '')}"})


def _finish_draft(c: Ctx, first: VarResult) -> str:
    """Taslak: test turunda geçen ilk varyasyon tek başına havuza girer. Diğer geçenler yerinde kalır (iyileştirme
    geçişi onları yeniden kullanır)."""
    gate.finalize_draft(c.scene_id, first.id, {
        "status": "refined", "visual_hash": c.visual_hash, "lipsync_video_key": None, "final_video_key": None,
        "last_error": None, "cut_tier": "draft"})
    killswitch.event(c.project_id, "refined", scene_id=c.scene_id, pool=1, draft=True)
    return "refined"


def lint_blocks(project_id: str, scene_id: str) -> bool:
    """Worker tarafındaki üretim öncesi kontrol: dispatch ile görev başlangıcı arasında bir varlığın kilidi açılmış
    olabilir. Engellerse sahneyi `blocked` yapar; hiçbir şey harcanmaz."""
    with session_scope() as s:
        result = lint.check_scene(s, scene_id, project_settings(s.get(Project, project_id).settings))
    if not result.blocks:
        return False
    advance_scene(project_id, scene_id, status="blocked", last_error=result.message())
    killswitch.event(project_id, "blocked", scene_id=scene_id, missing=[m.name for m in result.missing])
    return True


def board_blocks(project_id: str, scene_id: str) -> bool:
    """Onay zorunluyken storyboard karesi bu görsel girdiler için onaylanmamışsa görüntü üretilmez (worker tarafı)."""
    from app.engine.storyboard import board_ready
    with session_scope() as s:
        cfg = project_settings(s.get(Project, project_id).settings)
        if not cfg["require_board_approval"]:
            return False
        scene = s.get(Scene, scene_id)
        style = bible.active_version(s, project_id)
        ready = board_ready(scene, visual_fingerprint(scene, load_characters(s, scene_id), load_assets(s, scene_id), style))
    if ready:
        return False
    advance_scene(project_id, scene_id, status="board_review")
    killswitch.event(project_id, "board_required", scene_id=scene_id)
    return True


def run_tiered_generation(project_id: str, scene_id: str, mode: str = "full") -> str:
    """Sahnenin görsel aşamasını yürütür. mode="draft": yalnızca test turu, geçen ilk varyasyon havuza girer.
    Dönen değer: "refined", "needs_review", "blocked" ya da "board_review"."""
    gate.ensure_scorer()                                  # para harcanmadan önce
    if lint_blocks(project_id, scene_id):
        return "blocked"
    if board_blocks(project_id, scene_id):
        return "board_review"
    ctx = _prepare(project_id, scene_id, uuid.uuid4().hex)
    cfg = {**ctx.cfg, **{k: max(1, int(ctx.cfg[k])) for k in ("probe_n", "full_n", "wave_size", "refined_k")}}

    # 1) Test turu, gerekirse yeni seed'lerle yeniden denemeler
    for round_no in range(max(0, int(cfg["max_correction_rounds"])) + 1):
        results = run_wave(ctx, "probe", round_no, cfg["probe_n"], cfg["probe_resolution"])
        report = summarize(results)
        log.info("scene=%s test turu %d: %s", scene_id, round_no, report)
        killswitch.event(project_id, "probe_wave", scene_id=scene_id, round=round_no, **report)
        if mode == "draft" and (first := next((r for r in results if r.passed), None)):
            return _finish_draft(ctx, first)
        if mode != "draft" and report["mean_composite"] >= cfg["clip_threshold"] and report["pass_rate"] >= cfg["min_pass_rate"]:
            break
        with session_scope() as s:
            scene = s.get(Scene, scene_id)
            scene.correction_count += 1
        advance_scene(project_id, scene_id, status="correcting")
    else:
        advance_scene(project_id, scene_id, status="needs_review", last_error="test turu eşiği geçilemedi")
        return "needs_review"

    # 2) Tam tur: dalgalar halinde, erken durmalı
    advance_scene(project_id, scene_id, status="generating")
    produced, wave = 0, 0
    while produced < cfg["full_n"]:
        # İlk dalga her zaman çalışır: yoksa limite yakınken başlayan sahne hiç bitemezdi.
        if wave > 0 and killswitch.is_soft_limited(project_id):
            killswitch.event(project_id, "soft_limit_stop", scene_id=scene_id, produced=produced)
            break
        n = min(cfg["wave_size"], cfg["full_n"] - produced)
        report = summarize(run_wave(ctx, "full", wave, n, cfg["full_resolution"]))
        produced, wave = produced + n, wave + 1
        excellent = gate.excellent_count(scene_id, ctx.run_id, cfg["excellent_threshold"])
        log.info("scene=%s tam dalga %d: %s, mükemmel=%d", scene_id, wave, report, excellent)
        killswitch.event(project_id, "full_wave", scene_id=scene_id, wave=wave, excellent=excellent, **report)
        if excellent >= cfg["refined_k"]:
            break

    # 3) Rafine Havuz. Video değişti: eski dudak senkronu ve final video artık geçersiz, plan yeniden çalıştırsın.
    winners = gate.finalize_refined_pool(scene_id, ctx.run_id, cfg["refined_k"], {
        "status": "refined", "visual_hash": ctx.visual_hash, "lipsync_video_key": None, "final_video_key": None,
        "last_error": None, "cut_tier": "final", "upgrade_requested": False})
    if not winners:
        advance_scene(project_id, scene_id, status="needs_review", last_error="tam turda eşiği geçen varyasyon yok")
        return "needs_review"
    killswitch.event(project_id, "refined", scene_id=scene_id, pool=len(winners))
    return "refined"
