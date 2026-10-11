"""Tahmini üretim maliyeti: senaryodan (proje oluşturulmadan) ya da projenin çekimlerinden, çekim başına ve toplam.

Ağ yok, ücretli modül yüklenmez; ücretsiz modda da gerçek fiyatlarla (pricing.yaml) hesaplanır. Rezervasyonla aynı
fonksiyonları kullanır (`pricing.estimate_video`, `pricing.estimate_keyframe`), panel ve bütçe aynı sayıyı görür.

Çekim başına:
  ilk kare     FL2VA çekiminde bir kare (karakterliyse Qwen-Image-Edit, değilse Qwen-Image); Ref2VA'da kare yok.
  son kare     sonraki çekim continuous değilse (kesme, yeni sahne, sekans sonu) çekim sonu karesi Kn_end: Qwen-Image-Edit
               Lightning (~15 GPU-sn); proje ayarı chain_end_frame kapalıysa yok.
  test turu    probe_n klip, probe_resolution'da.
  tam tur      en çok full_n klip, full_resolution'da (erken durma daha azını üretir).
  kesme karesi kesmede ilk karenin yöntemi (core/chain.cut_method) ayrı kalem: kırp + iyileştir (Lightning iyileştirme;
               odak karakter önceki karede yoksa önce bir ara kare) ya da açı LoRA'sı (QWEN_EDIT_ANGLES_MODE kalitesinde).
  tahmini      kare + test turu + tam tur.
  en çok       kimlik kapısının bütün yeniden denemeleri + bütün düzeltme turları (max_correction_rounds) dahil.
Toplam: çekimlerin toplamı + kullanılan her model için bir soğuk başlatma (kuyruk modele göre gruplu: Qwen-Image,
Qwen-Image-Edit, H3 FL2VA, H3 Ref2VA her biri bir kez yüklenir).
"""
import math

from app.budget import pricing
from app.config import settings

H3_SECONDS = (4, 15)                 # açık model 4-15 sn üretir (kısa çekim kesilir); live/vast_h3.SECONDS ile aynı
EDIT_ATTEMPTS = 3                    # Qwen-Image-Edit karesi: ilk deneme + 2 yeniden deneme (engine/storyboard.EDIT_RETRIES)


def billed_seconds(engine: str, duration_s: float) -> float:
    if engine == "h3":
        return float(min(H3_SECONDS[1], max(H3_SECONDS[0], math.ceil(duration_s))))
    return float(duration_s)


END_MODE = "lightning"               # çekim sonu karesi (engine/storyboard.END_MODE)


def cut_costs(cfg: dict, cut: dict, frame_usd: float, prices: dict | None = None) -> dict:
    """Kesme karesinin kalemleri: {"cut_method", "cut_attempts", "cut_place_usd", "cut_in_usd", "angles_usd",
    "first_usd", "worst_usd"}. Ara kare (odak önceki karede yok) bir kez çizilir, denemeler yeniden kullanır."""
    from app.core.h3mode import edit_mode
    from app.engine.cutin import REFINE_MODE
    methods = list(cut.get("methods") or [cut["method"]])
    place = pricing.estimate_keyframe("h3", prices, model="qwen_edit", edit_mode=edit_mode(cfg)) \
        if cut.get("placed") and any(m in ("cut_in", "angles") for m in methods) else 0.0
    refine = pricing.estimate_keyframe("h3", prices, model="qwen_edit", edit_mode=REFINE_MODE)
    angles = pricing.estimate_keyframe("h3", prices, model="qwen_edit", edit_mode=settings.qwen_edit_angles_mode)
    each = {"cut_in": refine, "angles": angles}
    costs = [each.get(m, frame_usd) for m in methods]
    return {"cut_method": cut["method"], "cut_attempts": methods, "cut_place_usd": round(place, 4),
            "cut_in_usd": round(place + refine, 4) if "cut_in" in methods else None,
            "angles_usd": round(place + angles, 4) if "angles" in methods else None,
            "first_usd": round(place + costs[0], 4), "worst_usd": round(place + sum(costs), 4)}


def shot(cfg: dict, code: str, duration_s: float, engine: str = "h3", mode: str = "fl2va", frame: str = "qwen",
         prices: dict | None = None, end_frame: bool = False, cut: dict | None = None) -> dict:
    seconds = billed_seconds(engine, duration_s)
    h3_mode = mode if engine == "h3" else None
    probe = pricing.estimate_video(engine, cfg["probe_resolution"], seconds, prices, mode=h3_mode)
    full = pricing.estimate_video(engine, cfg["full_resolution"], seconds, prices, mode=h3_mode)
    from app.core.h3mode import edit_mode
    needs_frame = mode != "ref2va" and engine in ("h3", "seedance")
    quality = edit_mode(cfg)
    frame_usd = pricing.estimate_keyframe("h3", prices, model=frame, edit_mode=quality) if needs_frame else 0.0
    end = bool(end_frame and engine == "h3" and mode == "fl2va" and cfg.get("chain_end_frame", True))
    end_usd = pricing.estimate_keyframe("h3", prices, model="qwen_edit", edit_mode=END_MODE) if end else 0.0
    probe_n, full_n, rounds = int(cfg["probe_n"]), int(cfg["full_n"]), int(cfg["max_correction_rounds"])
    frame_worst = frame_usd * (EDIT_ATTEMPTS if frame == "qwen_edit" and needs_frame else 1)
    items = cut_costs(cfg, cut, frame_usd, prices) if cut and needs_frame and frame == "qwen_edit" else None
    if items:                                        # kesme karesi: yöntemin kendi kalemleri
        frame_usd, frame_worst = items.pop("first_usd"), items.pop("worst_usd")
    expected = frame_usd + end_usd + probe_n * probe + full_n * full
    worst = frame_worst + end_usd * EDIT_ATTEMPTS + probe_n * probe * (1 + rounds) + full_n * full
    return {"code": code, "duration_s": duration_s, "billed_seconds": seconds, "engine": engine, "mode": mode,
            "frame": frame if needs_frame else None, "frame_usd": round(frame_usd, 4),
            "edit_mode": quality if needs_frame and frame == "qwen_edit" else None, "end_frame": end,
            "end_frame_usd": round(end_usd, 4), "probe_clip_usd": round(probe, 4),
            "full_clip_usd": round(full, 4), "clips": probe_n + full_n, "expected_usd": round(expected, 4),
            "max_usd": round(worst, 4), **(items or {})}


