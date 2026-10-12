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
        winner = s.scalar(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank == 1))
        if winner is not None and (winner.params or {}).get("split"):    # üretilmiş klibin gerçek bölme sınırları
            out["split"] = {**winner.params["split"], "segment": winner.params.get("segment"),
                            "segment_planned": winner.params.get("segment_planned")}
        out["generation"] = scene_parts.scene_generations(s, sc).get(sc.scene_code)
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


@router.post("/scenes/{scene_id}/prompt/write")
@curated(lambda kw, r: None if kw.get("dry_run") else "prompt_write")
def prompt_write(scene_id: SceneId, force: bool = False, dry_run: bool = False, s: Session = Depends(get_session)):
    """Parçanın istemini LLM ile yazdırır (arka planda; PROMPT_WRITER=llm). dry_run: tahmini belirteç ve bedel, önbellek
    durumu; hiçbir şey çağrılmaz. force: önbellekteki istem yeniden yazdırılır (ücretli). Ücretsiz modda ya da anahtar
    yokken 409 (kurallar istemi kullanılır)."""
    from app.engine import prompt_writer
    sc = scene_or_404(s, scene_id)
    if not scene_parts.active(sc.engine):
        raise HTTPException(409, "İstem yazıcı yalnızca referans modunda (H3 Max / Seedance 2.5) kullanılır.")
    plan = scene_parts.plan_part(s, sc)
    tokens_in, tokens_out, usd = prompt_writer.estimate_usd(plan.engine, plan.writer_input)
    meta = plan.prompt_meta or {}
    reason = ("PROMPT_WRITER=rules: istem kurallarla yazılıyor (Ayarlar'dan llm seçin)."
              if prompt_writer.mode() != "llm" else prompt_writer.unavailable())
    view = {"part": [x.scene_code for x in plan.shots], "estimate": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                                    "usd": usd},
            "cached": meta.get("llm"), "source": plan.prompt_source, "available": reason is None, "reason": reason,
            "remaining_usd": remaining_budget(s, sc.project_id)}
    if dry_run:
        return {"dry_run": True, **view}
    if reason:
        raise HTTPException(409, reason)
    ensure_not_frozen(str(sc.project_id))
    if meta.get("llm") and not meta["llm"].get("ok"):
        force = True                                  # önceki yazım başarısız: elle istek yeniden yazdırır
    if meta.get("llm") and meta["llm"].get("ok") and not force:
        return {**view, "queued": False, "note": "LLM istemi zaten yazılı (yeniden yazdırmak için force=true)."}
    if usd > view["remaining_usd"]:
        raise HTTPException(409, f"Bütçe yetmiyor: tahmini {_usd(usd)} $, kalan {_usd(view['remaining_usd'])} $.")
    queued = prompt_writer.queue(str(sc.project_id), scene_id, plan, force=force)
    return {**view, "queued": queued,
            "note": "LLM istemi yazılıyor; bitince plan güncellenir." if queued else "Bu istem zaten yazılıyor."}


