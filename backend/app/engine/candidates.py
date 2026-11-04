"""Tek adaylı üretim ve yönetmen onaylı yeni adaylar.

Varsayılan (`candidate_mode="single"`): her çekim için TEK aday üretilir. Kalite kapısından kalırsa ya da skoru eşiğin
altındaysa hat kendiliğinden yeni aday üretmez; panel yönetmene "yeni aday üreteyim mi?" diye sorar ve tahmini ek
maliyeti (gerçek sağlayıcı fiyatlarıyla; ücretsiz modda sanal) gösterir. Yönetmen onaylarsa `n` yeni aday üretilir
(her biri yeni tohumla, ayrı bütçe rezervasyonuyla) ve en yüksek skorlu aday kurguya girer. Önceki kazanan silinmez:
`passed` olarak kalır, "Yine de kullan" ile geri seçilebilir.

`candidate_mode="multi"`: eski davranış (probe_n test turu + full_n tam tur dalgaları, fal_clip_attempts yeniden deneme).
"""
import logging
import uuid

from sqlalchemy import func, select

from app.budget import killswitch
from app.config import project_settings, settings
from app.core.state import advance_scene
from app.db import session_scope
from app.models import CurationEvent, Project, Scene, Variation

log = logging.getLogger("hellgrind.candidates")

MAX_MORE = 5                     # tek istekte en çok yeni aday
DECLINE = "more_candidates_decline"
WORKING = ("pending", "storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing")


def single(cfg: dict) -> bool:
    return cfg.get("candidate_mode", "single") != "multi"


def attempts(cfg: dict) -> int:
    """fal.ai ve referans modu: bir üretimde en çok kaç klip. Tek aday modunda 1 (kalırsa yönetmene sorulur)."""
    return 1 if single(cfg) else max(1, int(cfg.get("fal_clip_attempts", 2)))


def estimate_cfg(cfg: dict) -> dict:
    """Maliyet tahminleri için etkin ayarlar: tek aday modunda çekim başına bir tam klip (test turu ve düzeltme turu
    yok, yeniden deneme yok)."""
    if not single(cfg):
        return cfg
    return {**cfg, "probe_n": 0, "full_n": 1, "wave_size": 1, "max_correction_rounds": 0, "fal_clip_attempts": 1,
            "refined_k": 1}


# ---------- soru: yeni aday üreteyim mi? ----------

def declined_q(project_id):
    """Projedeki çekimlerin son "hayır, böyle kalsın" kararı: {sahne: zaman} (kürasyon kaydından; durum tutulmaz)."""
    return (select(CurationEvent.scene_id, func.max(CurationEvent.created_at))
            .where(CurationEvent.project_id == project_id, CurationEvent.action == DECLINE)
            .group_by(CurationEvent.scene_id))


def offer(sc, variations: list, cfg: dict, declined_at=None) -> dict | None:
    """Panelin "Yeni aday" bölümü. ask=True: skor düşük, yönetmene sorulur. None: soru yok ve bölüm gösterilmez
    (çekim işleniyor, yöntemi üretim değil ya da henüz aday yok)."""
    if (sc.method or "generate") != "generate" or sc.status in WORKING:
        return None
    current = [v for v in variations if not (v.params or {}).get("stale_keyframe")
               and v.status in ("passed", "refined", "purged") and v.composite_score is not None]
    if not current:
        return None
    threshold = float(cfg["clip_threshold"])
    winner = next((v for v in variations if v.refined_rank == 1), None)
    if winner is not None:
        score = winner.composite_score
        low = (score is not None and score < threshold
               and not (winner.params or {}).get("director_override"))       # yönetmen bilerek seçti: sorulmaz
        reason = "low" if low else None
    else:
        score = max(v.composite_score for v in current)
        low, reason = True, "gate"
    latest = max((v.created_at for v in variations if v.created_at is not None), default=None)
    if low and declined_at is not None and latest is not None and declined_at >= latest:
        low = False                                   # yönetmen bu adaylar için "hayır" dedi
    if low:
        msg = (f"Skor düşük ({score:.2f}, eşik {threshold:.2f})" if score is not None else "Aday kalite kapısından kalmadı")
        msg += (": aday kalite kapısından geçemedi." if reason == "gate" else ".") + " Yeni aday üreteyim mi?"
    else:
        msg = None
    return {"ask": low, "reason": reason, "score": score, "threshold": threshold, "message": msg,
            "candidates": len(current), "max": MAX_MORE}


def _route(sc) -> str:
    from app.engine import fal_plan, scene_parts
    if scene_parts.active(sc.engine):
        return "part"
    return "fal" if fal_plan.is_fal(sc.engine) else "tiered"


