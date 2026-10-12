"""Bitirme: Seedance 2.5 taslağını 1080p'ye tamamlama ve upscale (onaylanmış klipleri 1080p'ye çıkaran ayrı adım).

Ağ yok, ücretli modül yalnızca fonksiyon içinde yüklenir (app/live/fal_finish.py). Ücretsiz modda ikisi de klibi yerelde
FFmpeg ile 1920x1080'e ölçekler ve ücretli karşılığının fiyatıyla sanal olarak bütçeden düşer.

- **Taslak tamamlama** (draft/complete): Seedance 2.5 taslağı (draft: true, 480p) 7 gün içinde YALNIZCA 1080p'ye
  tamamlanır (0,0234 $ / 1000 token ≈ 1,164 $/sn; tahmine %20 pay). Parçanın klibi tamamlanır ve yeniden çekimlere
  kesilir; taslak kesimleri varyasyon olarak kalır. Daha ucuz seçenek: upscale (bytedance-upscaler ≈ 0,0072 $/sn).
- **Upscale:** klip, çekim ya da (son montajdan önce) bütün film. Upscale edilmiş klip ayrı varyasyondur, orijinal korunur.
  Hedef fps kaynakla aynıdır (24; gereksiz 60 fps yok). Upscale edilen çekimin otomatik renk eşlemesi sıfırlanır: eşleme,
  upscale'den SONRA (montaj ve film birleştirmeden sonraki süreklilik denetiminde) yeniden yapılır. Bütün film upscale'i
  bitince (son çekimin montajından sonra) film yeniden birleştirilir; süreklilik ve renk eşleme onu izler.
"""
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid

from sqlalchemy import select

from app import storage
from app.budget import killswitch, pricing
from app.config import project_settings, settings
from app.db import session_scope
from app.models import Project, Scene, Variation

log = logging.getLogger("hellgrind.finishing")

DRAFT_TTL_S = 7 * 24 * 3600
UPSCALE_TARGET_H = 1080
UPSCALE_SIZE = (1920, 1080)
UPSCALE_ENDPOINTS = ("fal-ai/bytedance-upscaler/upscale/video", "fal-ai/kandinsky6-vsr", "fal-ai/seedvr/upscale/video",
                     "fal-ai/flashvsr/upscale/video", "blackforestlabs/flux-video-upscale")
PENDING = "hg:upscale_pending:{}"         # bütün film upscale'i: montajı bekleyen çekimler


class FinishBlocked(Exception):
    """İşlem yapılamaz (süre dolmuş, bütçe yetmiyor, tahmin değişti...): Türkçe gerekçe, HTTP 409."""


# ---------- ortak ----------

def probe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "json", path],
                         capture_output=True, text=True).stdout
    data = json.loads(out or "{}")
    st = (data.get("streams") or [{}])[0]
    num, _, den = str(st.get("r_frame_rate") or "24/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 24.0
    return {"width": int(st.get("width") or 0), "height": int(st.get("height") or 0), "fps": round(fps, 3),
            "duration_s": float((data.get("format") or {}).get("duration") or 0.0)}


def remaining_budget(s, project_id) -> float:
    p = s.get(Project, project_id)
    return round(float(p.budget_limit_usd) - float(p.spent_usd) - float(p.reserved_usd), 4)


def scale_local(src: str, dst: str, fps: float | None = None) -> str:
    """Ücretsiz modun karşılığı: 1920x1080'e ölçekleme (kaynak fps korunur)."""
    vf = f"scale={UPSCALE_SIZE[0]}:{UPSCALE_SIZE[1]}:force_original_aspect_ratio=decrease," \
         f"pad={UPSCALE_SIZE[0]}:{UPSCALE_SIZE[1]}:(ow-iw)/2:(oh-ih)/2"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart"]
    subprocess.run(cmd + [dst], check=True, capture_output=True)
    return dst


def cut(src: str, start: float, end: float, dst: str) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-c:a", "aac", "-movflags",
                    "+faststart", dst], check=True, capture_output=True)
    return dst


def _dispatch_post(project_id: str, scene_ids: list[str]) -> None:
    """Klip değişti: ses, renk ve montaj yeniden (VISUAL değil)."""
    from app.api.plan import plan_and_dispatch
    for sid in scene_ids:
        try:
            with session_scope() as s:
                plan_and_dispatch(s, s.get(Scene, sid), post_only=True)
        except Exception as e:                         # noqa: BLE001 — elle de başlatılabilir
            log.warning("ses/montaj kuyruğa verilemedi (%s): %s", sid, e)


