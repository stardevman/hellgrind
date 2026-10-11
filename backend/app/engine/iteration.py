"""Kademeli iterasyon: bir sahnenin görsel aşaması.

1. Test turu: `probe_n` düşük çözünürlüklü varyasyon. Ortalama birleşik skor ve geçme oranı eşiği geçmezse yeni
   seed'lerle tekrar (en fazla `max_correction_rounds`); yine olmazsa sahne `needs_review`.
2. Tam tur: `wave_size`'lık dalgalar, `full_n` üst sınırına kadar. `excellent_threshold` üstünde `refined_k` aday
   bulununca durur; yumuşak bütçe limiti bayrağı varken yeni dalga başlamaz.
3. Rafine Havuz: yalnızca tam turda geçenlerden en iyi `refined_k` kalır, gerisi imha edilir.

Taslak geçiş (mode="draft", Faz 14): yalnızca test turu çalışır; geçen ilk varyasyon tek başına havuza girer ve kaba
kurguda kullanılır. Tam tur hiç çalışmaz. Sonraki iyileştirme geçişi aynı seed'lerle başladığı için test turu klipleri
yeniden kullanılır (yeniden üretilmez, yeniden ücretlendirilmez).

Önce görsel, sonra video (Faz 18): fal.ai sağlayıcısında (`image_first`: Seedance ve eski Wan çekimleri) her varyasyonun
ilk karesi ayrı üretilir ve aynı eşikle puanlanır. Geçemeyen kare kalite kapısında elenir (`params.stage="first_frame"`)
ve videosu hiç üretilmez; video yalnızca geçen kareden istenir. Varsayılan motor MiniMax H3'te (kendi GPU sunucumuz)
kompozisyon koşulla kontrol edilir: FL2VA'da çekimin kalite kapısından geçmiş storyboard karesi (ya da yönetmenin yüklediği
kare) her varyasyonun ilk karesidir, Ref2VA'da karakter referans seti de gider; videonun kendisi puanlanır. Ücretli modda
FL2VA çekiminin kullanılabilir ilk karesi yoksa üretim başlamaz (`keyframe_blocks`). Ücretsiz modda yerel motor doğrudan
video çizer.

Seed'ler girdilerden türetildiği için çalıştırma tekrarlanabilir: bir kesintiden (Kill-Switch, çökme) sonra aynı girdiyle
yeniden başlayan çalıştırma, tamamlanmış varyasyonları yeniden üretmez ve bütçeden yeniden düşmez.
"""
import logging
import os
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from statistics import mean

from sqlalchemy import and_, or_, select

from app import storage
from app.budget import killswitch
from app.config import project_settings, settings
from app.core.chain import camera_of, close_shot
from app.core.h3mode import effective_mode
from app.core.hashing import seed_for, visual_fingerprint
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine.providers import GenRequest, TransientError, get_provider, route_engine
from app.hermes import lint
from app.style import bible
from app.style.composer import Composed, compose_prompt
from app.models import Project, Scene, Variation
from app.quality import gate, scorer

log = logging.getLogger("hellgrind.iteration")

MAX_ATTEMPTS = 3            # geçici sağlayıcı hatalarında varyasyon başına deneme
THUMB_WIDTH = 320


@dataclass
class VarResult:
    id: str
    passed: bool
    composite: float | None
    anomalies: list = field(default_factory=list)


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
    target: object = None          # gerçek kalite skorunun ölçütü (scorer.Target)
    keyframe: str | None = None    # MiniMax H3: her varyasyonun ilk karesi (sağlayıcıya yüklenmiş hâli)
    last_frame: str | None = None  # FL2VA: son kare = sonraki çekimin ilk karesi (geçişi continuous ise; yüklenmiş hâli)


class ChainStale(Exception):
    """Zincirdeki bir kare değişti: bu çekimin videosu artık eski kareden üretiliyor; üretim durur."""


CHAIN_STALE = "hg:chain_stale:{}"     # çekim -> neden (anahtar kare değişti; süren üretim durmalı)


def chain_check(c) -> None:
    reason = killswitch.r().get(CHAIN_STALE.format(c.scene_id))
    if reason:
        raise ChainStale(reason.decode() if isinstance(reason, bytes) else reason)


# ---------- yardımcılar ----------

