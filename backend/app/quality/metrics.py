"""Verimlilik metrikleri (Faz 17): aynı kaliteye daha az üretimle ulaşmak ölçülebilir olsun.

Varyasyon sayımına yalnızca sahne üretimleri girer (hareket testleri varlığa aittir; dış çekim kaydı bir üretim
değildir). Harcama projenin defterinden gelir (storyboard kareleri, referanslar ve hareket testleri dahil).
"""
import json
from collections import Counter, defaultdict

from sqlalchemy import select, text

from app import storage
from app.db import session_scope
from app.models import Project, Scene, Sequence, Variation

def _film_minutes(project: Project, scenes: list[Scene]) -> tuple[float, str]:
    """Bitmiş filmin süresi: birleştirilmiş filmin kesme listesinden, yoksa tamamlanan çekimlerin sürelerinden."""
    if project.timeline_key:
        try:
            cut = json.loads(storage.read_bytes(project.timeline_key.rsplit(".", 1)[0] + ".json"))
            return cut["duration_s"] / 60.0, "film"
        except Exception:                                  # kesme listesi okunamıyor: sahne sürelerine düş
            pass
    return sum(sc.duration_s for sc in scenes if sc.status == "done") / 60.0, "shots"


def _ratio(a: float, b: float, digits: int = 3) -> float | None:
    return round(a / b, digits) if b else None


def compute(project_id: str) -> dict:
    with session_scope() as s:
        project = s.get(Project, project_id)
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id)).all()
        sequences = s.scalars(select(Sequence).where(Sequence.project_id == project_id)
                              .order_by(Sequence.seq_order)).all()
        variations = s.scalars(select(Variation).join(Scene, Scene.id == Variation.scene_id)
                               .where(Scene.project_id == project_id)).all()
        ledger = s.execute(text("""SELECT split_part(provider, ':', 1),
                                          SUM(CASE WHEN kind = 'refund' THEN -amount_usd ELSE amount_usd END)
                                   FROM cost_ledger WHERE project_id = :p AND kind IN ('commit', 'refund')
                                   GROUP BY 1 ORDER BY 2 DESC"""), {"p": project_id}).all()
        spent = float(project.spent_usd)
        modes = {bool(x) for x in s.execute(text("""SELECT DISTINCT dry_run FROM service_calls WHERE project_id = :p
                                                     AND status = 'succeeded'"""), {"p": project_id}).scalars()}
        spend_mode = "mixed" if len(modes) > 1 else "virtual" if modes == {True} else "real" if modes else "none"
        minutes, minutes_source = _film_minutes(project, scenes)
        minutes = round(minutes, 3)                     # gösterilen dakika ile maliyet aynı sayıdan hesaplansın
        s.expunge_all()

    generated = [v for v in variations if (v.params or {}).get("source", "generate") != "external"]
    accepted = [sc for sc in scenes if sc.status == "done"]
    by_scene_seq = {sc.id: sc.sequence_id for sc in scenes}
    seq_vars = Counter(by_scene_seq[v.scene_id] for v in generated)
    seq_done = Counter(sc.sequence_id for sc in accepted)
    seq_spent = {q.id: float(q.spent_usd) for q in sequences}
    seq_total_spent = sum(seq_spent.values())

    purges = Counter()
    for v in generated:
        if v.status == "failed":
            purges[(v.params or {}).get("purge_reason") or "failed"] += 1
        elif v.status == "purged":
            purges[(v.params or {}).get("purge_reason") or "unknown"] += 1

    # Test turu: her başarısız tur bir düzeltme turudur (correction_count); görüntüsü üretilen çekimde son tur başarılıdır.
    probed = defaultdict(int)
    for v in generated:
        if v.tier == "probe":
            probed[v.scene_id] += 1
    probe_scenes = [sc for sc in scenes if sc.id in probed]
    succeeded = [sc for sc in probe_scenes if sc.visual_hash is not None and sc.status != "needs_review"]
    rounds = sum(sc.correction_count for sc in probe_scenes) + len(succeeded)

    drafted = [sc for sc in scenes if sc.draft_video_key]
    upgraded = [sc for sc in drafted if sc.cut_tier == "final"]

    return {
        "variations": len(generated),
        "accepted_shots": len(accepted),
        "variations_per_accepted_shot": _ratio(len(generated), len(accepted), 2),
        "spent_usd": round(spent, 4),
        "film_minutes": minutes, "film_minutes_source": minutes_source,
        "cost_per_film_minute_usd": _ratio(spent, minutes, 2),
        "spend_mode": spend_mode,          # virtual (ücretsiz mod, sanal) | real (ücretli mod) | mixed | none
        "spent_by_kind": {kind: round(float(usd), 4) for kind, usd in ledger},
        "sequences": [{
            "id": str(q.id), "code": q.code, "title": q.title, "variations": seq_vars.get(q.id, 0),
            "accepted_shots": seq_done.get(q.id, 0),
            "variations_per_accepted_shot": _ratio(seq_vars.get(q.id, 0), seq_done.get(q.id, 0), 2),
            "spent_usd": round(seq_spent[q.id], 4),
            "variation_share": _ratio(seq_vars.get(q.id, 0), len(generated)),
            "spend_share": _ratio(seq_spent[q.id], seq_total_spent),
        } for q in sequences],
        "purge_reasons": dict(sorted(purges.items(), key=lambda kv: -kv[1])),
        "probe": {"scenes": len(probe_scenes), "rounds": rounds, "succeeded": len(succeeded),
                  "success_rate": _ratio(len(succeeded), rounds),
                  "mean_correction_rounds": _ratio(sum(sc.correction_count for sc in probe_scenes), len(probe_scenes), 2)},
        "draft": {"drafted": len(drafted), "upgraded": len(upgraded), "upgrade_ratio": _ratio(len(upgraded), len(drafted))},
    }