def _reset_auto_grade(s, sc) -> None:
    """Klip değişti (upscale, tamamlama, yükseltme): eski klipte yapılmış otomatik renk eşlemesi geri alınır ve çekim
    "eşleme bekliyor" olarak işaretlenir; montajdan sonra (projede süren iş kalmayınca) otomatik eşleme yeniden çalışır
    (tasks.maybe_rematch). Yönetmenin elle yaptığı renk ayarına dokunulmaz."""
    from app.quality.consistency import _manual
    auto = sc.grade_auto or {}
    if auto.get("applied"):
        sc.grade = auto.get("previous") or {}
        sc.grade_auto = {"pending": True}
    elif not _manual(sc):
        sc.grade_auto = {**auto, "pending": True}


# ---------- Seedance taslağı ----------

def remember_draft(scene_ids: list[str], codes: list[str], winners: dict, part_hash: str, duration_s: float) -> None:
    now = time.time()
    with session_scope() as s:
        first = s.get(Variation, winners.get(scene_ids[0])) if winners.get(scene_ids[0]) else None
        params = (first.params or {}) if first is not None else {}
        draft_id = params.get("draft_id") or (params.get("provider_meta") or {}).get("draft_id")
        if not draft_id:
            return
        info = {"draft_id": draft_id, "created_at": now, "expires_at": now + DRAFT_TTL_S, "part": codes,
                "part_hash": part_hash, "variations": {k: str(v) for k, v in winners.items()},
                "duration_s": float(duration_s), "local": draft_id.startswith("local-")}
        for sid in scene_ids:
            sc = s.get(Scene, sid)
            if sc is not None:
                sc.seedance_draft = info


def draft_view(info: dict | None) -> dict | None:
    if not info:
        return None
    left = float(info["expires_at"]) - time.time()
    return {**info, "remaining_s": max(0, round(left)), "expired": left <= 0}


def complete_preview(s, sc) -> dict:
    """Taslak tamamlamanın tahmini ve uygunluğu; yanında daha ucuz seçenek (upscale)."""
    info = draft_view(sc.seedance_draft)
    if not info:
        return {"available": False, "reason": "Bu çekimin Seedance 2.5 taslağı yok (draft kapalıyken üretilmiş)."}
    seconds = float(info.get("duration_s") or sc.duration_s or 5.0)
    cost = pricing.seedance25_complete_cost(seconds)
    remaining = remaining_budget(s, sc.project_id)
    alt = pricing.upscale_cost(settings.upscale_endpoint, seconds, 24.0, settings.upscale_target, settings.upscale_tier)
    out = {"available": True, "draft": info, "part": info.get("part"), "seconds": seconds, "cost": cost,
           "label": f"≈ {cost['usd']:.1f} $".replace(".", ","), "remaining_usd": remaining,
           "budget_ok": cost["reserve_usd"] <= remaining,
           "cheaper": {"kind": "upscale", "endpoint": settings.upscale_endpoint, "cost": alt,
                       "label": f"Upscale ile 1080p (≈ {alt['usd']:.2f} $)".replace(".", ",")},
           "reason": None}
    if info.get("completed_at"):
        out.update(available=False, reason="Taslak zaten 1080p'ye tamamlandı.")
    elif info["expired"]:
        out.update(available=False, reason="Taslağın 7 günlük süresi doldu: draft/complete artık kabul edilmez.")
    elif not out["budget_ok"]:
        out.update(available=False, reason=f"Bütçe yetmiyor: tahmini {cost['reserve_usd']:.2f} $ (%20 paylı), kalan "
                                            f"{remaining:.2f} $.".replace(".", ","))
    return out