def make_thumb(video_path: str, key: str) -> str | None:
    """320 px genişliğinde jpg; 2. saniyeden, klip daha kısaysa (ya da tek kareyse) ilk kareden. Başarısızlık üretimi
    durdurmaz."""
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
    üretmez. Havuzdan emekli edilmiş (superseded / not_top_k) ya da hata vermiş olanlar yeniden kullanılmaz.
    Koşul (ilk ve son karenin içerik kimliği, `conditioning`) aynı olmalı: kare yeniden çizilince görsel özet ve seed'ler
    aynı kalır; Kıvılcım şerit onayından sonra SH03/SH04/SH06 eski karelerin elenmiş test klipleriyle hemen needs_review
    olmuştu."""
    usable = ("passed", "refined") if tier == "probe" else ("passed",)
    with session_scope() as s:
        v = s.scalars(select(Variation).where(
            Variation.scene_id == c.scene_id, Variation.seed == seed, Variation.tier == tier,
            Variation.params["resolution"].astext == resolution,
            or_(Variation.status.in_(usable),
                and_(Variation.status == "purged", Variation.params["purge_reason"].astext == "quality_gate")),
            Variation.params["stale_keyframe"].astext.is_(None),                  # eski kareden üretilenler değil
        ).order_by(Variation.created_at.desc()).limit(1)).first()
        if v is None:
            return None
        if v.status != "purged" and not (v.video_key and storage.exists(v.video_key)):
            return None
        if c.render.get("seam") and (v.params or {}).get("keyframe_key") != c.render.get("keyframe_key"):
            return None                                   # kesintisiz dikiş: başka bir son kareden üretilmiş
        if c.render.get("cond") and (v.params or {}).get("cond") != c.render["cond"]:
            return None                                   # başka bir ilk/son kareden üretilmiş (kare yeniden çizildi)
        s.expunge(v)
        return v


def compose_for_scene(scene, chars, assets, style, frame: str | None = None, entering=None) -> Composed:
    """Sağlayıcıya giden prompt: yalnızca bestecinin çıktısı (kılavuzun ışık ve hareket dili, çekim tarifi, varlıklar ve
    "yalnızca listelenen karakterler" kısıtı). Görüntü, storyboard karesi, previs aktarımı ve ilk kare düzeltmesi aynı
    fonksiyonu kullanır. frame: "start" (ilk kare) | "end" (son kare) | None (video): karenin durumu
    (core/states.frame_states; SH05 "eyes flicker and light up": ilk karede gözler karanlık, son karede yanık)."""
    from app.core.h3mode import is_human
    spec = scene.prompt_spec or {}
    return compose_prompt({"prompt": spec.get("prompt", ""), "negative": spec.get("negative", ""), "lang": spec.get("lang"),
                           "camera": prompt_camera(scene), "characters": [ch.name for ch in chars],
                           "note": getattr(scene, "director_note", None), "frame": frame,
                           "entering": list(entering or []) if getattr(scene, "entry_mode", None) == "walk_in" else [],
                           "nonhuman": [ch.name for ch in chars if not is_human(ch)]}, assets, style)


def prompt_camera(scene) -> dict:
    """İstemdeki kamera. Kare kırp + iyileştir ile çizildiyse (board_method cut_in) kare aynı kameranın yakın kırpımıdır:
    yan açı ve dikey açı yazılmaz (Kıvılcım SH03: "35° to the left, low angle" son karede ve videoda kamerayı döndürdü,
    lamba ve masa göründü)."""
    camera = camera_of(scene)
    if getattr(scene, "board_method", None) in ("cut_in", "upload"):
        camera = {k: v for k, v in camera.items() if k not in ("side", "angle")}
    return camera


def still_of(ch) -> str | None:
    """Karakterin durağan referans görseli (Varlıklar sayfasında "Durağan görsel" olarak yüklenen)."""
    return next((k for k in ch.reference_images or [] if "/still/" in k), None)


def reference_of(chars) -> dict | None:
    """Ücretli ilk kare ve storyboard karesi için karakter referansı: LoRA'sı olmayan ve durağan görseli olan ilk
    karakter. Kontext tek görsel alır; LoRA varsa LoRA kazanır (karakter zaten eğitilmiş)."""
    for ch in chars:
        if not ch.lora_url and (key := still_of(ch)):
            return {"name": ch.name, "key": key}
    return None


def reference_set(chars, limit: int = 9) -> list[dict]:
    """Ref2VA için karakter referans seti: her karakterin durağan görseli, ifade sayfası ve dönüş videosundan üç kare
    (önden, yandan, arkadan). Yer karakterler arasında sırayla dağıtılır (en çok `limit`); çıktı karaktere göre gruplu."""
    picks = []
    for ch in chars:
        keys = list(ch.reference_images or [])
        frames = [k for k in keys if "/frames/" in k]
        chosen = [k for k in keys if "/still/" in k][:1] + [k for k in keys if "/expressions/" in k][:1]
        chosen += [frames[i] for i in sorted({0, len(frames) // 3, 2 * len(frames) // 3})] if frames else []
        picks.append((ch.name, chosen))
    quota, left = [0] * len(picks), limit
    while left and any(quota[i] < len(keys) for i, (_, keys) in enumerate(picks)):
        for i, (_, keys) in enumerate(picks):
            if left and quota[i] < len(keys):
                quota[i] += 1
                left -= 1
    return [{"name": name, "key": key} for (name, keys), n in zip(picks, quota) for key in keys[:n]]


def loras_of(chars) -> dict:
    """Karakter kimliğinin sağlayıcıya giden biçimleri: Flux için LoRA'lar ve tek Kontext referansı; MiniMax H3 (Ref2VA)
    için çekimdeki bütün karakterlerin durağan görselleri."""
    out = {"loras": [{"path": ch.lora_url, "scale": ch.lora_scale} for ch in chars if ch.lora_url],
           "lora_triggers": [ch.lora_trigger for ch in chars if ch.lora_trigger],
           "stills": [{"name": ch.name, "key": key} for ch in chars if (key := still_of(ch))],
           "reference_set": reference_set(chars)}
    if (reference := reference_of(chars)) is not None:
        out["reference"] = reference
    return out


# ---------- tek varyasyon ----------

def run_one(c: Ctx, tier: str, wave_no: int, idx: int, resolution: str, seed: int | None = None,
            force: dict | None = None) -> VarResult:
    """seed: yönetmenin seçtiği adayın seed'i (geçersiz kılma: aynı seed ile tam tur); force: kapıdan kalsa da kabul
    edilir (yönetmen seçimi), {"from": aday kimliği}."""
    if killswitch.is_frozen(c.project_id):                                             # 1. Frozen kontrolü
        raise killswitch.ProjectFrozen(c.project_id)
    chain_check(c)                                                                     #    zincir karesi değişmedi mi
    if seed is None:
        seed_parts = [c.scene_id, c.visual_hash, tier, wave_no, idx]                   # 2. Seed
        seed = seed_for(*seed_parts, *([c.revision] if c.revision else []))            #    ("baştan üret" yeni seed'ler verir)

    done = None if force else _reusable(c, seed, tier, resolution)
    if done is not None:
        _merge_params(done.id, run=c.run_id)
        return VarResult(str(done.id), done.status in ("passed", "refined"), done.composite_score, list(done.anomalies or []))

    label = f"{c.render.get('label', '')}  {tier} {resolution} #{seed % 10000}"
    req = GenRequest(prompt=c.prompt_spec.get("prompt", ""), negative=c.prompt_spec.get("negative", ""), seed=seed,
                     resolution=resolution, duration_s=c.duration, image_url=c.keyframe,
                     extra={**c.render, "label": label, **({"last_frame": c.last_frame} if c.last_frame else {})})
    with session_scope() as s:                                                         # 3. Variation satırı
        v = Variation(scene_id=c.scene_id, tier=tier, wave_no=wave_no, engine=c.provider.engine, seed=seed,
                      prompt_revision=c.revision, status="queued",
                      params={"resolution": resolution, "duration_s": c.duration, "prompt": req.prompt,
                              "negative": req.negative, "run": c.run_id,
                              **({"cond": c.render["cond"]} if c.render.get("cond") else {}),
                              **({"director_override": force} if force else {}),
                              **({"keyframe_key": c.render["keyframe_key"], "h3_mode": c.render["h3_mode"]}
                                 if (c.keyframe or c.render.get("seam")) and c.render.get("keyframe_key") else {}),
                              **({"last_frame_key": c.render["last_frame_key"],
                                  "last_frame_kind": c.render.get("last_frame_kind")} if c.last_frame else {})})
        s.add(v)
        s.flush()
        vid = v.id
    try:
        frame_cost = 0.0
        if getattr(c.provider, "image_first", False) and not c.keyframe:               # önce görsel, sonra video
            frame = _first_frame(c, req, vid, seed)
            if not isinstance(frame, tuple):
                return frame                                                           # kare elendi ya da üretilemedi
            url, frame_cost = frame
            req = replace(req, image_url=url)
        result = _generate(c, req, vid)                                                # 4. spend() içinde üretim
        if result is None:
            _set_variation(vid, status="failed", cost_usd=frame_cost)                  # ödenmiş kare bedeli görünsün
            return VarResult(str(vid), False, None)
        # Ödeme yapıldı: kimlik ve maliyet, yükleme çökse bile denetim için hemen kaydedilir.
        _set_variation(vid, status="generated", external_request_id=result.request_id,
                       cost_usd=round(frame_cost + result.cost_usd, 4))
        try:
            key = f"variations/{c.scene_id}/{vid}.mp4"                                 # 5. Depo ve küçük resim
            storage.put_file(result.local_path, key)
            thumb_key = make_thumb(result.local_path, f"thumbs/{c.scene_id}/{vid}.jpg")
            score = gate.score_video(result.local_path, seed, c.cfg, c.target)         # 6. Skorlama
        finally:
            os.remove(result.local_path)
    except BaseException:
        _set_variation(vid, status="failed")                                           # "running" satırı asılı kalmasın
        raise

    extra = {"provider_meta": result.meta, **({"score_detail": score.detail} if score.detail else {})}
    if score.passed or force:                                                          # yönetmen seçimi: kapı kaydedilir
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
        for name, value in fields.items():
            setattr(v, name, value)
    return VarResult(str(vid), score.passed or bool(force), score.composite, list(score.anomalies))


def _first_frame(c: Ctx, req: GenRequest, vid, seed: int):
    """İlk kareyi üretir, depoya koyar ve puanlar. Geçerse (kare adresi, bedeli); elenirse ya da üretilemezse VarResult.
    Elenen karenin videosu hiç istenmez: kare karantinaya taşınır, küçük resmi panelde kalır."""
    _set_variation(vid, status="running")
    model = "flux_kontext" if req.extra.get("reference") else "flux_lora"
    frame = _attempts(c, c.provider.estimate_frame(req), f"{model}:first_frame:{req.resolution}", vid,
                      lambda: c.provider.first_frame(req))
    if frame is None:
        _set_variation(vid, status="failed")
        return VarResult(str(vid), False, None)
    key = f"frames/{c.scene_id}/{vid}.jpg"
    try:
        storage.put_file(frame.local_path, key, "image/jpeg")
        thumb = make_thumb(frame.local_path, f"thumbs/{c.scene_id}/{vid}.jpg")
        score = gate.score_frame(frame.local_path, seed, c.cfg, c.target)
    finally:
        os.remove(frame.local_path)
    extra = {"first_frame_key": key, "first_frame_request_id": frame.request_id, "frame_score": score.composite,
             **({"frame_score_detail": score.detail} if score.detail else {})}
    if score.passed:
        _merge_params(vid, **extra)
        return frame.url, frame.cost_usd
    extra.update(stage="first_frame", purge_reason="quality_gate", quarantine_key=storage.purge(key))
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.status, v.keyframe_key, v.cost_usd = "purged", thumb, frame.cost_usd
        v.external_request_id = frame.request_id
        v.clip_score, v.face_score, v.composite_score = score.clip, score.face, score.composite
        v.anomalies = score.anomalies
        v.params = {**(v.params or {}), **extra}
    return VarResult(str(vid), False, score.composite, list(score.anomalies))


def _attempts(c: Ctx, estimate: float, label: str, vid, call):
    """Geçici hatalarda en fazla MAX_ATTEMPTS deneme; her deneme kendi rezervasyonunu alır, başarısızsa iade eder.
    İşlenen tutar sağlayıcının bildirdiği bedeldir (`cost_usd`)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        chain_check(c)                              # iptal edilen (eski kareden) işin yeniden denemesi gönderilmez
        try:
            with killswitch.spend(c.project_id, estimate, label, scene_id=c.scene_id, variation_id=vid,
                                  details={"attempt": attempt}) as charge:
                result = call()
                charge.actual = result.cost_usd
            return result
        except TransientError as e:
            log.warning("geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
    return None


def _generate(c: Ctx, req: GenRequest, vid):
    _set_variation(vid, status="running")
    return _attempts(c, c.provider.estimate(req), f"{c.provider.engine}:{req.resolution}", vid,
                     lambda: c.provider.generate(req))


# ---------- dalga ----------

def run_wave(c: Ctx, tier: str, wave_no: int, n: int, resolution: str) -> list[VarResult]:
    """n varyasyonu `gen_concurrency` iş parçacığıyla üretir. Donma ya da başka hata, dalga bitince yükseltilir.
    Kuyruklu sağlayıcı (RunPod Serverless, `whole_wave`) dalganın bütün işlerini birlikte gönderir: soğuk başlatma işler
    arasında paylaşılır, eşzamanlılığı uç noktanın worker sayısı belirler."""
    results, frozen, error = [], None, None
    workers = n if getattr(c.provider, "whole_wave", False) else settings.gen_concurrency
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
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

def _prepare(project_id: str, scene_id: str, run_id: str, seam_key: str | None = None) -> Ctx:
    killswitch.r().delete(CHAIN_STALE.format(scene_id))   # yeni çalıştırma güncel karelerle başlar
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
        camera = camera_of(scene)
        spec = dict(scene.prompt_spec or {})
        from app.core.chain import plan_of
        walk_in = plan_of(s, scene)["entering"] if scene.entry_mode == "walk_in" else None    # giriş eylemi videoda
        composed = compose_for_scene(scene, chars, assets, style, entering=walk_in)   # görsel özetteki girdilerden
        spec["prompt"], spec["negative"] = composed.prompt, composed.negative
        # Yarım kalmış önceki çalıştırmanın asılı satırları: kimse üstünde çalışmıyor.
        for v in s.scalars(select(Variation).where(Variation.scene_id == scene_id,
                                                   Variation.status.in_(("queued", "running", "generated")))):
            v.status = "failed"
            v.params = {**(v.params or {}), "purge_reason": "interrupted"}
        visual_hash = visual_fingerprint(scene, chars, assets, style.version if style else None)
        ctx = Ctx(
            project_id=project_id, scene_id=scene_id, run_id=run_id, provider=get_provider(scene.engine), cfg=cfg,
            duration=scene.duration_s, prompt_spec=spec, revision=scene.prompt_revision, visual_hash=visual_hash,
            render={"emotion": scene.emotion, "camera": camera, "characters": [ch.name for ch in chars],
                    "lighting": style.lighting_prefix if style else "",
                    "action": scene.action_complexity,
                    **loras_of(chars),
                    "project_id": project_id, "scene_id": str(scene_id), "h3_mode": effective_mode(scene, chars, assets),
                    "audio": cfg.get("audio", "on") != "off",
                    "label": f"{scene.scene_code}  {camera.get('shot', '')} {camera.get('movement', '')}"},
            target=scorer.target_for(scene, chars, style))
        keyframe_key = seam_key or keyframe_of(scene, visual_hash)
        last_key, last_kind = last_frame_of(s, scene, style.version if style else None, cfg) \
            if keyframe_key and ctx.render["h3_mode"] == "fl2va" and (scene.engine or "h3") == "h3" else (None, None)
        if seam_key:
            ctx.render["seam"] = True                     # kesintisiz dikiş: ilk kare önceki klibin gerçek son karesi
        ctx.render["cond"] = conditioning(s, scene, keyframe_key, last_key, last_kind)
    if keyframe_key and hasattr(ctx.provider, "upload"):
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:   # bir kez yüklenir, bütün varyasyonlar kullanır
            ctx.keyframe = ctx.provider.upload(storage.download(keyframe_key, os.path.join(tmp, "keyframe.jpg")))
            if last_key:                                  # çekimin son karesi sonraki çekimin ilk karesi olsun
                ctx.last_frame = ctx.provider.upload(storage.download(last_key, os.path.join(tmp, "last.jpg")))
    ctx.render["keyframe_key"] = keyframe_key
    if keyframe_key and close_shot(ctx.render["camera"]):     # yakın plan: stil, onaylı ilk kareye göre (ölçek eşleşen)
        ctx.target = replace(ctx.target, style_frame=keyframe_key)
    if ctx.last_frame:
        ctx.render["last_frame_key"], ctx.render["last_frame_kind"] = last_key, last_kind
        ctx.target = replace(ctx.target, next_frame=last_key, arrival_kind=last_kind)
    return ctx


def run_override_full(project_id: str, scene_id: str, variation_id: str) -> str:
    """Yönetmen seçimi + 720p tam tur: seçilen adayın seed'iyle tam tur çözünürlüğünde tek klip; kapıdan kalsa da kabul
    edilir (ölçümleri kaydedilir) ve Rafine Havuz'un 1. sırasına girer. Ardından zincir dudak senkronu ve montajı yapar."""
    gate.ensure_scorer()
    with session_scope() as s:
        seed = s.get(Variation, variation_id).seed
    ctx = _prepare(project_id, scene_id, uuid.uuid4().hex)
    advance_scene(project_id, scene_id, status="generating")
    result = run_one(ctx, "full", 0, 0, ctx.cfg["full_resolution"], seed=seed, force={"from": str(variation_id)})
    with session_scope() as s:
        v = s.get(Variation, result.id)
        if v is None or v.status != "passed" or not v.video_key:
            scene = s.get(Scene, scene_id)
            scene.status, scene.last_error = "needs_review", "Yönetmen seçimi: tam tur klibi üretilemedi."
            return "needs_review"
        gate.promote_override(s, scene_id, result.id, {
            "status": "refined", "visual_hash": ctx.visual_hash, "lipsync_video_key": None, "final_video_key": None,
            "last_error": None, "cut_tier": "final"}, int(ctx.cfg["refined_k"]),
            {"from": str(variation_id), "seed": seed, "full": True})
        code = s.get(Scene, scene_id).scene_code
    killswitch.event(project_id, "director_override", scene_id=scene_id, code=code, variation=str(result.id), full=True)
    return "refined"


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


def keyframe_of(scene, fp: str) -> str | None:
    """Videoya giden ilk kare: yönetmenin yüklediği kare, yoksa bu görsel özetin storyboard karesinin yazısız hâli.
    MiniMax H3 (FL2VA) her zaman; Seedance (image-to-video) kare RunPod'da (Qwen-Image-Edit ya da Qwen-Image) çizildiyse:
    yoksa Seedance her varyasyon için kendi Flux karesini üretir (önce görsel, sonra video)."""
    from app.engine.storyboard import SHARED_SOURCES, clean_key
    engine = scene.engine or "h3"
    if engine not in ("h3", "seedance"):
        return None
    if scene.keyframe_key:
        return scene.keyframe_key
    if engine == "seedance" and scene.board_source not in SHARED_SOURCES:
        return None
    if scene.board_key and scene.board_hash == fp and scene.board_status != "changes_requested":
        key = clean_key(scene.board_key)
        return key if storage.exists(key) else None
    return None


def conditioning(s, scene, keyframe_key: str | None, last_key: str | None, last_kind: str | None) -> str | None:
    """Klibin koşul kimliği: ilk ve son karenin içerik kimliği (SHA; yüklenen kare keyframe_hash, dikiş karesi anahtarı).
    Kare yeniden çizilince değişir (anahtar ve görsel özet aynı kalsa da): eski koşulun klipleri yeniden kullanılmaz,
    yönetmen geçersiz kılmasında "eski" sayılır."""
    from app.core.chain import next_continuous
    from app.core.hashing import stable_hash
    if not keyframe_key:
        return None
    if scene.keyframe_key and keyframe_key == scene.keyframe_key:
        first = scene.keyframe_hash or keyframe_key
    elif keyframe_key.startswith("seam/"):
        first = keyframe_key
    else:
        first = scene.board_sha or keyframe_key
    last = None
    if last_key:
        if last_kind == "end":
            last = scene.end_board_sha or last_key
        else:
            nxt = next_continuous(s, scene)
            last = (nxt.board_sha if nxt is not None else None) or last_key
    return stable_hash([first, last])[:16]


def current_conditioning(s, scene) -> str | None:
    """Bugünkü koşul (API: yönetmen seçiminde adayın eski olup olmadığı)."""
    style = bible.active_version(s, scene.project_id)
    fp = visual_fingerprint(scene, load_characters(s, scene.id), load_assets(s, scene.id), style)
    keyframe_key = keyframe_of(scene, fp)
    cfg = project_settings(s.get(Project, scene.project_id).settings)
    last_key, last_kind = last_frame_of(s, scene, style, cfg) \
        if keyframe_key and effective_mode(scene) == "fl2va" and (scene.engine or "h3") == "h3" else (None, None)
    return conditioning(s, scene, keyframe_key, last_key, last_kind)


def last_frame_of(s, scene, style_version, cfg: dict | None = None) -> tuple[str | None, str | None]:
    """FL2VA last_frame (anahtar kare zinciri; core/chain.py): (anahtar, tür).
    - Sonraki çekimin geçişi continuous: onun ilk karesi K(n+1) (bu görsel özet için, incelemede değil): "next".
    - Değilse (kesme, yeni sahne, sekansın sonu): çekimin kendi son karesi Kn_end (güncel ve kapıdan geçmişse): "end".
      Video sonraki kareye "yolculuk" yapmaz; Kn_end yoksa son kare serbest (None)."""
    from app.core.chain import next_continuous
    from app.engine.storyboard import clean_key, end_usable
    nxt = next_continuous(s, scene)
    if nxt is None:
        if not (cfg or {}).get("chain_end_frame", True):
            return None, None
        fp = visual_fingerprint(scene, load_characters(s, scene.id), load_assets(s, scene.id), style_version)
        return (clean_key(scene.end_board_key), "end") if end_usable(scene, fp) else (None, None)
    if not nxt.board_key or nxt.board_status == "changes_requested":
        return None, None
    if nxt.board_hash != visual_fingerprint(nxt, load_characters(s, nxt.id), load_assets(s, nxt.id), style_version):
        return None, None
    key = clean_key(nxt.board_key)
    return (key, "next") if storage.exists(key) else (None, None)


def seam_outdated(s, scene) -> str | None:
    """Kazanan klip bugünkü dikiş kuralına göre mi üretildi: son karesi (FL2VA last_frame) şimdi beklenenle aynı değilse
    (geçiş sınıfı değişti, çekim sonu karesi Kn_end eklendi ya da değişti) gerekçe; değilse None. Yalnızca ilk karesi
    sağlayıcıya yüklenmiş (ücretli H3 FL2VA) kliplerde; ücretsiz modun yerel klipleri ve kesintisiz dikiş hariç.
    Kıvılcım SH01: video kesme kuralından önce last_frame = K2 ile üretilmişti, şerit onayında yeniden planlanmadı."""
    if (scene.engine or "h3") != "h3" or (scene.method or "generate") != "generate":
        return None
    winner = s.scalars(select(Variation).where(Variation.scene_id == scene.id, Variation.refined_rank == 1,
                                               Variation.status == "refined")).first()
    params = (winner.params or {}) if winner is not None else {}
    if winner is None or params.get("stale_keyframe") or not params.get("keyframe_key") \
            or str(params["keyframe_key"]).startswith("seam/") or params.get("h3_mode") == "ref2va":
        return None
    project = s.get(Project, scene.project_id)
    expected, kind = last_frame_of(s, scene, bible.active_version(s, scene.project_id),
                                   project_settings(project.settings))
    if (params.get("last_frame_key") or None) == (expected or None):
        return None
    was = {"next": "sonraki çekimin karesi", "end": "çekimin son karesi"}.get(params.get("last_frame_kind"),
                                                                            "sonraki çekimin karesi" if params.get("last_frame_key") else "serbest")
    now = {"next": "sonraki çekimin karesi", "end": "çekimin son karesi"}.get(kind, "serbest")
    return f"son kare kuralı değişti ({was} → {now})"


def mark_seam_outdated(s, scene) -> bool:
    """seam_outdated ise çekimin klipleri "eski kareden" işaretlenir (silinmez, yeniden kullanılmaz) ve görsel özet
    sıfırlanır: plan VISUAL der. Olay yazılır."""
    reason = seam_outdated(s, scene)
    if reason is None:
        return False
    for v in s.scalars(select(Variation).where(Variation.scene_id == scene.id,
                                               Variation.status.in_(("passed", "refined", "generated")))):
        if not (v.params or {}).get("stale_keyframe"):
            v.params = {**(v.params or {}), "stale_keyframe": reason}
    scene.visual_hash = None
    s.flush()
    killswitch.event(str(scene.project_id), "seam_outdated", scene_id=str(scene.id), code=scene.scene_code, reason=reason)
    return True


SEAM_WAIT_S = 6 * 3600         # kesintisiz dikiş: önceki çekimin kazanan klibini en çok bu kadar bekle
SEAM_POLL_S = 10.0
SEAM_WAITING = ("pending", "parsed", "storyboard", "probing", "generating", "correcting")


def exact_seam(s, scene, cfg: dict) -> tuple[str, object, object]:
    """Kesintisiz dikiş (proje ayarı seam_mode=exact): continuous çekimin ilk karesi = önceki çekimin kazanan klibinin
    gerçek son karesi (sıralı üretim). (durum, önceki çekim, kazanan): "off" (uygulanmaz), "ready", "wait" (önceki çekim
    hâlâ üretiliyor), "stop" (önceki çekimin kazananı yok ve üretilmiyor)."""
    from app.core.chain import neighbours, transition_of
    if cfg.get("seam_mode", "keyframe") != "exact" or transition_of(scene) != "continuous" \
            or (scene.engine or "h3") != "h3" or (scene.method or "generate") != "generate":
        return "off", None, None
    previous, _ = neighbours(s, scene)
    if previous is None:
        return "off", None, None
    winner = s.scalars(select(Variation).where(Variation.scene_id == previous.id, Variation.refined_rank == 1,
                                               Variation.status == "refined")).first()
    if winner is not None and winner.video_key and not (winner.params or {}).get("stale_keyframe") \
            and previous.status not in SEAM_WAITING:
        return "ready", previous, winner
    return ("wait" if previous.status in SEAM_WAITING else "stop"), previous, winner


def seam_frame(winner) -> str:
    """Kazanan klibin gerçek son karesi, depoda (içerik-adresli: varyasyon kimliği): seam/{varyasyon}.jpg."""
    key = f"seam/{winner.id}.jpg"
    if storage.exists(key):
        return key
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        video = storage.download(winner.video_key, os.path.join(tmp, "clip.mp4"))
        out = os.path.join(tmp, "last.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.1", "-i", video, "-frames:v", "1", "-q:v", "2", out],
                       check=True, capture_output=True)
        storage.put_file(out, key, "image/jpeg")
    return key


