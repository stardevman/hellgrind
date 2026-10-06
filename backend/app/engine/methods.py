"""Çekim üretim yöntemleri ve elle düzeltme (Faz 15).

generate      tamamen yapay zekâ (kademeli üretim, engine/iteration.py)
previs_guide  yüklenen 3D taslak video rehberdir. Ücretsiz modda rehber, stil kılavuzunun ışık diline göre renklendirilip
              doğrudan kullanılır; ücretli modda bestecinin prompt'uyla Wan 2.2 video-to-video stil aktarımından geçer
              (Faz 18). Bedel iki modda da o uç noktanın fiyatıdır (ücretsiz modda sanal).
external      dışarıda bitirilmiş çekim yüklenir: görüntü üretilmez, klip yine anomali kontrolünden, sesten ve
              montajdan geçer. Dosyanın içerik özeti görsel özetin yerine geçer.

Elle düzeltme: üretilmiş bir varyasyonun düzenlenmiş videosu ya da ilk karesi yüklenir. Her revizyon sürümlenir, orijinal
korunur; rafine #1'in revizyonu yalnızca dudak senkronu ve montajı yeniden çalıştırır. İlk kare düzeltmesi ücretsiz
modda klibin başında durup orijinale erir; ücretli modda düzenlenmiş kareden aynı prompt ve seed'le video yeniden üretilir.
"""
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import replace

from sqlalchemy import func, select

from app import storage
from app.budget import killswitch, pricing
from app.config import project_settings, settings
from app.core.state import advance_scene
from app.db import session_scope
from app.engine.iteration import make_thumb
from app.engine.procedural import lighting_look
from app.engine.providers import DEFAULT_ENGINE
from app.models import Project, Revision, Scene, Variation
from app.quality import gate
from app.quality.anomaly import detect_anomalies, probe_duration

METHODS = ("generate", "previs_guide", "external")
PREVIS_HINT_COMPLEXITY = 0.8
SPECIAL_MOVES = ("handheld", "tracking", "orbit", "crane")
# Işık görünümü -> FFmpeg renk ayarı (rehber videoya stil kılavuzunun renk dili uygulanır)
GRADES = {
    "golden_hour": "colorbalance=rs=0.14:gs=0.04:bs=-0.14:rm=0.08:bm=-0.08,eq=saturation=1.12:gamma=1.03",
    "night": "colorbalance=rs=-0.10:bs=0.16:bm=0.08,eq=brightness=-0.08:saturation=0.8",
    "overcast": "eq=saturation=0.72:contrast=0.95",
}
FIRST_FRAME_HOLD_S, FIRST_FRAME_FADE_S = 0.5, 0.5
V2V_FPS, V2V_FRAMES = 16, (17, 161)       # Wan 2.2 video-to-video (fal_video.WAN_FPS, WAN_FRAMES ile aynı)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True)


def video_size(path: str) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    stream = json.loads(out)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def suggest_method(scene) -> str | None:
    """Yönlendiricinin önerisi: aksiyon çok yoğun ve hareket özelse previs rehberi. Karar yönetmenindir."""
    camera = scene.camera or {}
    if (scene.method or "generate") == "generate" and (scene.action_complexity or 0) >= PREVIS_HINT_COMPLEXITY \
            and camera.get("movement") in SPECIAL_MOVES:
        return "previs_guide"
    return None


def grade_filter(lighting_prefix: str | None) -> str:
    look = lighting_look(lighting_prefix)
    return GRADES.get(look["name"], "null") if look else "null"


def grade(src: str, dst: str, lighting_prefix: str | None, width: int, height: int) -> str:
    """Rehber videoyu stilin renk diline ve çekim çözünürlüğüne getirir (ses atılır; ses montajda eklenir)."""
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
          f"{grade_filter(lighting_prefix)},format=yuv420p")
    _ffmpeg("-i", src, "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart", dst)
    return dst


# ---------- dış çekim ----------