def run_complete(project_id: str, scene_id: str) -> str:
    """Görev gövdesi: taslağı tamamlar (ücretli: fal draft/complete; ücretsiz: yerel 1080p), çekimlere keser, yeni
    varyasyonları kazanan yapar ve ses/montajı yeniden kuyruğa verir."""
    from app.quality import gate
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        preview = complete_preview(s, sc)
        if not preview["available"]:
            raise FinishBlocked(preview["reason"])
        info = preview["draft"]
        codes = info.get("part") or [sc.scene_code]
        shots = {x.scene_code: x for x in s.scalars(select(Scene).where(Scene.project_id == sc.project_id,
                                                                       Scene.scene_code.in_(codes)))}
        sources = {}
        for code in codes:
            x = shots[code]
            v = s.get(Variation, info["variations"].get(str(x.id))) if info["variations"].get(str(x.id)) else None
            if v is None:
                raise FinishBlocked(f"{code}: taslak klibi bulunamadı.")
            sources[str(x.id)] = (code, v.id, v.tier, v.engine, dict(v.params or {}), v.video_key)
        leader = str(shots[codes[0]].id)
    first = next(iter(sources.values()))
    part_clip = first[4].get("part_clip") or first[5]
    estimate = preview["cost"]["reserve_usd"]
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        with killswitch.spend(project_id, estimate, "seedance25:1080p:complete", scene_id=leader,
                              details={"draft_id": info["draft_id"], "part": codes}) as charge:
            if settings.dry_run or info.get("local"):
                full = scale_local(storage.download(part_clip, os.path.join(tmp, "draft.mp4")),
                                   os.path.join(tmp, "complete.mp4"))
                request_id, cost = f"local-complete-{uuid.uuid4()}", preview["cost"]["usd"]
            else:
                from app.live import fal_finish             # ücretli modül: yalnızca fonksiyon içinde
                made = fal_finish.complete_draft(info["draft_id"], scene_id=leader, project_id=project_id)
                full, request_id, cost = made.local_path, made.request_id, made.cost_usd
            charge.actual = cost
        clip_key = f"complete/{info['part_hash']}.mp4"
        storage.put_file(full, clip_key)
        winners = {}
        for sid, (code, vid, tier, engine, params, _) in sources.items():
            start, end = params.get("segment") or [0.0, probe(full)["duration_s"]]
            new = uuid.uuid4()
            seg = cut(full, start, end, os.path.join(tmp, f"{code}.mp4"))
            key = f"variations/{sid}/{new}.mp4"
            storage.put_file(seg, key)
            from app.engine.iteration import make_thumb
            thumb = make_thumb(seg, f"thumbs/{sid}/{new}.jpg")
            anomalies = gate.detect_anomalies(seg)
            with session_scope() as s:
                s.add(Variation(id=new, scene_id=sid, tier="full", wave_no=0, engine=engine, seed=0, prompt_revision=0,
                                status="passed", video_key=key, keyframe_key=thumb,
                                external_request_id=request_id if sid == leader else None,
                                cost_usd=round(cost * (end - start) / max(1e-6, preview["seconds"]), 4),
                                anomalies=anomalies,
                                params={**{k: params[k] for k in ("part", "part_codes", "segment", "references", "prompt")
                                           if k in params},
                                        "completed_from": str(vid), "draft_id": info["draft_id"], "resolution": "1080p",
                                        "part_clip": clip_key, "request_id": request_id}))
            winners[sid] = str(new)
    for sid, vid in winners.items():
        gate.set_single_winner(sid, vid, {"status": "refined", "lipsync_video_key": None, "final_video_key": None,
                                          "graded_video_key": None, "last_error": None, "cut_tier": "final"})
    with session_scope() as s:
        for sid in winners:
            x = s.get(Scene, sid)
            _reset_auto_grade(s, x)
            x.seedance_draft = {**(x.seedance_draft or {}), "completed_at": time.time()}
    killswitch.event(project_id, "draft_completed", scene_id=leader, part=codes, usd=round(cost, 4))
    _dispatch_post(project_id, list(winners))
    return "completed"


# ---------- upscale ----------