def wait_for_seam(project_id: str, scene_id: str) -> tuple[str, str | None]:
    """Kesintisiz dikişte önceki çekimin kazanan klibini bekler. ("off" | "ready" | "stop", ilk kare anahtarı)."""
    import time
    deadline = time.monotonic() + SEAM_WAIT_S
    while True:
        with session_scope() as s:
            scene = s.get(Scene, scene_id)
            state, previous, winner = exact_seam(s, scene, project_settings(s.get(Project, project_id).settings))
            code = previous.scene_code if previous is not None else ""
            if state == "ready":
                s.expunge(winner)
        if state == "off":
            return "off", None
        if state == "ready":
            return "ready", seam_frame(winner)
        if state == "stop" or time.monotonic() > deadline:
            advance_scene(project_id, scene_id, status="board_review",
                          last_error=f"Kesintisiz dikiş: bu çekimin ilk karesi önceki çekimin ({code}) kazanan klibinin "
                                     f"son karesidir; {code} henüz bitmedi ya da havuzu boş. Önce {code}'i üretin.")
            killswitch.event(project_id, "seam_waiting", scene_id=scene_id, previous=code, stopped=True)
            return "stop", None
        if killswitch.is_frozen(project_id):
            raise killswitch.ProjectFrozen(project_id)
        time.sleep(SEAM_POLL_S)