@router.post("/scenes/{scene_id}/prompt/rerender")
@curated(lambda kw, r: None if kw.get("dry_run") else "prompt_rerender")
def prompt_rerender(scene_id: SceneId, expected_usd: float | None = None, dry_run: bool = False,
                    s: Session = Depends(get_session)):
    """"Bu istemle yeniden üret": parçanın şu anki istemiyle (çoğunlukla yönetmen istemi) bütün parça yeniden üretilir.
    Yönetmen istemini kaydetmek bitmiş çekimi kendiliğinden yeniden üretmez; bu onay ister. dry_run: parça, tahmin ve
    kalan bütçe. Onay planı da onaylar (gösterilen istem ve tutar); tahmin yükselmişse (expected_usd) ya da bütçe
    yetmiyorsa 409."""
    from app.api.plan import Busy, dispatch_result
    sc = scene_or_404(s, scene_id)
    if not scene_parts.active(sc.engine):
        raise HTTPException(409, "Yalnızca referans modunda (H3 Max / Seedance 2.5) kullanılır.")
    plan = scene_parts.plan_part(s, sc)
    total, remaining = round(float(plan.cost["reserve_usd"]), 4), remaining_budget(s, sc.project_id)
    view = {"part": [x.scene_code for x in plan.shots], "engine": plan.engine, "source": plan.prompt_source,
            "source_label": scene_parts.SOURCE_LABEL[plan.prompt_source], "prompt_chars": len(plan.prompt),
            "total_usd": total, "remaining_usd": remaining, "budget_ok": total <= remaining,
            "blocked": bool(plan.missing) or plan.lint_blocks}
    if dry_run:
        return {"dry_run": True, **view}
    ensure_not_frozen(str(sc.project_id))
    if view["blocked"]:
        raise HTTPException(409, "Eksik ya da onaysız referans var: önce varlıkları tamamlayın.")
    if expected_usd is not None and total > float(expected_usd) + 0.01:
        raise HTTPException(409, f"Tahmin değişti: şimdi {_usd(total)} $ (onaylanan {_usd(float(expected_usd))} $).")
    if total > remaining:
        raise HTTPException(409, f"Bütçe yetmiyor: tahmini {_usd(total)} $, kalan {_usd(remaining)} $.")
    for x in plan.shots:
        scene_parts.approve(s, x, plan)               # onay ekranı istemi ve tutarı gösterdi
    s.commit()
    leader = plan.shots[0]
    try:                                              # yeni revizyon: parçanın özeti değişir, bütün parça yeniden üretilir
        dispatched = dispatch_result(s, leader, force_visual=True)
    except Busy as e:
        raise HTTPException(409, e.detail)
    return {**view, "dispatched": {leader.scene_code: dispatched}}


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
    if plan.lint_blocks:                              # dağıtımın referans denetimi: kuru çalıştırmayla aynı
        raise HTTPException(409, "Onaylı referansı olmayan varlıklar (gereken en az durum: durağan görsel onaylı): " +
                                 ", ".join(m["name"] for m in plan.lint) + ". Varlıklar sayfasından onaylayın.")
    if body.plan_hash and body.plan_hash != plan.plan_hash:
        raise HTTPException(409, "Plan değişti (istem, referans, süre ya da motor); planı yeniden inceleyin.")
    scene_parts.approve(s, sc, plan)
    from app.api.plan import has_winner
    for x in plan.shots:
        if x.status == "plan_review":                # klibi olan çekim bitmiş hâline döner (render=false yeniden üretmez)
            x.status = ("done" if x.final_video_key else "refined") if has_winner(s, x) else "pending"
            x.last_error = None
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
            scene_parts.approve(s, x, _llm_plan(s, x))
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


# ---------- model karışımı: bir parçanın çekimleri aynı üretimden ----------

@router.post("/scenes/{scene_id}/generation/adopt")
@curated("generation_adopt")
def adopt_generation(scene_id: SceneId, variation_id: str | None = None, s: Session = Depends(get_session)):
    """"Bütün sahneyi bu motorla değiştir" (tek tık): seçilen varyasyonun üretimi (whole_scene parçası) bütün çekimlerin
    kazananı olur; kesimler zaten varsa yeniden üretim ve ücret yok, çekimlerin motoru o üretiminkine geçer. Üretimin bir
    çekimde kesimi yoksa 409 (sahneyi o motorla yeniden üretin: /scenes/{id}/scene-engine)."""
    from app.engine.finishing import _dispatch_post, _reset_auto_grade
    from app.quality import gate
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    v = s.get(Variation, variation_id) if variation_id else \
        s.scalar(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank == 1))
    if v is None or str(v.scene_id) != str(sc.id):
        raise HTTPException(404, "Bu çekimin böyle bir varyasyonu yok")
    part, codes = (v.params or {}).get("part"), (v.params or {}).get("part_codes") or [sc.scene_code]
    if not part:
        raise HTTPException(409, "Bu varyasyon bir sahne parçasından üretilmedi (per_shot / kare yolu).")
    shots = {x.scene_code: x for x in scene_parts.scene_shots(s, sc) if x.scene_code in codes}
    chosen = {}
    for code in codes:
        x = shots.get(code)
        row = s.scalar(select(Variation).where(Variation.scene_id == x.id, Variation.params["part"].astext == part,
                                               Variation.status.in_(("passed", "refined")))
                       .order_by(Variation.created_at.desc())) if x is not None else None
        if row is None:
            raise HTTPException(409, f"Bu üretimin {code} kesimi yok (elenmiş ya da silinmiş): sahneyi "
                                     f"{fal_plan.ENGINE_TR.get(v.engine, v.engine)} ile yeniden üretin.")
        chosen[str(x.id)] = str(row.id)
    for x in shots.values():
        x.engine = v.engine
    s.commit()
    for sid, vid in chosen.items():
        gate.set_single_winner(sid, vid, {"status": "refined", "lipsync_video_key": None, "final_video_key": None,
                                          "graded_video_key": None, "last_error": None})
    from app.db import session_scope
    with session_scope() as s2:
        for sid in chosen:
            _reset_auto_grade(s2, s2.get(Scene, sid))      # yeni klipler: renk eşleme montajdan sonra yeniden
    _dispatch_post(str(sc.project_id), list(chosen))
    return {"adopted": True, "engine": v.engine, "part": codes, "stages": ["LIPSYNC", "MUX"], "usd": 0.0}