def estimate(s, sc, n: int) -> dict:
    """n yeni adayın tahmini ek maliyeti (gerçek fiyatlar; ücretsiz modda sanal) ve kalan bütçe. Hiçbir şey yazmaz."""
    from app.budget import pricing
    from app.engine import fal_plan, scene_parts
    from app.style import bible
    p = s.get(Project, sc.project_id)
    cfg = project_settings(p.settings)
    route = _route(sc)
    shots = [sc.scene_code]
    if route == "part":                               # parça tek istekte: kardeş çekimler de birlikte yeniden üretilir
        plan = scene_parts.plan_part(s, sc)
        per = float(plan.cost["reserve_usd"])
        shots = [x.scene_code for x in plan.shots]
        resolution = plan.resolution
    elif route == "fal":
        per = bible.visual_cost(estimate_cfg(cfg), sc.engine, float(sc.duration_s or 4.0))[0]
        resolution = fal_plan.resolution(sc.engine, None, cfg)
    else:
        resolution = cfg["full_resolution"]
        per = pricing.estimate_video(sc.engine or "h3", resolution, float(sc.duration_s or 4.0))
    total = round(per * n, 4)
    remaining = round(float(p.budget_limit_usd) - float(p.spent_usd) - float(p.reserved_usd), 4)
    return {"n": n, "per_candidate_usd": round(per, 4), "total_usd": total, "remaining_usd": remaining,
            "over_budget": total > remaining, "engine": sc.engine,
            "engine_label": fal_plan.ENGINE_TR.get(sc.engine or "", sc.engine), "resolution": resolution,
            "shots": shots, "virtual": settings.dry_run}


# ---------- üretim ----------

def run_more(project_id: str, scene_id: str, n: int) -> str:
    """Yönetmenin onayladığı n yeni aday. Dönen: refined | needs_review | blocked | plan_review | failed."""
    from app.engine import fal_plan, iteration
    from app.engine.providers import H3_DISABLED, ProviderError, h3_disabled
    from app.quality import gate
    n = max(1, min(MAX_MORE, int(n)))
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        route = _route(sc)
        engine = sc.engine
    gate.ensure_scorer()
    if route == "part":
        from app.engine.part_render import render_more
        return render_more(project_id, scene_id, n)
    if not settings.dry_run and h3_disabled(engine):
        advance_scene(project_id, scene_id, status="needs_review", last_error=H3_DISABLED)
        return "needs_review"
    ctx = iteration._prepare(project_id, scene_id, uuid.uuid4().hex)
    if fal_plan.is_fal(ctx.provider.engine):
        tier, resolution = fal_plan.tier_of(ctx.provider.engine, ctx.cfg), ctx.render["fal_resolution"]
    else:
        tier, resolution = "full", ctx.cfg["full_resolution"]
    with session_scope() as s:                        # yeni dalga: yeni tohumlar (önceki adaylar yeniden kullanılmaz)
        wave = (s.scalar(select(func.max(Variation.wave_no)).where(Variation.scene_id == scene_id,
                                                                   Variation.tier == tier)) or 0) + 1
    advance_scene(project_id, scene_id, status="generating")
    try:
        results = iteration.run_wave(ctx, tier, wave, n, resolution)
    except ProviderError as e:
        advance_scene(project_id, scene_id, status="failed", last_error=str(e))
        killswitch.event(project_id, "provider_rejected", scene_id=scene_id, error=str(e)[:300])
        return "failed"
    killswitch.event(project_id, "more_candidates", scene_id=scene_id, n=n,
                     passed=sum(r.passed for r in results), engine=ctx.provider.engine)
    return _adopt_best(ctx, [r.id for r in results if r.passed], tier, n)


def _adopt_best(ctx, new_ids: list[str], tier: str, n: int) -> str:
    """Yeni geçenler ve mevcut kazanan arasından en yüksek skorlu kurguya girer."""
    from app.engine.iteration import invalidate_next_seam
    from app.quality import gate
    with session_scope() as s:
        pool = [v for i in new_ids if (v := s.get(Variation, i)) is not None]
        current = s.scalar(select(Variation).where(Variation.scene_id == ctx.scene_id, Variation.refined_rank == 1))
        if current is not None and not (current.params or {}).get("stale_keyframe"):
            pool.append(current)
        if not pool:
            best_id, changed = None, False
        else:
            best = max(pool, key=lambda v: (v.composite_score or 0.0, -v.seed))
            best_id, changed = str(best.id), current is None or best.id != current.id
    if best_id is None:
        advance_scene(ctx.project_id, ctx.scene_id, status="needs_review",
                      last_error=f"{n} yeni aday da kalite kapısından geçmedi. Adaylardan birini \"Yine de kullan\" ile "
                                 "seçebilir ya da yeniden yeni aday isteyebilirsiniz.")
        return "needs_review"
    if changed:
        gate.set_single_winner(ctx.scene_id, best_id, {
            "status": "refined", "visual_hash": ctx.visual_hash, "lipsync_video_key": None, "final_video_key": None,
            "last_error": None, "cut_tier": "final" if tier == "full" else "draft", "upgrade_requested": False})
        invalidate_next_seam(ctx.project_id, ctx.scene_id)
    else:                                             # mevcut kazanan hâlâ en iyisi: kurgu değişmez
        advance_scene(ctx.project_id, ctx.scene_id, status="refined", last_error=None)
    killswitch.event(ctx.project_id, "refined", scene_id=ctx.scene_id, pool=1, more=True, changed=changed)
    return "refined"
