"""Kuru çalıştırma (fal.ai'ye gitmeden istek planı) ve "Seedance 2.5 ile yükselt".

`POST /scenes/{id}/render?dry_run=true` ve `GET /projects/{id}/render-plan?dry_run=true`: hiçbir şey kuyruğa verilmez,
hiçbir dosya yüklenmez, hiçbir ücretli modül yüklenmez. Dönen gövde, ücretli modda gönderilecek gövdenin kendisidir
(dosya adresleri yerine "local:<anahtar>" ve SHA-256).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, SceneId, ensure_not_frozen, project_or_404, scene_or_404
from app.config import project_settings, settings
from app.core.rerender import plan_rerender, stage_names
from app.core.state import load_assets, load_characters
from app.curation import curated
from app.db import get_session
from app.engine import fal_plan
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


def scene_dry_run(s: Session, sc: Scene, force_visual: bool = False, with_sha: bool = True) -> dict:
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    engine = sc.engine or fal_plan.draft_engine(cfg)
    out = {"dry_run": True, "scene_id": str(sc.id), "scene_code": sc.scene_code, "engine": engine,
           "engine_label": fal_plan.ENGINE_TR.get(engine, engine), "stages": _stages(s, sc, force_visual),
           "paid_mode": not settings.dry_run, "network": False}
    if (sc.method or "generate") != "generate":
        return {**out, "note": f"Yöntem {sc.method}: fal.ai video çağrısı yok."}
    if h3_disabled(engine) and not settings.dry_run:
        return {**out, "disabled": True, "message": H3_DISABLED, "suggest": "h3max"}
    if not fal_plan.is_fal(engine):
        note = (H3_DISABLED if h3_disabled(engine) else
                f"{fal_plan.ENGINE_TR.get(engine, engine)}: kuru çalıştırma gövdesi yalnızca H3 Max ve Seedance 2.5 "
                "için verilir.")
        return {**out, **({"disabled": True, "suggest": "h3max"} if h3_disabled(engine) else {}), "note": note}
    plan = fal_plan.scene_plan(s, sc, engine, with_sha=with_sha)
    out.update(plan.as_dict())
    up = upgrade_state(sc, cfg)
    if up["available"] or engine == fal_plan.draft_engine(cfg):
        final = fal_plan.scene_plan(s, sc, fal_plan.final_engine(cfg), with_sha=False)
        out["upgrade"] = {**up, "engine": final.engine, "engine_label": fal_plan.ENGINE_TR[final.engine],
                          "mode": final.mode, "endpoint": final.endpoint, "cost": final.cost}
    return out


@router.get("/scenes/{scene_id}/fal-plan")
def fal_plan_view(scene_id: SceneId, s: Session = Depends(get_session)):
    """Denetçi için hafif plan (SHA hesaplanmaz): motor, mod ve gerekçesi, referanslar, çözünürlük, süre, ses, tahmin ve
    yükseltme tahmini."""
    return scene_dry_run(s, scene_or_404(s, scene_id), with_sha=False)


@router.get("/projects/{project_id}/render-plan")
def render_plan(project_id: ProjectId, dry_run: bool = True, s: Session = Depends(get_session)):
    """Projenin her çekimi için kuru çalıştırma planı ve toplamlar (taslak ve yükseltme). Her zaman kurudur."""
    if not dry_run:
        raise HTTPException(422, "render-plan yalnızca kuru çalıştırmadır (dry_run=true); üretim için render-all.")
    project_or_404(s, project_id)
    rows = [scene_dry_run(s, sc, with_sha=True) for sc in
            s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code))]
    total = sum(r["cost"]["reserve_usd"] for r in rows if r.get("cost"))
    upgrade = sum(r["upgrade"]["cost"]["reserve_usd"] for r in rows if r.get("upgrade", {}).get("cost"))
    return {"dry_run": True, "network": False, "paid_mode": not settings.dry_run, "scenes": rows,
            "total_usd": round(total, 4), "upgrade_total_usd": round(upgrade, 4),
            "engines": {"draft": fal_plan.draft_engine(project_settings(s.get(Project, project_id).settings)),
                        "final": fal_plan.final_engine(project_settings(s.get(Project, project_id).settings))}}


@router.post("/scenes/{scene_id}/upgrade-final")
@curated("upgrade_request")
def upgrade_final(scene_id: SceneId, s: Session = Depends(get_session)):
    """"Seedance 2.5 ile yükselt": taslak motorla üretilmiş çekimi final motoruyla yeniden üretir. Aynı kare ve
    referanslar kullanılır (görsel özet motor ailesini görür, storyboard karesi yeniden çizilmez); eski klip varyasyon
    olarak kalır (yönetmen seçimiyle geri dönülebilir)."""
    from app.api.plan import dispatch_result
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    up = upgrade_state(sc, cfg)
    if not up["available"]:
        raise HTTPException(409, f"{sc.scene_code}: {up['reason']}")
    draft = s.scalar(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank == 1))
    if draft is None:
        raise HTTPException(409, f"{sc.scene_code}: taslak klip yok; önce taslağı üretin.")
    previous = sc.engine
    sc.engine, sc.upgrade_requested = up["engine"], True
    s.commit()
    result = dispatch_result(s, sc, rerun_visual=True)
    return {**result, "engine": sc.engine, "from_engine": previous, "draft_variation_id": str(draft.id)}
