"""Süreklilik kontrolü (Faz 16): ardışık çekimler arasında renk sıçraması.

Her çekimin montajlı videosunun orta karesinden HSV histogramları çıkarılır (ton doygunlukla ağırlıklı 12 kutu,
doygunluk 8 kutu, parlaklık 8 kutu) ve Bhattacharyya mesafesi raporlanır. Histogram mesafesi kompozisyon farkını da
ölçtüğü için (aynı sahnede geniş ve yakın çekim) karar, histogramlardan türetilen ortalamalarla verilir: parlaklık,
sıcaklık (kırmızı - mavi) ve doygunluk farkı, ölçeklerine bölünür; en büyüğü 1'i aşarsa renk sıçramasıdır. Ölçekler,
aynı paletli farklı çekimler arasındaki farkın üstünde seçildi (parlaklık ve sıcaklıkta en çok ~27, doygunlukta ~55).

Yalnızca aynı sekanstaki ardışık çekimler karşılaştırılır: sekans geçişlerindeki renk değişimi çoğunlukla bilinçlidir.

Öneri: sonraki çekimin renk ayarı, önceki çekimin ortalama parlaklık ve sıcaklığına yaklaşacak şekilde aranır. Aday
ayarlar sonraki çekimin renklendirilmemiş (dudak senkronlu) karesine GRADE aşamasıyla birebir aynı filtreyle uygulanıp
ölçülür; böylece önerilen ayar uygulanınca ölçülecek sonuç tahmin değil, ölçümdür.
"""
import logging
import os
import tempfile
import time

import numpy as np
from PIL import Image
from sqlalchemy import select

from app import storage
from app.config import settings
from app.core.hashing import grade_settings
from app.db import session_scope
from app.models import Project, Scene
from app.post.grade import clamp, mid_frame
from app.quality.anomaly import probe_duration

log = logging.getLogger("hellgrind.consistency")

