"""Kuru çalıştırma (fal.ai'ye gitmeden istek planı) ve "Seedance 2.5 ile yükselt".

`POST /scenes/{id}/render?dry_run=true` ve `GET /projects/{id}/render-plan?dry_run=true`: hiçbir şey kuyruğa verilmez,
hiçbir dosya yüklenmez, hiçbir ücretli modül yüklenmez. Dönen gövde, ücretli modda gönderilecek gövdenin kendisidir
(dosya adresleri yerine "local:<anahtar>" ve SHA-256).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, SceneId, ensure_not_frozen, project_or_404, scene_or_404
from app.config import project_settings, settings
from app.core.rerender import plan_rerender, stage_names
from app.core.state import load_assets, load_characters
from app.curation import curated
from app.db import get_session
from app.engine import fal_plan, scene_parts
from app.engine.providers import H3_DISABLED, h3_disabled
from app.models import AudioTrack, Project, Scene, Variation
from app.style import bible

router = APIRouter(tags=["render-plan"])


def _stages(s: Session, sc: Scene, force_visual: bool) -> list[str]:
    tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)))
    project = s.get(Project, sc.project_id)
    stages, _ = plan_rerender(sc, load_characters(s, sc.id), tracks, load_assets(s, sc.id),
                              bible.active_version(s, sc.project_id), project.lut_hash)
    names = stage_names(stages)
    if force_visual and "VISUAL" not in names:
        names = ["VISUAL", *[n for n in ("LIPSYNC", "GRADE", "MUX") if n not in names]]
    return names


def upgrade_state(sc: Scene, cfg: dict) -> dict:
    """"Seedance 2.5 ile yükselt" uygun mu: taslak motorla üretilmiş (cut_tier draft) ve kazanan klibi olan çekim."""
    final = fal_plan.final_engine(cfg)
    if sc.engine == final:
        return {"available": False, "reason": f"Çekim zaten final motorunda ({fal_plan.ENGINE_TR.get(final, final)})."}
    if sc.cut_tier != "draft":
        return {"available": False, "reason": "Önce taslak motoruyla üretilmeli (kaba kurgu klibi yok)."}
    return {"available": True, "engine": final, "reason": None}


def remaining_budget(s: Session, project_id) -> float:
    p = s.get(Project, project_id)
    return round(float(p.budget_limit_usd) - float(p.spent_usd) - float(p.reserved_usd), 4)


def upgrade_targets(s: Session, sc: Scene, cfg: dict) -> list[Scene]:
    """Yükseltilecek çekimler: referans modunda bütün sahne (aynı başlığın taslak motorlu çekimleri), değilse çekim."""
    final = fal_plan.final_engine(cfg)
    if not scene_parts.active(sc.engine):
        return [sc]
    return [x for x in scene_parts.scene_shots(s, sc)
            if fal_plan.is_fal(x.engine) and x.engine != final and (x.method or "generate") == "generate"]


def upgrade_preview(s: Session, sc: Scene, cfg: dict) -> dict:
    """Yükseltmenin tahmini (yazılmaz): final motoruyla sahnenin parçaları ve toplamı, kalan bütçe. Motorlar bir kayıt
    noktasında değiştirilip plan hesaplanır, sonra geri alınır."""
    final = fal_plan.final_engine(cfg)
    targets = upgrade_targets(s, sc, cfg)
    up = upgrade_state(sc, cfg)
    if not up["available"]:
        return {**up, "engine": final, "engine_label": fal_plan.ENGINE_TR.get(final, final)}
    undrafted = [x.scene_code for x in targets if x.cut_tier != "draft"]
    if undrafted:
        return {"available": False, "engine": final, "engine_label": fal_plan.ENGINE_TR[final],
                "reason": f"Sahnenin şu çekimleri henüz taslak değil: {', '.join(undrafted)}."}
    ids = [x.id for x in targets]
    nested = s.begin_nested()
    try:
        for x in targets:
            x.engine = final
        s.flush()
        parts, seen = [], set()
        for x in s.scalars(select(Scene).where(Scene.id.in_(ids)).order_by(Scene.seq_order)):
            if scene_parts.active(final):
                plan = scene_parts.plan_part(s, x)
                if plan.part_hash in seen:
                    continue
                seen.add(plan.part_hash)
                parts.append({"part": [y.scene_code for y in plan.shots], "duration_s": plan.duration_s,
                              "usd": plan.cost["reserve_usd"], "endpoint": plan.endpoint, "plan_hash": plan.plan_hash})
            else:
                plan = fal_plan.scene_plan(s, x, final, with_sha=False)
                parts.append({"part": [x.scene_code], "duration_s": float(x.duration_s or 0),
                              "usd": plan.cost["reserve_usd"], "endpoint": plan.endpoint})
    finally:
        nested.rollback()
    total = round(sum(p["usd"] for p in parts), 4)
    remaining = remaining_budget(s, sc.project_id)
    return {"available": True, "engine": final, "engine_label": fal_plan.ENGINE_TR[final],
            "scope": "scene" if len(targets) > 1 or scene_parts.active(sc.engine) else "shot",
            "shots": [x.scene_code for x in targets], "parts": parts, "total_usd": total,
            "cost": {"reserve_usd": total}, "remaining_usd": remaining, "budget_ok": total <= remaining,
            "reason": None if total <= remaining else
            f"Bütçe yetmiyor: tahmini {total:.2f} $, kalan {remaining:.2f} $.".replace(".", ",")}


def scene_dry_run(s: Session, sc: Scene, force_visual: bool = False, with_sha: bool = True) -> dict:
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    engine = sc.engine or fal_plan.draft_engine(cfg)
    out = {"dry_run": True, "scene_id": str(sc.id), "scene_code": sc.scene_code, "engine": engine,
           "engine_label": fal_plan.ENGINE_TR.get(engine, engine), "stages": _stages(s, sc, force_visual),
           "paid_mode": not settings.dry_run, "network": False, "input_mode": settings.video_input_mode,
           "render_mode": settings.scene_render_mode}
    if (sc.method or "generate") != "generate":
        return {**out, "note": f"Yöntem {sc.method}: fal.ai video çağrısı yok."}
    if h3_disabled(engine) and not settings.dry_run:
        return {**out, "disabled": True, "message": H3_DISABLED, "suggest": "h3max"}
    if not fal_plan.is_fal(engine):
        note = (H3_DISABLED if h3_disabled(engine) else
                f"{fal_plan.ENGINE_TR.get(engine, engine)}: kuru çalıştırma gövdesi yalnızca H3 Max ve Seedance 2.5 "
                "için verilir.")
        return {**out, **({"disabled": True, "suggest": "h3max"} if h3_disabled(engine) else {}), "note": note}
    if scene_parts.active(engine):                    # referans + senaryo: sahnenin parçası tek istekte
        plan = scene_parts.plan_part(s, sc, with_sha=with_sha)
        out.update(plan.as_dict())
        out["approval"] = {"required": scene_parts.plan_required(cfg), "approved": plan.approved(),
                           "plan_hash": plan.plan_hash}
    else:
        plan = fal_plan.scene_plan(s, sc, engine, with_sha=with_sha)
        out.update(plan.as_dict())
    from app.budget import pricing
    from app.engine import finishing
    seconds = float(out.get("duration_s") or sc.duration_s or 5.0)
    out["upscale_estimate"] = {e: pricing.upscale_cost(e, seconds, 24.0, settings.upscale_target, settings.upscale_tier)
                               for e in finishing.UPSCALE_ENDPOINTS}            # 1080p'ye çıkarma, kaynak fps
    if out.get("draft") or (engine == "seedance25" and fal_plan.draft(engine, sc, cfg)):
        out["complete_estimate"] = pricing.seedance25_complete_cost(seconds)      # taslağı 1080p'ye tamamlama
    if sc.seedance_draft:
        out["seedance_draft"] = finishing.complete_preview(s, sc)
    up = upgrade_state(sc, cfg)
    if up["available"] or engine == fal_plan.draft_engine(cfg):
        out["upgrade"] = upgrade_preview(s, sc, cfg) if up["available"] else \
            {**up, "engine": fal_plan.final_engine(cfg), "engine_label": fal_plan.ENGINE_TR.get(fal_plan.final_engine(cfg))}
    return out


@router.get("/scenes/{scene_id}/fal-plan")
def fal_plan_view(scene_id: SceneId, s: Session = Depends(get_session)):
    """Denetçi için hafif plan (SHA hesaplanmaz): motor, mod ve gerekçesi, referanslar, çözünürlük, süre, ses, tahmin ve
    yükseltme tahmini."""
    return scene_dry_run(s, scene_or_404(s, scene_id), with_sha=False)


@router.get("/projects/{project_id}/render-plan")
def render_plan(project_id: ProjectId, dry_run: bool = True, s: Session = Depends(get_session)):
    """Projenin her çekimi için kuru çalıştırma planı ve toplamlar (taslak ve yükseltme). Her zaman kurudur. Referans
    modunda bir parça (aynı istek) yalnızca bir kez sayılır."""
    if not dry_run:
        raise HTTPException(422, "render-plan yalnızca kuru çalıştırmadır (dry_run=true); üretim için render-all.")
    project_or_404(s, project_id)
    rows = [scene_dry_run(s, sc, with_sha=True) for sc in
            s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code))]
    seen, total = set(), 0.0
    for r in rows:
        key = r.get("part_hash") or r["scene_id"]
        if r.get("cost") and key not in seen:
            seen.add(key)
            total += r["cost"]["reserve_usd"]
    seen_up, upgrade = set(), 0.0
    for r in rows:
        up = r.get("upgrade") or {}
        for part in up.get("parts") or ([{"part": [r["scene_code"]], "usd": up["cost"]["reserve_usd"]}]
                                        if up.get("cost") else []):
            key = tuple(part["part"])
            if key not in seen_up:
                seen_up.add(key)
                upgrade += part["usd"]
    cfg = project_settings(s.get(Project, project_id).settings)
    return {"dry_run": True, "network": False, "paid_mode": not settings.dry_run, "scenes": rows,
            "requests": len(seen), "total_usd": round(total, 4), "upgrade_total_usd": round(upgrade, 4),
            "remaining_usd": remaining_budget(s, project_id),
            "input_mode": settings.video_input_mode, "render_mode": settings.scene_render_mode,
            "engines": {"draft": fal_plan.draft_engine(cfg), "final": fal_plan.final_engine(cfg)}}


class PlanApproveIn(BaseModel):
    plan_hash: str | None = None       # panelin gördüğü plan; değiştiyse onay reddedilir


@router.post("/scenes/{scene_id}/plan/approve")
@curated("plan_approve")
def approve_plan(scene_id: SceneId, body: PlanApproveIn, render: bool = False, s: Session = Depends(get_session)):
    """Referans modu: kuru çalıştırma planını (motor, referanslar ve numaraları, istem, süre, maliyet) parçanın bütün
    çekimleri için onaylar. render: onaydan sonra parçanın çekimlerini üretime verir."""
    from app.api.plan import Busy, dispatch_result
    sc = scene_or_404(s, scene_id)
    if not scene_parts.active(sc.engine):
        raise HTTPException(409, "Plan onayı yalnızca referans modunda (H3 Max / Seedance 2.5) kullanılır.")
    plan = scene_parts.plan_part(s, sc)
    if plan.missing:
        raise HTTPException(409, "Eksik referans: " + ", ".join(m["name"] for m in plan.missing) +
                                 " (görsel yükleyin; plan onaylanamaz).")
    if body.plan_hash and body.plan_hash != plan.plan_hash:
        raise HTTPException(409, "Plan değişti (istem, referans, süre ya da motor); planı yeniden inceleyin.")
    scene_parts.approve(s, sc, plan)
    for x in plan.shots:
        if x.status == "plan_review":
            x.status, x.last_error = "pending", None
    s.commit()
    dispatched = {}
    if render:
        ensure_not_frozen(str(sc.project_id))
        for x in plan.shots:
            try:
                dispatched[x.scene_code] = dispatch_result(s, x, force_visual=False)
            except Busy as e:
                dispatched[x.scene_code] = {"stages": [], "busy": e.detail}
    return {"approved": True, "plan_hash": plan.plan_hash, "part": [x.scene_code for x in plan.shots],
            "total_usd": plan.cost["reserve_usd"], "dispatched": dispatched}


@router.post("/scenes/{scene_id}/upgrade-final")
@curated("upgrade_final")
def upgrade_final(scene_id: SceneId, expected_usd: float | None = None, s: Session = Depends(get_session)):
    """"Seedance 2.5 ile yükselt" (tek adımlı onay: panel tahmini gösterir, yönetmen onaylar). Referans modunda bütün
    sahne yükseltilir (parçaları final motoruyla yeniden planlanır, planları bu onayla onaylanmış sayılır); değilse çekim.
    Bütçe kapısı: tahmini toplam kalan bütçeyi aşarsa reddedilir. expected_usd: panelin gösterdiği tahmin; güncel tahmin
    ondan yüksekse (plan değişti) reddedilir. Eski klipler varyasyon olarak kalır."""
    from app.api.plan import Busy, dispatch_result
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    preview = upgrade_preview(s, sc, cfg)
    if not preview["available"]:
        raise HTTPException(409, f"{sc.scene_code}: {preview['reason']}")
    if expected_usd is not None and preview["total_usd"] > float(expected_usd) + 0.01:
        raise HTTPException(409, f"Tahmin değişti: şimdi {preview['total_usd']:.2f} $ (onaylanan {float(expected_usd):.2f} $); "
                                 "yeniden inceleyin.")
    if not preview["budget_ok"]:
        raise HTTPException(409, preview["reason"])
    targets = upgrade_targets(s, sc, cfg)
    drafts = {x.id: s.scalar(select(Variation).where(Variation.scene_id == x.id, Variation.refined_rank == 1))
              for x in targets}
    if any(v is None for v in drafts.values()):
        missing = [x.scene_code for x in targets if drafts[x.id] is None]
        raise HTTPException(409, f"Taslak klip yok: {', '.join(missing)}; önce taslağı üretin.")
    draft = drafts.get(sc.id) or next(iter(drafts.values()))
    previous = sc.engine
    for x in targets:
        x.engine, x.upgrade_requested = preview["engine"], True
    s.flush()
    if scene_parts.active(preview["engine"]):          # bu onay, final planlarının onayıdır (tek adım)
        for x in targets:
            scene_parts.approve(s, x, scene_parts.plan_part(s, x))
    s.commit()
    dispatched = {}
    for x in targets:
        try:
            dispatched[x.scene_code] = dispatch_result(s, x, rerun_visual=True)
        except Busy as e:
            dispatched[x.scene_code] = {"stages": [], "busy": e.detail}
    first = dispatched.get(sc.scene_code, {})
    return {**first, "engine": preview["engine"], "from_engine": previous, "draft_variation_id": str(draft.id),
            "shots": preview["shots"], "parts": preview["parts"], "total_usd": preview["total_usd"],
            "dispatched": dispatched}