def invalidate_next_seam(project_id: str, scene_id: str) -> None:
    """Kesintisiz dikişte çekimin kazananı değişti: sonraki (continuous) çekimin önceki son kareden üretilmiş klipleri
    eski sayılır ve çekim yeniden üretilmek üzere işaretlenir."""
    from app.core.chain import next_continuous
    from app.engine.storyboard import FINISHED
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        if project_settings(s.get(Project, project_id).settings).get("seam_mode", "keyframe") != "exact":
            return
        nxt = next_continuous(s, scene)
        winner = s.scalars(select(Variation).where(Variation.scene_id == scene_id, Variation.refined_rank == 1)).first()
        if nxt is None or winner is None:
            return
        current, marked = f"seam/{winner.id}.jpg", 0
        for v in s.scalars(select(Variation).where(Variation.scene_id == nxt.id,
                                                   Variation.status.in_(("passed", "refined", "generated")))):
            key = (v.params or {}).get("keyframe_key") or ""
            if key.startswith("seam/") and key != current and not (v.params or {}).get("stale_keyframe"):
                v.params = {**(v.params or {}), "stale_keyframe": f"{scene.scene_code} kazananı değişti"}
                marked += 1
        if marked:
            nxt.visual_hash = None
            if nxt.status in FINISHED:
                nxt.status = "needs_review"
                nxt.last_error = f"Klip eski dikişten üretildi ({scene.scene_code} kazananı değişti); yeniden üretin."
            killswitch.event(project_id, "chain_invalidated", scene_id=str(nxt.id), reason="seam", codes=[nxt.scene_code],
                             clips=marked)