def upscale_body(endpoint: str, video_url: str, height: int, fps: float, tier: str | None = None) -> dict:
    """Uç noktanın gövdesi (şemalara göre; hedef 1080p, kaynak fps). height: kaynağın yüksekliği."""
    factor = UPSCALE_TARGET_H / max(1, height)
    if endpoint == "fal-ai/bytedance-upscaler/upscale/video":
        return {"video_url": video_url, "target_resolution": "1080p", "target_fps": max(24, round(fps)),
                "enhancement_tier": tier or settings.upscale_tier, "fidelity": "high"}
    if endpoint == "fal-ai/kandinsky6-vsr":
        return {"video_url": video_url, "upscale_factor": min((2, 2.25, 4), key=lambda f: (f < factor - 1e-6, abs(f - factor)))}
    if endpoint == "fal-ai/seedvr/upscale/video":
        return {"video_url": video_url, "upscale_mode": "target", "target_resolution": "1080p"}
    if endpoint == "fal-ai/flashvsr/upscale/video":
        return {"video_url": video_url, "upscale_factor": round(min(4.0, max(1.0, factor)), 3), "preserve_audio": True}
    if endpoint == "blackforestlabs/flux-video-upscale":
        return {"video_url": video_url, "creativity": 0, "upscale_factor": round(min(3.0, max(1.5, factor)), 3)}
    raise ValueError(f"Bilinmeyen upscale uç noktası: {endpoint}")


def upscale_items(s, scope: str, ref_id: str) -> list[Variation]:
    """Upscale edilecek klipler: variation (tek klip), scene (çekimin kazananı), project (1080p altındaki bütün
    kazananlar; son montajdan önce)."""
    if scope == "variation":
        v = s.get(Variation, ref_id)
        return [v] if v is not None and v.video_key else []
    q = select(Variation).join(Scene, Scene.id == Variation.scene_id).where(Variation.refined_rank == 1,
                                                                            Variation.video_key.is_not(None))
    q = q.where(Variation.scene_id == ref_id) if scope == "scene" else q.where(Scene.project_id == ref_id)
    return [v for v in s.scalars(q.order_by(Scene.seq_order)) if (v.params or {}).get("resolution") != "1080p"]


def upscale_plan(s, scope: str, ref_id: str, endpoint: str | None = None) -> dict:
    """Plan ve onay ekranı: uç nokta, örnek gövde, klip başına ve toplam tahmin, diğer seçeneklerin toplamı, bütçe."""
    endpoint = endpoint or settings.upscale_endpoint
    items, total, alts, warnings = [], 0.0, {e: 0.0 for e in UPSCALE_ENDPOINTS}, []
    project_id = None
    for v in upscale_items(s, scope, ref_id):
        sc = s.get(Scene, v.scene_id)
        project_id = sc.project_id
        seconds = float((v.params or {}).get("duration_s") or sc.duration_s or 5.0)
        fps = 24.0
        cost = pricing.upscale_cost(endpoint, seconds, fps, settings.upscale_target, settings.upscale_tier)
        for e in UPSCALE_ENDPOINTS:
            alts[e] += pricing.upscale_cost(e, seconds, fps, settings.upscale_target, settings.upscale_tier)["usd"]
        if cost.get("warning"):
            warnings.append(f"{sc.scene_code}: {cost['warning']}")
        items.append({"variation_id": str(v.id), "scene_id": str(sc.id), "scene_code": sc.scene_code,
                      "seconds": seconds, "fps": fps, "from": (v.params or {}).get("resolution"), "cost": cost})
        total += cost["reserve_usd"]
    remaining = remaining_budget(s, project_id) if project_id else None
    body = upscale_body(endpoint, "local:<klip>", 480, 24.0) if items else None
    return {"scope": scope, "endpoint": endpoint, "target": settings.upscale_target, "fps": "kaynakla aynı (24)",
            "items": items, "total_usd": round(total, 4), "alternatives": {e: round(u, 4) for e, u in alts.items()},
            "body": body, "remaining_usd": remaining, "budget_ok": remaining is None or total <= remaining,
            "warnings": warnings}