@router.post("/scenes/{scene_id}/scene-engine")
@curated(lambda kw, r: None if kw.get("dry_run") else "scene_engine")
def scene_engine(scene_id: SceneId, engine: str, expected_usd: float | None = None, dry_run: bool = False,
                 s: Session = Depends(get_session)):
    """Bütün sahneyi bir motorla yeniden üretir (model değişimi sahne sınırında önerilir): sahnenin bütün çekimleri o
    motora geçer, parçaları yeniden planlanır ve bu onayla onaylanmış sayılır; bütçe kapısı ve tahmin denetimi."""
    from app.api.plan import Busy, dispatch_result
    if engine not in fal_plan.FAL_ENGINES:
        raise HTTPException(422, f"Motor: {', '.join(fal_plan.FAL_ENGINES)}")
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    targets = [x for x in scene_parts.scene_shots(s, sc) if (x.method or "generate") == "generate"]
    nested = s.begin_nested()
    try:
        for x in targets:
            x.engine = engine
        s.flush()
        plans, seen = [], set()
        for x in targets:
            plan = scene_parts.plan_part(s, x)
            if plan.part_hash not in seen:
                seen.add(plan.part_hash)
                plans.append(plan)
        total = round(sum(p.cost["reserve_usd"] for p in plans), 4)
    finally:
        nested.rollback()
    remaining = remaining_budget(s, sc.project_id)
    if dry_run:                                       # onay ekranı: tahmin, parçalar, kalan bütçe
        return {"dry_run": True, "engine": engine, "shots": [x.scene_code for x in targets], "total_usd": total,
                "parts": [{"part": [y.scene_code for y in p.shots], "usd": p.cost["reserve_usd"], "endpoint": p.endpoint}
                          for p in plans], "remaining_usd": remaining, "budget_ok": total <= remaining}
    if expected_usd is not None and total > float(expected_usd) + 0.01:
        raise HTTPException(409, f"Tahmin değişti: şimdi {total:.2f} $ (onaylanan {float(expected_usd):.2f} $).")
    if total > remaining:
        raise HTTPException(409, f"Bütçe yetmiyor: tahmini {total:.2f} $, kalan {remaining:.2f} $.".replace(".", ","))
    for x in targets:
        x.engine = engine
        x.visual_hash = None                          # görüntü yeniden planlanır
    s.flush()
    for x in targets:
        scene_parts.approve(s, x, _llm_plan(s, x))
    s.commit()
    dispatched = {}
    for x in targets:
        try:
            dispatched[x.scene_code] = dispatch_result(s, x, rerun_visual=True)
        except Busy as e:
            dispatched[x.scene_code] = {"stages": [], "busy": e.detail}
    return {"engine": engine, "shots": [x.scene_code for x in targets], "total_usd": total, "dispatched": dispatched}