SCALE = {"brightness": 40.0, "warmth": 40.0, "saturation": 80.0}
JUMP_SCORE = 1.0
EXPOSURE_STEPS = (-2.0, -1.5, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
FINE_EXPOSURE_STEPS = (-0.125, 0.0, 0.125)
TEMPERATURES = (3000, 3500, 4000, 5000, 6500, 8000, 10000, 12000)
SATURATIONS = (0.6, 0.8, 1.0, 1.2, 1.4)


# ---------- ölçüm ----------

def frame_stats(img: np.ndarray) -> dict:
    """HSV histogramları ve onlardan türetilen ortalamalar."""
    hsv = np.asarray(Image.fromarray(img).convert("HSV")).reshape(-1, 3).astype(float)
    hue = np.histogram(hsv[:, 0], bins=12, range=(0, 256), weights=hsv[:, 1] + 1)[0].astype(float)
    sat = np.histogram(hsv[:, 1], bins=8, range=(0, 256))[0].astype(float)
    val = np.histogram(hsv[:, 2], bins=8, range=(0, 256))[0].astype(float)
    centers = (np.arange(8) + 0.5) * 32
    rgb = img.reshape(-1, 3).astype(float)
    return {"hist": [h / h.sum() for h in (hue, sat, val)],
            "brightness": float((val / val.sum() * centers).sum()),
            "saturation": float((sat / sat.sum() * centers).sum()),
            "warmth": float(rgb[:, 0].mean() - rgb[:, 2].mean())}


def bhattacharyya(p: np.ndarray, q: np.ndarray) -> float:
    return float(np.sqrt(max(0.0, 1.0 - float(np.sum(np.sqrt(p * q))))))


def compare(a: dict, b: dict) -> dict:
    deltas = {k: round(b[k] - a[k], 1) for k in SCALE}
    score = max(abs(deltas[k]) / SCALE[k] for k in SCALE)
    return {"score": round(score, 3), "delta": deltas,
            "histogram_distance": round(float(np.mean([bhattacharyya(x, y) for x, y in zip(a["hist"], b["hist"])])), 3)}


# ---------- öneri ----------

def suggest(target: dict, source: str, at: float, current: dict, lut: str | None) -> tuple[dict, float]:
    """Sonraki çekim için renk ayarı: önceki çekimin ölçüsüne en yakın sonucu veren aday (koordinat araması:
    pozlama, sıcaklık, doygunluk, ince pozlama). (ayar, o ayarla ölçülen skor)."""
    cache: dict[str, float] = {}

    def cost(g: dict) -> float:
        g = clamp(g)
        key = repr(sorted(g.items()))
        if key not in cache:
            cache[key] = compare(target, frame_stats(mid_frame(source, at, g, lut)))["score"]
        return cache[key]

    best = clamp(current)
    for field, values in (("exposure", [best["exposure"] + d for d in EXPOSURE_STEPS]),
                          ("temperature", TEMPERATURES + (best["temperature"],)),
                          ("saturation", SATURATIONS + (best["saturation"],))):
        best = min((clamp({**best, field: v}) for v in values), key=cost)
    best = min((clamp({**best, "exposure": best["exposure"] + d}) for d in FINE_EXPOSURE_STEPS), key=cost)
    return best, cost(best)


# ---------- proje raporu ----------

def _pairs(scenes: list[Scene]) -> list[tuple[Scene, Scene]]:
    """Aynı sekanstaki ardışık tamamlanmış çekimler."""
    done = [sc for sc in scenes if sc.status == "done" and sc.final_video_key]
    return [(a, b) for a, b in zip(done, done[1:]) if a.sequence_id == b.sequence_id]


def check_project(project_id: str, only_scene: str | None = None) -> dict:
    """Süreklilik raporunu üretir ve projeye yazar. only_scene: yalnızca o çekimin komşu geçişleri yeniden ölçülür."""
    with session_scope() as s:
        project = s.get(Project, project_id)
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id)
                           .order_by(Scene.seq_order, Scene.scene_code)).all()
        previous = {(p["from_id"], p["to_id"]): p for p in (project.continuity or {}).get("pairs", [])}
        lut_key = project.lut_key
        s.expunge_all()
    pairs = _pairs(scenes)
    os.makedirs(settings.work_dir, exist_ok=True)
    out = []
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        lut = storage.download(lut_key, os.path.join(tmp, "film.cube")) if lut_key else None
        stats_cache: dict[str, dict] = {}

        def measured(key: str) -> dict:
            if key not in stats_cache:
                path = storage.download(key, os.path.join(tmp, f"{len(stats_cache)}.mp4"))
                stats_cache[key] = frame_stats(mid_frame(path, probe_duration(path) / 2))
            return stats_cache[key]

        for a, b in pairs:
            ids = (str(a.id), str(b.id))
            old = previous.get(ids)
            if only_scene and only_scene not in ids and old is not None and old.get("final") == [a.final_video_key, b.final_video_key]:
                out.append(old)
                continue
            result = compare(measured(a.final_video_key), measured(b.final_video_key))
            pair = {"from": a.scene_code, "to": b.scene_code, "from_id": ids[0], "to_id": ids[1],
                    "final": [a.final_video_key, b.final_video_key], **result, "jump": result["score"] > JUMP_SCORE,
                    "suggestion": None, "suggestion_score": None}
            if pair["jump"] and b.lipsync_video_key:
                source = storage.download(b.lipsync_video_key, os.path.join(tmp, f"src_{b.id}.mp4"))
                grade, score = suggest(measured(a.final_video_key), source, probe_duration(source) / 2,
                                       grade_settings(b.grade), lut)
                pair["suggestion"], pair["suggestion_score"] = grade, round(score, 3)
            out.append(pair)
    report = {"checked_at": time.time(), "threshold": JUMP_SCORE, "scale": SCALE, "pairs": out}
    with session_scope() as s:
        s.get(Project, project_id).continuity = report
    log.info("süreklilik project=%s geçiş=%d sıçrama=%d", project_id, len(out), sum(p["jump"] for p in out))
    return report

