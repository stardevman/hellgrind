"""Hareket testi kapısı: durağan görselde iyi görünen bir varlık hareket halinde bozulabilir.

Varlık kilitlenmeden önce kısa, düşük çözünürlüklü bir klip üretilir; yönetmen panelden izleyip onaylar. Klip her üretim
gibi bütçe rezervasyonundan geçer (ücretsiz modda sanal) ve gerçek anomali tespitinden (siyah, donmuş) geçer. Kusurlu test
onaylanamaz. Test, varlığın sürümüne bağlıdır: referans değişince eski test geçersiz olur.
"""
import logging
import os

from sqlalchemy import func, select

from app import storage
from app.budget import killswitch
from app.core.hashing import seed_for
from app.db import session_scope
from app.engine.iteration import make_thumb
from app.engine.providers import GenRequest, TransientError, get_provider
from app.hermes import lint
from app.models import Asset, Variation
from app.quality.anomaly import detect_anomalies

log = logging.getLogger("hellgrind.motion_test")

MOTION_TEST_S = 3.0
ENGINE = "fal_wan"               # en ucuz motor: test yalnızca hareket altındaki tutarlılığı sınar
MAX_ATTEMPTS = 3
RENDER = {   # tür -> (kamera, aksiyon): karakter koşar, mekânda vinç, nesne etrafında yörünge
    "character": ({"shot": "medium", "movement": "orbit"}, 0.8),
    "location": ({"shot": "wide", "movement": "crane"}, 0.0),
    "prop": ({"shot": "close_up", "movement": "orbit"}, 0.0),
}
ACTION_TEXT = {
    "character": "walking, turning and running, full body in motion",
    "location": "slow crane move through the space",
    "prop": "object rotating and moving through the frame",
}


def prompt_for(asset) -> str:
    base = f"{asset.name}. {asset.description}" if asset.description else asset.name
    prompt, _ = lint.constrain_prompt(f"Motion test: {base}. {ACTION_TEXT[asset.type]}",
                                      "", [asset.name] if asset.type == "character" else [])
    return prompt


def create(s, asset: Asset, cfg: dict) -> Variation:
    """Sıradaki testin varyasyon satırı (queued). Her test yeni bir seed alır; varlığın en son testi olarak işaretlenir."""
    n = s.scalar(select(func.count()).select_from(Variation).where(Variation.asset_id == asset.id)) or 0
    v = Variation(asset_id=asset.id, scene_id=None, tier="probe", wave_no=n, engine=ENGINE,
                  seed=seed_for(str(asset.id), asset.version, "motion", n), prompt_revision=0, status="queued",
                  params={"kind": "motion_test", "asset_version": asset.version, "resolution": cfg["probe_resolution"],
                          "duration_s": MOTION_TEST_S, "prompt": prompt_for(asset)})
    s.add(v)
    s.flush()
    asset.motion_test_variation_id = v.id
    return v


def _set(vid, **fields) -> None:
    with session_scope() as s:
        v = s.get(Variation, vid)
        for key, value in fields.items():
            setattr(v, key, value)


def run(project_id: str, variation_id: str) -> str:
    """Testi üretir, depoya yükler ve anomaliye bakar. Dönen: "passed", "rejected" ya da "failed"."""
    with session_scope() as s:
        v = s.get(Variation, variation_id)
        asset = s.get(Asset, v.asset_id)
        if v.status not in ("queued", "running"):
            return v.status                                   # tekrar teslim edilen görev: iş zaten bitti
        camera, action = RENDER[asset.type]
        req = GenRequest(prompt=v.params["prompt"], seed=v.seed, resolution=v.params["resolution"],
                         duration_s=v.params["duration_s"],
                         extra={"emotion": "neutral", "camera": camera, "action": action,
                                "characters": [asset.name] if asset.type == "character" else [],
                                "label": f"{asset.name} · hareket testi #{v.wave_no + 1}"})
        asset_id = str(asset.id)
    if killswitch.is_frozen(project_id):
        _set(variation_id, status="failed", params={**v.params, "purge_reason": "frozen"})
        raise killswitch.ProjectFrozen(project_id)

    provider = get_provider(ENGINE)
    estimate = provider.estimate(req)
    _set(variation_id, status="running")
    result = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with killswitch.spend(project_id, estimate, f"{ENGINE}:{req.resolution}:motion_test",
                                      variation_id=variation_id) as charge:
                    result = provider.generate(req)
                    charge.actual = result.cost_usd
                break
            except TransientError as e:
                log.warning("hareket testi geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
        if result is None:
            _set(variation_id, status="failed")
            return "failed"
        try:
            key = f"motion/{asset_id}/{variation_id}.mp4"
            storage.put_file(result.local_path, key)
            thumb = make_thumb(result.local_path, f"thumbs/assets/{asset_id}/{variation_id}.jpg")
            anomalies = detect_anomalies(result.local_path)
        finally:
            os.remove(result.local_path)
    except BaseException:
        _set(variation_id, status="failed")
        raise
    status = "rejected" if anomalies else "passed"
    _set(variation_id, status=status, video_key=key, keyframe_key=thumb, anomalies=anomalies,
         external_request_id=result.request_id, cost_usd=result.cost_usd,
         params={**v.params, "provider_meta": result.meta})
    killswitch.event(project_id, "motion_test", asset_id=asset_id, ok=not anomalies,
                     anomalies=sorted({a["type"] for a in anomalies}))
    return status
