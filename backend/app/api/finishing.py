"""Bitirme uç noktaları: Seedance 2.5 taslağını 1080p'ye tamamlama ve upscale (engine/finishing.py).

Hepsi tahmini tutarlı tek adımlı onaya dayanır: panel önce tahmini gösterir (GET ya da dry_run), yönetmen onaylar,
istek `expected_usd` ile gelir. Güncel tahmin ondan yüksekse ya da kalan bütçeyi aşarsa reddedilir (409).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import ProjectId, SceneId, VariationId, ensure_not_frozen, project_or_404, scene_or_404
from app.curation import curated
from app.db import get_session
from app.engine import finishing
from app.models import Scene, Variation

router = APIRouter(tags=["finishing"])


def _expected(total: float, expected_usd: float | None) -> None:
    if expected_usd is not None and total > float(expected_usd) + 0.01:
        raise HTTPException(409, f"Tahmin değişti: şimdi {total:.2f} $ (onaylanan {float(expected_usd):.2f} $); "
                                 "yeniden inceleyin.")


@router.get("/scenes/{scene_id}/draft")
def draft(scene_id: SceneId, s: Session = Depends(get_session)):
    """Seedance taslağı: draft_id, kalan süre, 1080p tamamlamanın tahmini ve daha ucuz seçenek (upscale)."""
    return finishing.complete_preview(s, scene_or_404(s, scene_id))


@router.post("/scenes/{scene_id}/seedance/complete")
@curated("draft_complete")
def complete(scene_id: SceneId, expected_usd: float | None = None, s: Session = Depends(get_session)):
    """"Taslağı 1080p'ye tamamla": onay ekranı tahmini gösterir; bütçe kapısını aşarsa engellenir."""
    from app.api.plan import ensure_idle
    from app.worker import tasks
    sc = scene_or_404(s, scene_id)
    ensure_not_frozen(str(sc.project_id))
    ensure_idle(sc)
    preview = finishing.complete_preview(s, sc)
    if not preview["available"]:
        raise HTTPException(409, f"{sc.scene_code}: {preview['reason']}")
    _expected(preview["cost"]["usd"], expected_usd)
    tasks.complete_draft.delay(str(sc.project_id), scene_id)
    return {"queued": True, "part": preview["part"], "total_usd": preview["cost"]["usd"],
            "reserve_usd": preview["cost"]["reserve_usd"], "draft_id": preview["draft"]["draft_id"]}


def _upscale(s: Session, scope: str, ref_id: str, endpoint: str | None, dry_run: bool, expected_usd: float | None,
             project_id: str) -> dict:
    from app.worker import tasks
    if endpoint is not None and endpoint not in finishing.UPSCALE_ENDPOINTS:
        raise HTTPException(422, f"Bilinmeyen upscale uç noktası: {endpoint}")
    plan = finishing.upscale_plan(s, scope, ref_id, endpoint)
    if dry_run:
        return {"dry_run": True, "network": False, **plan}
    if not plan["items"]:
        raise HTTPException(409, "Upscale edilecek klip yok (kazanan klip yok ya da zaten 1080p).")
    _expected(plan["total_usd"], expected_usd)
    if not plan["budget_ok"]:
        raise HTTPException(409, f"Bütçe yetmiyor: tahmini {plan['total_usd']:.2f} $, kalan {plan['remaining_usd']:.2f} $."
                            .replace(".", ","))
    ensure_not_frozen(project_id)
    batch = scope == "project"
    if batch:                                              # son çekimin montajından sonra film birleşir
        finishing.start_batch(project_id, [i["scene_id"] for i in plan["items"]])
    for item in plan["items"]:
        tasks.upscale_clip.delay(project_id, item["scene_id"], item["variation_id"], plan["endpoint"], batch)
    return {"queued": len(plan["items"]), **plan}


@router.post("/variations/{variation_id}/upscale")
@curated(lambda kw, r: None if kw.get("dry_run") else "upscale")
def upscale_variation(variation_id: VariationId, endpoint: str | None = None, dry_run: bool = False,
                      expected_usd: float | None = None, s: Session = Depends(get_session)):
    v = s.get(Variation, variation_id)
    if v is None or v.scene_id is None:
        raise HTTPException(404, "Varyasyon yok")
    sc = s.get(Scene, v.scene_id)
    return {"scene_id": str(sc.id), **_upscale(s, "variation", variation_id, endpoint, dry_run, expected_usd,
                                              str(sc.project_id))}


@router.post("/scenes/{scene_id}/upscale")
@curated(lambda kw, r: None if kw.get("dry_run") else "upscale")
def upscale_scene(scene_id: SceneId, endpoint: str | None = None, dry_run: bool = False,
                  expected_usd: float | None = None, s: Session = Depends(get_session)):
    sc = scene_or_404(s, scene_id)
    return _upscale(s, "scene", scene_id, endpoint, dry_run, expected_usd, str(sc.project_id))


@router.post("/projects/{project_id}/upscale")
@curated(lambda kw, r: None if kw.get("dry_run") else "upscale")
def upscale_project(project_id: ProjectId, endpoint: str | None = None, dry_run: bool = False,
                    expected_usd: float | None = None, s: Session = Depends(get_session)):
    """Bütün film (son montajdan önce): 1080p altındaki bütün kazanan klipler."""
    project_or_404(s, project_id)
    return _upscale(s, "project", project_id, endpoint, dry_run, expected_usd, project_id)