def run_upscale(project_id: str, variation_id: str, endpoint: str | None = None, batch: bool = False) -> str:
    """Görev gövdesi: klibi 1080p'ye çıkarır, ayrı varyasyon olarak saklar (orijinal korunur); kazanan klipse yeni
    klip kazanan olur, otomatik renk eşlemesi sıfırlanır ve ses/renk/montaj yeniden kuyruğa verilir."""
    from app.quality import gate
    endpoint = endpoint or settings.upscale_endpoint
    with session_scope() as s:
        v = s.get(Variation, variation_id)
        sc = s.get(Scene, v.scene_id)
        sid, code, rank, params = str(sc.id), sc.scene_code, v.refined_rank, dict(v.params or {})
        tier, engine, key = v.tier, v.engine, v.video_key
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        src = storage.download(key, os.path.join(tmp, "src.mp4"))
        info = probe(src)
        cost = pricing.upscale_cost(endpoint, info["duration_s"] or float(params.get("duration_s") or 5.0),
                                    info["fps"] or 24.0, settings.upscale_target, settings.upscale_tier)
        with killswitch.spend(project_id, cost["reserve_usd"], "upscale:1080p", scene_id=sid, variation_id=variation_id,
                              details={"endpoint": endpoint}) as charge:
            if settings.dry_run:
                out = scale_local(src, os.path.join(tmp, "up.mp4"), info["fps"])
                request_id, actual = f"local-upscale-{uuid.uuid4()}", cost["usd"]
            else:
                from app.live import fal_finish             # ücretli modül: yalnızca fonksiyon içinde
                body = upscale_body(endpoint, "", info["height"], info["fps"])
                made = fal_finish.upscale(endpoint, src, body, scene_id=sid, project_id=project_id, estimate=cost["usd"])
                out, request_id, actual = made.local_path, made.request_id, made.cost_usd
            charge.actual = actual
        new = uuid.uuid4()
        new_key = f"variations/{sid}/{new}.mp4"
        storage.put_file(out, new_key)
        from app.engine.iteration import make_thumb
        thumb = make_thumb(out, f"thumbs/{sid}/{new}.jpg")
        anomalies = gate.detect_anomalies(out)
        measured = probe(out)
    with session_scope() as s:
        s.add(Variation(id=new, scene_id=sid, tier=tier, wave_no=0, engine=engine, seed=0, prompt_revision=0,
                        status="passed", video_key=new_key, keyframe_key=thumb, external_request_id=request_id,
                        cost_usd=actual, anomalies=anomalies,
                        params={**{k: params[k] for k in ("part", "part_codes", "segment", "prompt", "duration_s")
                                   if k in params},
                                "upscaled_from": str(variation_id), "upscale_endpoint": endpoint, "resolution": "1080p",
                                "fps": measured["fps"], "size": [measured["width"], measured["height"]],
                                "request_id": request_id}))
    if rank == 1:                                        # kazanan klip: yeni klip kazanır, orijinal varyasyon olarak kalır
        gate.set_single_winner(sid, str(new), {"status": "refined", "lipsync_video_key": None, "final_video_key": None,
                                               "graded_video_key": None, "last_error": None})
        with session_scope() as s:
            _reset_auto_grade(s, s.get(Scene, sid))
    killswitch.event(project_id, "upscaled", scene_id=sid, code=code, endpoint=endpoint, usd=round(actual, 4))
    if rank == 1:
        _dispatch_post(project_id, [sid])
    elif batch:
        upscale_done(project_id, sid)
    return "upscaled"


def start_batch(project_id: str, scene_ids: list[str]) -> None:
    """Bütün film upscale'i: çekimlerin montajı bitince film yeniden birleştirilir (renk eşleme ondan sonra)."""
    r = killswitch.r()
    key = PENDING.format(project_id)
    r.delete(key)
    if scene_ids:
        r.sadd(key, *scene_ids)
        r.expire(key, 24 * 3600)


def upscale_done(project_id: str, scene_id: str) -> bool:
    """Çekimin montajı bitti (mux_scene): bekleyen bütün film upscale'inin son çekimiyse film birleştirilir. True: tetiklendi."""
    r = killswitch.r()
    key = PENDING.format(project_id)
    if not r.srem(key, scene_id):
        return False
    if r.scard(key) == 0:
        r.delete(key)
        killswitch.event(project_id, "upscale_batch_done")
        from app.worker.tasks import auto_assemble, maybe_rematch
        if not maybe_rematch(project_id):              # eşleme turu varsa film tur bitince bir kez birleşir
            auto_assemble(project_id, "final", "upscale")
        return True
    return False


def film_size(paths: list[str]) -> tuple[int, int]:
    """Filmin çözünürlüğü: bütün klipler 1080p ise 1920x1080, değilse 1280x720 (upscale edilmemiş klip küçülmez,
    upscale edilmiş klip boşuna küçülmez)."""
    heights = [probe(p)["height"] for p in paths]
    return (1920, 1080) if heights and min(heights) >= UPSCALE_TARGET_H else (1280, 720)


def project_cfg(s, project_id) -> dict:
    return project_settings(s.get(Project, project_id).settings)
