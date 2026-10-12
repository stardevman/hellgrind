"""ÜCRETLİ: Seedance 2.5 taslağını 1080p'ye tamamlama (draft/complete) ve upscale, fal.ai kuyruğunda. Bu modül testlerde
yalnızca sahte istemciyle çalışır. Akış app/engine/finishing.py'dedir (bütçe, kesim, varyasyonlar); burada yalnızca
çağrı, yükleme (SHA önbellekli) ve sonucun hemen indirilmesi vardır.
"""
from app.budget import pricing
from app.config import settings
from app.engine.providers import GenResult
from app.live import fal_queue, guard

COMPLETE_TIMEOUT_S = 900
UPSCALE_TIMEOUT_S = 900


def complete_body(draft_id: str) -> dict:
    """draft/complete gövdesi: yalnızca 1080p (şema)."""
    return {"draft_id": draft_id, "resolution": "1080p"}


def complete_draft(draft_id: str, scene_id: str | None = None, project_id: str | None = None) -> GenResult:
    guard("fal_key")
    client = fal_queue.client()
    endpoint = settings.fal_seedance25_draft_complete_endpoint
    out = fal_queue.run(client, endpoint, complete_body(draft_id), timeout_s=COMPLETE_TIMEOUT_S, scene_id=scene_id,
                        project_id=project_id, info={"engine": "seedance25", "mode": "complete"})
    video = out.result.get("video") or {}
    if not video.get("url"):
        raise fal_queue.FalError(f"fal.ai yanıtında video yok ({out.request_id})")
    path = fal_queue.download(video["url"], ".mp4", video.get("file_size"))
    from app.live.fal_engines import probe
    seconds = probe(path)["duration_s"]
    return GenResult(local_path=path, request_id=out.request_id, cost_usd=pricing.seedance25_complete_cost(seconds)["usd"],
                     engine="seedance25", meta={"endpoint": endpoint, "draft_id": draft_id, "seed": out.result.get("seed")})


def upscale(endpoint: str, path: str, body: dict, scene_id: str | None = None, project_id: str | None = None,
            estimate: float = 0.0) -> GenResult:
    """Klibi fal deposuna yükler (SHA önbellekli), upscale eder, sonucu hemen indirir."""
    guard("fal_key")
    client = fal_queue.client()
    url, _, _ = fal_queue.upload_cached(client, path)
    args = {**body, "video_url": url}
    out = fal_queue.run(client, endpoint, args, timeout_s=UPSCALE_TIMEOUT_S, scene_id=scene_id, project_id=project_id,
                        info={"engine": "upscale", "mode": endpoint})
    video = out.result.get("video") or {}
    if not video.get("url"):
        raise fal_queue.FalError(f"fal.ai yanıtında video yok ({out.request_id})")
    local = fal_queue.download(video["url"], ".mp4", video.get("file_size"))
    return GenResult(local_path=local, request_id=out.request_id, cost_usd=estimate, engine="upscale",
                     meta={"endpoint": endpoint})
