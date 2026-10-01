"""Quality Gate.

Kademe 1: CLIP skoru yerine seed'e bağlı tekrarlanabilir sahte skor + gerçek FFmpeg anomali tespiti.
Kademe 2: score_clip / score_face gerçek modellerle (OpenCLIP + InsightFace) değiştirilecek.
"""
import random
from dataclasses import dataclass

from sqlalchemy import select, update

from app import storage
from app.config import settings
from app.db import session_scope
from app.models import Scene, Variation
from app.quality.anomaly import detect_anomalies


@dataclass
class Score:
    clip: float
    face: float | None
    composite: float
    anomalies: list[dict]
    passed: bool


def score_clip_dry(seed: int) -> float:
    rng = random.Random(seed ^ 0x5EED)
    return round(min(max(rng.gauss(settings.dry_run_score_mean, settings.dry_run_score_sd), 0.0), 1.0), 4)


def score_video(path: str, seed: int, cfg: dict) -> Score:
    clip = score_clip_dry(seed)
    face = None                                   # Kademe 2: ArcFace
    composite = clip if face is None else round(0.4 * clip + 0.6 * face, 4)
    anomalies = detect_anomalies(path)
    if clip < cfg["clip_threshold"]:
        anomalies.append({"type": "low_clip", "score": clip, "threshold": cfg["clip_threshold"]})
    passed = not anomalies
    return Score(clip=clip, face=face, composite=composite, anomalies=anomalies, passed=passed)


def finalize_refined_pool(scene_id, k: int = 3) -> list[str]:
    """Tam turda eşiği geçenlerden en iyi k'yı Rafine Havuz'a yazar, gerisini imha eder."""
    with session_scope() as s:
        s.execute(update(Variation).where(Variation.scene_id == scene_id).values(refined_rank=None))
        passed = s.scalars(
            select(Variation)
            .where(Variation.scene_id == scene_id, Variation.status == "passed")
            .order_by(Variation.composite_score.desc())
        ).all()
        winners, losers = [], []
        for v in passed:
            if v.tier == "full" and len(winners) < k:
                winners.append(v)
            else:
                losers.append(v)              # test turu (480p) ve fazla adaylar
        for rank, v in enumerate(winners, start=1):
            v.refined_rank = rank
            v.status = "refined"
        s.flush()
        loser_keys = [(v.id, v.video_key) for v in losers if v.video_key]
        winner_ids = [str(v.id) for v in winners]

    for vid, key in loser_keys:
        q = storage.purge(key)
        with session_scope() as s:
            v = s.get(Variation, vid)
            v.status, v.video_key = "purged", None
            v.params = {**(v.params or {}), "quarantine_key": q, "purge_reason": "not_top_k"}
    return winner_ids


def refined_count(scene_id, min_score: float) -> int:
    with session_scope() as s:
        return len(s.scalars(select(Variation.id).where(
            Variation.scene_id == scene_id, Variation.tier == "full",
            Variation.status == "passed", Variation.composite_score >= min_score)).all())


def best_refined_key(scene_id) -> str | None:
    with session_scope() as s:
        return s.scalar(select(Variation.video_key).where(
            Variation.scene_id == scene_id, Variation.refined_rank == 1))