def keyframe_blocks(project_id: str, scene_id: str) -> bool:
    """Ücretli modda MiniMax H3 FL2VA çekiminin ilk karesi hazır değilse görüntü üretilmez: storyboard karesi yoksa ya da
    karakterli çekimin karesi metinden çizilmişse önce (karakter referansından) çizilir ve kalite kapısından geçer;
    kullanılamıyorsa sahne onaya kalır ve neden Türkçe yazılır. Seedance çekimi de aynı kareyi kullanır: RunPod'da
    çizilebiliyorsa çizilir; kalite kapısından kalırsa video üretilmez, yoksa her varyasyon kendi Flux karesini üretir."""
    from app.core.chain import parent_of, stale
    from app.core.h3mode import keyframe_model, strict_refs
    from app.engine.storyboard import (SHARED_SOURCES, board_source, chain_enabled, end_fresh, end_wanted,
                                       keyframe_problem, make_board, outdated)
    if settings.dry_run:
        return False
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        chars, assets = load_characters(s, scene_id), load_assets(s, scene_id)
        engine = scene.engine or "h3"
        if engine not in ("h3", "seedance") or (scene.method or "generate") != "generate" \
                or (engine == "h3" and effective_mode(scene, chars, assets) == "ref2va"):
            return False                                  # Ref2VA ilk kareyi kullanmaz: kare çizilmez
        style = bible.active_version(s, project_id)
        fp = visual_fingerprint(scene, chars, assets, style)
        strict = strict_refs(s.get(Project, project_id).settings)
        parent = parent_of(s, scene) if chain_enabled() else None   # anahtar kare zinciri: kare önceki karesinden
        model = "qwen_edit" if parent is not None else keyframe_model(scene, chars, assets, strict)
        source = None if scene.keyframe_key else board_source("fl2va", False, model)
        current = bool(scene.board_key and scene.board_hash == fp)
        missing = not scene.keyframe_key and (not current or outdated(scene.board_source, source)
                                              or (parent is not None and stale(scene, parent)))
        # çekim sonu karesi (Kn_end) eksik ya da eski: make_board Kn güncelse yalnızca onu çizer
        end_missing = not scene.keyframe_key and current and end_wanted(
            s, scene, project_settings(s.get(Project, project_id).settings), chars, assets) and not end_fresh(scene, fp)
    if engine == "seedance" and source not in SHARED_SOURCES and not scene.keyframe_key:
        return False                                      # Seedance kendi ilk karesini üretir (fal.ai Flux)
    if missing or end_missing:
        make_board(project_id, scene_id)
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        problem = keyframe_problem(scene, fp)
        parent = parent_of(s, scene) if chain_enabled() else None
        if problem is None and parent is not None and not scene.keyframe_key:
            if parent.board_status == "changes_requested":   # zincir durdu: eski kareden video üretilmez
                problem = (f"Zincirdeki önceki kare ({parent.scene_code}) inceleme bekliyor: bu çekimin karesi ondan "
                           f"çizilir. {parent.scene_code} karesini onaylayın ya da yeniden çizdirin.")
            elif stale(scene, parent):                       # yeniden çizilemedi: kare hâlâ eski
                problem = (f"Kare eski: zincirdeki önceki kare ({parent.scene_code}) değişti ve bu kare yeniden "
                           "çizilemedi. Video eski kareden üretilmez.")
    if problem is None:
        return False
    advance_scene(project_id, scene_id, status="board_review", last_error=problem)
    killswitch.event(project_id, "keyframe_required", scene_id=scene_id)
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