@router.post("/scenes/{scene_id}/shot-engine")
@curated(lambda kw, r: None if kw.get("dry_run") else "shot_engine")
def shot_engine(scene_id: SceneId, engine: str, expected_usd: float | None = None, dry_run: bool = False,
                s: Session = Depends(get_session)):
    """"Bu çekimi ayrı üret": sahne içi model değişimi. Çekim kendi parçası olur (per_shot) ve önceki çekimin gerçek son
    karesiyle başlar; sonraki çekim de bu çekimin son karesiyle yeniden bağlanır (ikisi de yeniden üretilir). Model
    değişimi varsayılan olarak yalnızca sahne sınırında önerilir. dry_run: onay ekranının maliyet tahmini (yeniden
    üretilecek parçalar, kalan bütçe); hiçbir şey yazılmaz. Onay, parçaların planını onaylanmış sayar; bütçe kapısı ve
    tahmin denetimi (expected_usd) scene-engine ile aynıdır."""
    from app.api.plan import Busy, dispatch_result
    from app.core.chain import neighbours
    if engine not in fal_plan.FAL_ENGINES:
        raise HTTPException(422, f"Motor: {', '.join(fal_plan.FAL_ENGINES)}")
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    _, nxt = neighbours(s, sc)
    relink = [sc]
    if nxt is not None and scene_parts.heading_key(nxt) == scene_parts.heading_key(sc) \
            and (nxt.method or "generate") == "generate" and scene_parts.active(nxt.engine):
        relink.append(nxt)                            # sonraki çekim bu çekimin son karesiyle yeniden bağlanır
    nested = s.begin_nested()
    try:
        sc.engine = engine
        for x in relink:
            x.visual_hash = None
        s.flush()
        plans, seen = [], set()
        for x in relink:
            plan = scene_parts.plan_part(s, x)
            if plan.part_hash not in seen:
                seen.add(plan.part_hash)
                plans.append(plan)
        total = round(sum(p.cost["reserve_usd"] for p in plans), 4)
    finally:
        nested.rollback()
    remaining = remaining_budget(s, sc.project_id)
    if dry_run:                                       # onay ekranı: tahmin, parçalar, kalan bütçe
        return {"dry_run": True, "engine": engine, "shots": sorted({y.scene_code for p in plans for y in p.shots}),
                "relinked": [x.scene_code for x in relink], "total_usd": total,
                "parts": [{"part": [y.scene_code for y in p.shots], "usd": p.cost["reserve_usd"], "endpoint": p.endpoint}
                          for p in plans], "remaining_usd": remaining, "budget_ok": total <= remaining}
    if expected_usd is not None and total > float(expected_usd) + 0.01:
        raise HTTPException(409, f"Tahmin değişti: şimdi {_usd(total)} $ (onaylanan {_usd(float(expected_usd))} $).")
    if total > remaining:
        raise HTTPException(409, f"Bütçe yetmiyor: tahmini {_usd(total)} $, kalan {_usd(remaining)} $.")
    sc.engine = engine
    for x in relink:
        x.visual_hash = None
    s.flush()
    for x in relink:
        scene_parts.approve(s, x, _llm_plan(s, x))    # onay ekranı planı gösterdi: onaylanmış sayılır
    s.commit()
    dispatched = {}
    for x in relink:
        try:
            dispatched[x.scene_code] = dispatch_result(s, x, rerun_visual=True)
        except Busy as e:
            dispatched[x.scene_code] = {"stages": [], "busy": e.detail}
    return {"engine": engine, "relinked": [x.scene_code for x in relink], "dispatched": dispatched, "total_usd": total,
            "note": "Sahne içi model değişimi: çekim ayrı üretilir ve önceki çekimin son karesiyle başlar."}


def _llm_plan(s: Session, x):
    """Sunucunun onayladığı plan (yükseltme, sahne/çekim motoru): LLM istemi gerekiyorsa ve yazılabiliyorsa önce
    yazdırılır (ücretli, bütçe rezervasyonuyla), böylece onay LLM istemine bağlanır; yazılamazsa kurallar istemiyle."""
    from app.engine import prompt_writer
    plan = scene_parts.plan_part(s, x)
    if not prompt_writer.needed(plan):
        return plan
    try:
        prompt_writer.write(str(x.project_id), plan.writer_input)
    except Exception as e:                            # noqa: BLE001 — kurallar istemiyle devam, iz olay olarak
        from app.budget import killswitch
        killswitch.event(str(x.project_id), "prompt_write_failed", part=[y.scene_code for y in plan.shots],
                         error=prompt_writer.full_error(e))
        return plan
    return scene_parts.plan_part(s, x)


def _usd(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")