def register_external(scene_id: str, fingerprint: str) -> str:
    """Yüklenmiş dış çekimi rafine #1 yapar (para harcanmaz). Aynı dosya için varyasyon satırı yeniden kullanılır.
    Önceki havuz silinmez: `passed` olarak geri çekilir, sahne üretime dönerse yeniden kullanılabilir."""
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        v = s.scalar(select(Variation).where(Variation.scene_id == scene_id,
                                             Variation.params["external_hash"].astext == sc.external_hash))
        if v is None:
            v = Variation(id=uuid.uuid4(), scene_id=scene_id, tier="full", wave_no=0, engine=sc.engine or DEFAULT_ENGINE,
                          seed=0, prompt_revision=sc.prompt_revision, status="passed", video_key=sc.external_key,
                          params={"source": "external", "external_hash": sc.external_hash}, anomalies=[], cost_usd=0)
            s.add(v)
            s.flush()
        vid, video_key = str(v.id), sc.external_key
    if not storage.exists(f"thumbs/{scene_id}/{vid}.jpg"):
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            make_thumb(storage.download(video_key, os.path.join(tmp, "x.mp4")), f"thumbs/{scene_id}/{vid}.jpg")
        with session_scope() as s:
            s.get(Variation, vid).keyframe_key = f"thumbs/{scene_id}/{vid}.jpg"
    gate.set_single_winner(scene_id, vid, {
        "status": "refined", "visual_hash": fingerprint, "lipsync_video_key": None, "final_video_key": None,
        "last_error": None, "cut_tier": "final"})
    return vid


# ---------- previs rehberi ----------

def run_guide(project_id: str, scene_id: str, visual_hash: str, style) -> str:
    """Rehberli VISUAL aşaması. Ücretsiz modda rehber renklendirilip tek varyasyon olarak kullanılır; ücretli modda
    video-to-video stil aktarımından geçer. Bedel Wan 2.2 video-to-video fiyatıdır (ücretsiz modda sanal).
    Dönen: "refined" ya da "needs_review" (rehber ya da aktarım kusurluysa)."""
    from app.core.hashing import seed_for
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene
    from app.engine.providers import RESOLUTIONS, route_engine
    with session_scope() as s:
        sc, project = s.get(Scene, scene_id), s.get(Project, project_id)
        cfg = project_settings(project.settings)
        guide_key, engine = sc.guide_key, sc.engine or route_engine(sc)
        n = s.scalar(select(func.count()).select_from(Variation).where(Variation.scene_id == scene_id)) or 0
        composed = compose_for_scene(sc, load_characters(s, scene_id), load_assets(s, scene_id), style)
        seed = seed_for(scene_id, visual_hash, "v2v")
    advance_scene(project_id, scene_id, status="generating", last_error=None)
    width, height = RESOLUTIONS.get(cfg["full_resolution"], RESOLUTIONS["720p"])
    vid = uuid.uuid4()
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        src = storage.download(guide_key, os.path.join(tmp, "guide.mp4"))
        duration = probe_duration(src)
        estimate = restyle_estimate(cfg["full_resolution"], duration)
        label = f"v2v:{cfg['full_resolution']}:previs"
        with session_scope() as s:
            s.add(Variation(id=vid, scene_id=scene_id, tier="full", wave_no=n, engine=engine, seed=seed,
                            prompt_revision=0, status="running",
                            params={"source": "previs_guide", "resolution": cfg["full_resolution"],
                                    "duration_s": round(duration, 3), "lighting": style.lighting_prefix if style else "",
                                    "grade": grade_filter(style.lighting_prefix if style else None),
                                    "prompt": composed.prompt, "negative": composed.negative}))
        try:
            if settings.dry_run:
                with killswitch.spend(project_id, estimate, label, scene_id=scene_id, variation_id=vid) as charge:
                    out = grade(src, os.path.join(tmp, "graded.mp4"), style.lighting_prefix if style else None,
                                width, height)
                    charge.actual = estimate
            else:
                out = paid_restyle(project_id, scene_id, vid, src, composed, seed, cfg["full_resolution"], duration,
                                   estimate, label, os.path.join(tmp, "restyled.mp4"), width, height)
        except BaseException:
            with session_scope() as s:
                s.get(Variation, vid).status = "failed"           # "running" satırı asılı kalmasın
            raise
        anomalies = detect_anomalies(out)
        key = f"variations/{scene_id}/{vid}.mp4"
        storage.put_file(out, key)
        thumb = make_thumb(out, f"thumbs/{scene_id}/{vid}.jpg")
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.keyframe_key, v.anomalies, v.cost_usd = thumb, anomalies, estimate
        if anomalies:
            v.status, v.video_key = "purged", None
            v.params = {**v.params, "purge_reason": "quality_gate", "quarantine_key": storage.purge(key)}
        else:
            v.status, v.video_key = "passed", key
    if anomalies:
        found = ", ".join(sorted({a["type"] for a in anomalies}))
        advance_scene(project_id, scene_id, status="needs_review", last_error=f"previs rehberinde kusur var ({found})")
        return "needs_review"
    gate.set_single_winner(scene_id, str(vid), {
        "status": "refined", "visual_hash": visual_hash, "lipsync_video_key": None, "final_video_key": None,
        "last_error": None, "cut_tier": "final", "upgrade_requested": False})
    killswitch.event(project_id, "refined", scene_id=scene_id, pool=1, method="previs_guide")
    return "refined"


