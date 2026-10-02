"""Quality Gate: skorlama, eşik ve Rafine Havuz.

Ücretsiz modda CLIP skoru yerine seed'e bağlı tekrarlanabilir, Gauss dağılımlı sahte skor kullanılır; anomali
tespiti ise gerçektir (FFmpeg). Gerçek skor (OpenCLIP stil + InsightFace yüz) Faz 18'in sonraki adımlarındadır.
"""
import logging
import random
from dataclasses import dataclass

from sqlalchemy import select, update

from app import storage
from app.config import settings
from app.db import session_scope
from app.models import Scene, Variation
from app.quality.anomaly import detect_anomalies

log = logging.getLogger("hellgrind.gate")


@dataclass(frozen=True)
class Score:
    clip: float
    face: float | None
    composite: float
    anomalies: list[dict]
    passed: bool


def ensure_scorer() -> None:
    """Sahte skor yalnızca ücretsiz modda geçerlidir. Ücretli modda gerçek bir skorlayıcı yoksa üretim başlamadan,
    yani para harcanmadan durur; yoksa rastgele skorlar ücretli klipleri sessizce onaylar ya da reddederdi."""
    if not settings.dry_run:
        raise NotImplementedError("Gerçek kalite skoru (OpenCLIP/InsightFace) henüz yok: ücretli modda üretim "
                                  "başlatılamaz. Ücretsiz modda sahte skor kullanılır.")


def clip_score_dry(seed: int) -> float:
    rng = random.Random(seed ^ 0x5EED)
    return round(min(max(rng.gauss(settings.dry_run_score_mean, settings.dry_run_score_sd), 0.0), 1.0), 4)


def score_video(path: str, seed: int, cfg: dict) -> Score:
    """Birleşik skor + anomaliler. Eşik altı CLIP skoru da `low_clip` anomalisi sayılır; geçmek = anomali yok."""
    clip = clip_score_dry(seed)
    anomalies = detect_anomalies(path)
    if clip < cfg["clip_threshold"]:
        anomalies.append({"type": "low_clip", "score": clip, "threshold": cfg["clip_threshold"]})
    return Score(clip=clip, face=None, composite=clip, anomalies=anomalies, passed=not anomalies)


# ---------- Rafine Havuz ----------

def excellent_count(scene_id, run_id: str, threshold: float) -> int:
    """Bu çalıştırmanın tam turunda eşiği geçip `threshold` üstünde skor alanların sayısı (erken durma için)."""
    with session_scope() as s:
        return len(s.scalars(select(Variation.id).where(
            Variation.scene_id == scene_id, Variation.tier == "full", Variation.status == "passed",
            Variation.params["run"].astext == run_id, Variation.composite_score >= threshold)).all())


def finalize_refined_pool(scene_id, run_id: str, k: int, scene_update: dict) -> list[str]:
    """Bu çalıştırmanın tam turunda geçenlerden en iyi k'yı rank 1..n ile Rafine Havuz'a yazar.

    Hepsi tek işlemde: yeni sıralar, eski havuzun ve açıkta kalmış adayların emekli edilmesi ("superseded"),
    kalanların "not_top_k" ile imhası ve sahne güncellemesi (`scene_update`). Kazanan yoksa hiçbir şeye dokunulmaz:
    eski havuz yerinde kalır. Dosyalar işlem kesinleştikten sonra karantinaya taşınır."""
    with session_scope() as s:
        mine = s.scalars(select(Variation).where(
            Variation.scene_id == scene_id, Variation.tier == "full", Variation.status == "passed",
            Variation.params["run"].astext == run_id).order_by(Variation.composite_score.desc(), Variation.seed)).all()
        winners = mine[:k]
        if not winners:
            return []
        winner_ids = {v.id for v in winners}
        s.execute(update(Variation).where(Variation.scene_id == scene_id).values(refined_rank=None))
        s.flush()
        retire = []
        for v in s.scalars(select(Variation).where(Variation.scene_id == scene_id,
                                                   Variation.status.in_(("refined", "passed")))):
            if v.id in winner_ids:
                continue
            reason = "not_top_k" if (v.params or {}).get("run") == run_id else "superseded"
            retire.append((v.id, v.video_key, reason))
            v.status = "purged"
            v.params = {**(v.params or {}), "purge_reason": reason}
        for rank, v in enumerate(winners, start=1):
            v.refined_rank, v.status = rank, "refined"
        scene = s.get(Scene, scene_id)
        for field, value in scene_update.items():
            setattr(scene, field, value)
        ranked = [str(v.id) for v in winners]

    purge_files(retire)
    return ranked


def purge_files(items: list[tuple]) -> None:
    """[(variation_id, video_key, neden)]: dosyayı karantinaya taşır (hard modda siler), satırı günceller."""
    for vid, key, reason in items:
        quarantined = storage.purge(key) if key else None
        with session_scope() as s:
            v = s.get(Variation, vid)
            v.video_key = None
            v.params = {**(v.params or {}), "quarantine_key": quarantined, "purge_reason": reason}
