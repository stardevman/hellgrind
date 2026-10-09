"""Tahmini üretim maliyeti: senaryodan (proje oluşturulmadan) ya da projenin çekimlerinden, çekim başına ve toplam.

Ağ yok, ücretli modül yüklenmez; ücretsiz modda da gerçek fiyatlarla (pricing.yaml) hesaplanır. Rezervasyonla aynı
fonksiyonları kullanır (`pricing.estimate_video`, `pricing.estimate_keyframe`), panel ve bütçe aynı sayıyı görür.

Çekim başına:
  ilk kare     FL2VA çekiminde bir kare (karakterliyse Qwen-Image-Edit, değilse Qwen-Image); Ref2VA'da kare yok.
  test turu    probe_n klip, probe_resolution'da.
  tam tur      en çok full_n klip, full_resolution'da (erken durma daha azını üretir).
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


def shot(cfg: dict, code: str, duration_s: float, engine: str = "h3", mode: str = "fl2va", frame: str = "qwen",
         prices: dict | None = None) -> dict:
    seconds = billed_seconds(engine, duration_s)
    h3_mode = mode if engine == "h3" else None
    probe = pricing.estimate_video(engine, cfg["probe_resolution"], seconds, prices, mode=h3_mode)
    full = pricing.estimate_video(engine, cfg["full_resolution"], seconds, prices, mode=h3_mode)
    needs_frame = mode != "ref2va" and engine in ("h3", "seedance")
    frame_usd = pricing.estimate_keyframe("h3", prices, model=frame) if needs_frame else 0.0
    probe_n, full_n, rounds = int(cfg["probe_n"]), int(cfg["full_n"]), int(cfg["max_correction_rounds"])
    expected = frame_usd + probe_n * probe + full_n * full
    worst = frame_usd * (EDIT_ATTEMPTS if frame == "qwen_edit" and needs_frame else 1) \
        + probe_n * probe * (1 + rounds) + full_n * full
    return {"code": code, "duration_s": duration_s, "billed_seconds": seconds, "engine": engine, "mode": mode,
            "frame": frame if needs_frame else None, "frame_usd": round(frame_usd, 4), "probe_clip_usd": round(probe, 4),
            "full_clip_usd": round(full, 4), "clips": probe_n + full_n, "expected_usd": round(expected, 4),
            "max_usd": round(worst, 4)}


def summarize(shots: list[dict], prices: dict | None = None) -> dict:
    models = {s["frame"] for s in shots if s["frame"] in ("qwen", "qwen_edit")} | \
        {s["mode"] for s in shots if s["engine"] == "h3"}
    rp = pricing.serverless(prices) if settings.h3_platform == "runpod" else None
    cold = pricing.estimate_runpod(float(rp.get("cold_start_seconds", 0)), None, prices) if rp else 0.0
    loads = round(cold * len(models), 4)
    return {"shots": shots, "model_loads": len(models), "cold_start_usd": loads,
            "expected_usd": round(sum(s["expected_usd"] for s in shots) + loads, 4),
            "max_usd": round(sum(s["max_usd"] for s in shots) + loads, 4)}


def from_script(text: str, cfg: dict) -> dict:
    """Senaryodan (proje yokken): her çekim H3 FL2VA; karakterli çekimin karesi Qwen-Image-Edit sayılır (referanslar
    üretimden önce onaylanır; lint aksi hâlde üretimi durdurur)."""
    from app.hermes import rules
    edit = settings.keyframe_character_provider == "qwen_edit"
    parsed = rules.parse(text)
    return summarize([shot(cfg, s["scene_id"], float(s.get("duration_s") or 4.0),
                           frame="qwen_edit" if edit and s.get("characters") else "qwen") for s in parsed["shots"]])


def from_project(s, project_id: str, cfg: dict) -> dict:
    """Projenin çekimlerinden: çekimin motoru, H3 koşulu ve ilk kare modeli. Dış çekimler (yüklenen klip) ücretsizdir."""
    from sqlalchemy import select
    from app.core.h3mode import effective_mode, keyframe_model, strict_refs
    from app.core.state import load_assets, load_characters
    from app.models import Scene
    out = []
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)):
        if (sc.method or "generate") == "external":
            continue
        chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
        mode = effective_mode(sc, chars, assets) if (sc.engine or "h3") == "h3" else "fl2va"
        frame = "upload" if sc.keyframe_key else keyframe_model(sc, chars, assets, strict_refs(cfg))
        row = shot(cfg, sc.scene_code, float(sc.duration_s or 4.0), sc.engine or "h3", mode,
                   "qwen" if frame == "upload" else frame)
        if frame == "upload":                       # yönetmenin ilk karesi: çizim yok
            row.update(frame="upload", expected_usd=round(row["expected_usd"] - row["frame_usd"], 4),
                       max_usd=round(row["max_usd"] - row["frame_usd"], 4), frame_usd=0.0)
        out.append(row)
    return summarize(out)