def restyle_estimate(resolution: str, duration_s: float) -> float:
    """Wan 2.2 video-to-video bedeli: 16 fps'deki kare sayısı (belgelenen aralık 17-161) üzerinden saniye başına.
    Bilinmeyen çözünürlükte en pahalı fiyat kullanılır."""
    frames = min(V2V_FRAMES[1], max(V2V_FRAMES[0], round(duration_s * V2V_FPS)))
    return pricing.estimate_video("wan_v2v", resolution, frames / V2V_FPS)


def paid_restyle(project_id: str, scene_id: str, vid, src: str, composed, seed: int, resolution: str, duration: float,
                 estimate: float, label: str, out: str, width: int, height: int) -> str:
    """Ücretli previs aktarımı (Faz 18): rehber yüklenir, Wan 2.2 video-to-video bestecinin prompt'uyla stile aktarır;
    sonuç çekim çözünürlüğüne getirilir. Kilit rezervasyondan önce çalışır; geçici hatalar yeniden denenir."""
    from app.engine import paid
    from app.live import fal_video                  # ücretli modül: yalnızca fonksiyon içinde
    made, request_id = paid.call(project_id, estimate, label, "fal_key",
                                 lambda: fal_video.restyle(src, composed.prompt, composed.negative, seed, resolution,
                                                           duration),
                                 scene_id=scene_id, variation_id=vid)
    try:
        _ffmpeg("-i", made, "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", out)
    finally:
        os.remove(made)
    with session_scope() as s:
        s.get(Variation, vid).external_request_id = request_id
    return out


# ---------- elle düzeltme ----------