def review_reason(results, cfg: dict, stage: str) -> str:
    """needs_review gerekçesi. Adayların çoğu kimlik kapısında kaldıysa "Kimlik düşük" denir (yüz görünmedi ya da karakter
    benzerliği eşiğin altında): çözüm referanslarda ve koşuldadır, yeni seed'de değil."""
    failed = [r for r in results if not r.passed]
    identity = [r for r in failed if any(a.get("type") in gate.IDENTITY_ANOMALIES for a in (r.anomalies or []))]
    if failed and len(identity) * 2 >= len(failed):
        return (f"Kimlik düşük: {stage} adaylarının çoğunda karakterin yüzü görünmedi ya da benzerlik eşiğin "
                f"({float(cfg.get('identity_threshold', 0.6)):.2f}) altında. Ref2VA referanslarını ve çekimin "
                "'yüz görünmeli' ayarını gözden geçirin.")
    return "test turu eşiği geçilemedi" if stage == "test turu" else "tam turda eşiği geçen varyasyon yok"


def run_tiered_generation(project_id: str, scene_id: str, mode: str = "full") -> str:
    """Sahnenin görsel aşamasını yürütür. mode="draft": yalnızca test turu, geçen ilk varyasyon havuza girer.
    Dönen değer: "refined", "needs_review", "blocked" ya da "board_review"."""
    if lint_blocks(project_id, scene_id):
        return "blocked"
    if board_blocks(project_id, scene_id):
        return "board_review"
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        method = scene.method or "generate"
        if method == "previs_guide":
            style = bible.active(s, project_id)
            visual_hash = visual_fingerprint(scene, load_characters(s, scene_id), load_assets(s, scene_id),
                                             style.version if style else None)
            s.expunge_all()
    if method == "external":                              # planlayıcı planlamaz; görüntü yüklenirken kaydedildi
        return "external"
    if method == "previs_guide":                          # yönlendirici: rehberli çekim üretilmez, rehberden çıkar
        from app.engine.methods import run_guide
        return run_guide(project_id, scene_id, visual_hash, style)
    gate.ensure_scorer()                                  # para harcanmadan önce (rehber ve dış çekim skor kullanmaz)
    seam, seam_key = wait_for_seam(project_id, scene_id)  # kesintisiz dikiş: önceki çekimin gerçek son karesi
    if seam == "stop":
        return "board_review"
    if seam_key is None and keyframe_blocks(project_id, scene_id):   # ilk kare çizilecekse skorlayıcı hazır olduktan sonra
        return "board_review"
    ctx = _prepare(project_id, scene_id, uuid.uuid4().hex, seam_key)
    try:
        return _tiered(ctx, project_id, scene_id, mode)
    except ChainStale as e:                               # zincirdeki kare değişti: eski kareden video üretilmez
        advance_scene(project_id, scene_id, status="board_review",
                      last_error=f"Üretim durdu: {e}. Kare zincirle yeniden çizilip video yeniden üretilecek.")
        killswitch.event(project_id, "chain_stale_stop", scene_id=scene_id, reason=str(e))
        return "board_review"


def _tiered(ctx: Ctx, project_id: str, scene_id: str, mode: str) -> str:
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
        advance_scene(project_id, scene_id, status="needs_review", last_error=review_reason(results, cfg, "test turu"))
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
        advance_scene(project_id, scene_id, status="needs_review",
                      last_error=review_reason(gate.run_results(scene_id, ctx.run_id), cfg, "tam tur"))
        return "needs_review"
    killswitch.event(project_id, "refined", scene_id=scene_id, pool=len(winners))
    invalidate_next_seam(project_id, scene_id)
    return "refined"