def summarize(shots: list[dict], prices: dict | None = None) -> dict:
    models = {s["frame"] for s in shots if s["frame"] in ("qwen", "qwen_edit")} | \
        {s["mode"] for s in shots if s["engine"] == "h3"} | ({"qwen_edit"} if any(s.get("end_frame") for s in shots) else set())
    rp = pricing.serverless(prices) if settings.h3_platform == "runpod" else None
    cold = pricing.estimate_runpod(float(rp.get("cold_start_seconds", 0)), None, prices) if rp else 0.0
    loads = round(cold * len(models), 4)
    return {"shots": shots, "model_loads": len(models), "cold_start_usd": loads,
            "end_frames": sum(bool(s.get("end_frame")) for s in shots),
            "end_frame_usd": round(sum(s.get("end_frame_usd", 0.0) for s in shots), 4),
            "cut_in_frames": sum((s.get("cut_attempts") or [None])[0] == "cut_in" for s in shots),   # uygulanacak yol
            "angles_frames": sum((s.get("cut_attempts") or [None])[0] == "angles" for s in shots),
            "expected_usd": round(sum(s["expected_usd"] for s in shots) + loads, 4),
            "max_usd": round(sum(s["max_usd"] for s in shots) + loads, 4)}


def from_script(text: str, cfg: dict) -> dict:
    """Senaryodan (proje yokken): her çekim H3 FL2VA; karakterli çekimin karesi Qwen-Image-Edit sayılır (referanslar
    üretimden önce onaylanır; lint aksi hâlde üretimi durdurur)."""
    from app.hermes import rules
    edit = settings.keyframe_character_provider == "qwen_edit"
    shots = rules.parse(text)["shots"]
    ends = [i + 1 == len(shots) or shots[i + 1].get("transition") != "continuous"
            or shots[i + 1].get("sequence") != s.get("sequence") for i, s in enumerate(shots)]
    return summarize([shot(cfg, s["scene_id"], float(s.get("duration_s") or 4.0),
                           frame="qwen_edit" if edit and (s.get("characters") or s.get("carried")) else "qwen",
                           end_frame=end) for s, end in zip(shots, ends)])


def from_project(s, project_id: str, cfg: dict) -> dict:
    """Projenin çekimlerinden: çekimin motoru, H3 koşulu ve ilk kare modeli. Dış çekimler (yüklenen klip) ücretsizdir."""
    from sqlalchemy import select
    from app.core.chain import (CHAINED, camera_of, cut_attempts, cut_method, next_continuous, plans_for,
                                transition_of, wants_compose)
    from app.core.h3mode import effective_mode, keyframe_model, strict_refs
    from app.core.state import load_assets, load_characters
    from app.models import Scene
    out, previous = [], {}
    plans = plans_for(s, project_id)
    angles_on = settings.qwen_edit_angles == "on" and bool(settings.qwen_edit_angles_lora)
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)):
        group = sc.sequence_id or sc.project_id
        prev, previous[group] = previous.get(group), sc
        if (sc.method or "generate") == "external":
            continue
        cut = None
        chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
        if prev is not None and transition_of(sc) == "cut":
            prev_cam, cam = camera_of(prev), camera_of(sc)
            scaled = any(getattr(a, "scale", None) for a in assets if getattr(a, "type", None) != "location")
            method = cut_method(prev_cam, cam, sc.cut_method, compose=wants_compose(sc, scaled))
            plan = plans.get(sc.id) or {}
            focus = [n for n in plan.get("cast") or [] if n not in (plan.get("carried") or [])]
            cut = {"method": method, "methods": cut_attempts(method, prev_cam, cam, angles_on),
                   "placed": bool(set(focus) - set((plans.get(prev.id) or {}).get("cast") or [])) and method != "compose"}
        mode = effective_mode(sc, chars, assets) if (sc.engine or "h3") == "h3" else "fl2va"
        frame = "upload" if sc.keyframe_key else keyframe_model(sc, chars, assets, strict_refs(cfg))
        if frame == "qwen" and prev is not None and transition_of(sc) in CHAINED \
                and settings.keyframe_character_provider == "qwen_edit":
            frame = "qwen_edit"                     # zincir karesi önceki kareden Qwen-Image-Edit ile çizilir
        row = shot(cfg, sc.scene_code, float(sc.duration_s or 4.0), sc.engine or "h3", mode,
                   "qwen" if frame == "upload" else frame, end_frame=next_continuous(s, sc) is None,
                   cut=cut if frame != "upload" else None)
        if frame == "upload":                       # yönetmenin ilk karesi: çizim yok
            row.update(frame="upload", expected_usd=round(row["expected_usd"] - row["frame_usd"], 4),
                       max_usd=round(row["max_usd"] - row["frame_usd"], 4), frame_usd=0.0)
        out.append(row)
    return summarize(out)