def first_frame_clip(base_video: str, frame: str, out: str) -> str:
    """İlk kare düzeltmesi (ücretsiz mod): düzenlenmiş kare klibin başında kısa süre durur ve orijinale erir.
    Ücretli modda düzenlenmiş kareden yeniden üretilir (`regenerate_from_frame`)."""
    width, height = video_size(base_video)
    end = FIRST_FRAME_HOLD_S + FIRST_FRAME_FADE_S
    _ffmpeg("-i", base_video, "-loop", "1", "-t", f"{end:.2f}", "-i", frame, "-filter_complex",
            f"[1:v]scale={width}:{height},format=rgba,fade=t=out:st={FIRST_FRAME_HOLD_S}:d={FIRST_FRAME_FADE_S}:alpha=1[f];"
            f"[0:v][f]overlay=eof_action=pass,format=yuv420p[v]", "-map", "[v]", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", out)
    return out


def regenerate_from_frame(variation_id: str, frame: str, out: str) -> str:
    """Ücretli ilk kare düzeltmesi (Faz 18): düzenlenmiş kare varyasyonun motoruna verilir (MiniMax H3'te ilk kare, fal.ai'de
    yüklenmiş görsel) ve varyasyonun prompt, seed, çözünürlük ve süresiyle videoya çevrilir (Flux yeniden çağrılmaz).
    Bedel yalnızca videodur. Kilit rezervasyondan önce çalışır."""
    from app.engine import paid
    from app.engine.providers import GenRequest, get_provider
    with session_scope() as s:
        v = s.get(Variation, variation_id)
        sc = s.get(Scene, v.scene_id)
        project_id, scene_id, engine = str(sc.project_id), str(sc.id), v.engine
        p = v.params or {}
        req = GenRequest(prompt=p.get("prompt", ""), negative=p.get("negative", ""), seed=v.seed,
                         resolution=p.get("resolution", "720p"), duration_s=float(p.get("duration_s") or sc.duration_s),
                         image_url="(düzenlenmiş kare)")
    provider = get_provider(engine)                     # ücretli modda: kilit burada, rezervasyondan önce

    def run():
        made = provider.generate(replace(req, image_url=provider.upload(frame)))
        os.replace(made.local_path, out)
        return made                                     # bedeli (H3: ölçülen GPU süresi) deftere işlensin
    paid.call(project_id, provider.estimate(req), f"{engine}:{req.resolution}:revision", provider.key_name, run,
              scene_id=scene_id, variation_id=variation_id)
    return out


def add_revision(variation_id: str, kind: str, path: str, ext: str, sha: str, note: str | None) -> tuple[dict, bool]:
    """Revizyonu kaydeder ve varyasyonun etkin revizyonu yapar. Aynı dosya yeniden yüklenirse yeni sürüm açılmaz.
    (revizyon, rafine #1 mi). Kusurlu (siyah, donuk) revizyon reddedilir: ValueError (Türkçe)."""
    with session_scope() as s:
        v = s.get(Variation, variation_id)
        base_key, scene_id = v.video_key, str(v.scene_id)
        existing = s.scalar(select(Revision).where(Revision.variation_id == variation_id, Revision.content_hash == sha,
                                                   Revision.kind == kind))
        existing_id = str(existing.id) if existing else None
    if existing_id is None:
        source_key = f"revisions/{variation_id}/{sha}{ext}"
        video_key = f"revisions/{variation_id}/{sha}.mp4" if kind == "video" else f"revisions/{variation_id}/{sha}_ff.mp4"
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            video = path
            if kind == "first_frame" and settings.dry_run:
                base = storage.download(base_key, os.path.join(tmp, "base.mp4"))
                video = first_frame_clip(base, path, os.path.join(tmp, "ff.mp4"))
            elif kind == "first_frame":
                video = regenerate_from_frame(variation_id, path, os.path.join(tmp, "ff.mp4"))
            anomalies = detect_anomalies(video)
            if anomalies:
                found = ", ".join(sorted({a["type"] for a in anomalies}))
                raise ValueError(f"Revizyonda kusur var ({found}); orijinal korunuyor.")
            if not storage.exists(source_key):
                storage.put_file(path, source_key, "video/mp4" if kind == "video" else "image/png")
            if not storage.exists(video_key):
                storage.put_file(video, video_key)
        with session_scope() as s:
            version = (s.scalar(select(func.max(Revision.version)).where(Revision.variation_id == variation_id)) or 0) + 1
            rev = Revision(id=uuid.uuid4(), variation_id=variation_id, scene_id=scene_id, version=version, kind=kind,
                           source_key=source_key, video_key=video_key, content_hash=sha, note=note, anomalies=[])
            s.add(rev)
            s.flush()
            existing_id = str(rev.id)
    return activate_revision(variation_id, existing_id)


def activate_revision(variation_id: str, revision_id: str | None) -> tuple[dict | None, bool]:
    """Etkin revizyonu değiştirir (None: orijinal). Rafine #1 ise dudak senkronu geçersizlenir: plan LIPSYNC + MUX."""
    with session_scope() as s:
        v = s.get(Variation, variation_id)
        changed = str(v.revision_id or "") != str(revision_id or "")
        v.revision_id = revision_id
        is_winner = v.refined_rank == 1
        if changed and is_winner:
            s.get(Scene, v.scene_id).lipsync_video_key = None
        rev = s.get(Revision, revision_id) if revision_id else None
        out = revision_out(rev) if rev else None
    return out, is_winner


def revision_out(r: Revision) -> dict:
    return {"id": str(r.id), "version": r.version, "kind": r.kind, "note": r.note,
            "video_url": f"/api/media?key={r.video_key}", "source_url": f"/api/media?key={r.source_key}",
            "created_at": r.created_at.timestamp() if r.created_at else None}
