"""Hareket testi kapısı: durağan görselde iyi görünen bir varlık hareket halinde bozulabilir.

Varlık kilitlenmeden önce kısa, düşük çözünürlüklü bir klip üretilir; yönetmen panelden izleyip onaylar. Klip her üretim
gibi bütçe rezervasyonundan geçer (ücretsiz modda sanal) ve gerçek anomali tespitinden (siyah, donmuş) geçer. Kusurlu test
onaylanamaz. Test, varlığın sürümüne bağlıdır: referans değişince eski test geçersiz olur.

Ücretli modda (Faz 18) test, varlığın onaylı referans görselinden başlar (MiniMax H3'e ilk kare olarak verilir, Flux
çağrılmaz): sınanan, onaylanan referansın kendisidir.
"""
import logging
import os
import tempfile
from dataclasses import replace

from sqlalchemy import func, select

from app import storage
from app.budget import killswitch
from app.config import settings
from app.core.hashing import seed_for
from app.db import session_scope
from app.engine import references
from app.engine.iteration import make_thumb
from app.engine.providers import GenRequest, TransientError, default_engine, get_provider
from app.style import bible
from app.style.composer import compose_prompt
from app.models import Asset, Variation
from app.quality.anomaly import detect_anomalies

log = logging.getLogger("hellgrind.motion_test")

MOTION_TEST_S = 3.0
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


def compose_for(asset, style):
    """Testin prompt'u da besteciden geçer: varlık, filmdeki ışık ve hareket diliyle sınanır."""
    camera, _ = RENDER[asset.type]
    return compose_prompt({"prompt": f"Motion test: {ACTION_TEXT[asset.type]}", "camera": camera,
                           "characters": [asset.name] if asset.type == "character" else []}, [asset], style)


def create(s, asset: Asset, cfg: dict) -> Variation:
    """Sıradaki testin varyasyon satırı (queued). Her test yeni bir seed alır; varlığın en son testi olarak işaretlenir."""
    n = s.scalar(select(func.count()).select_from(Variation).where(Variation.asset_id == asset.id)) or 0
    style = bible.active(s, asset.project_id)
    composed = compose_for(asset, style)
    v = Variation(asset_id=asset.id, scene_id=None, tier="probe", wave_no=n, engine=default_engine(cfg),   # filmin taslak motoru
                  seed=seed_for(str(asset.id), asset.version, "motion", n), prompt_revision=0, status="queued",
                  params={"kind": "motion_test", "asset_version": asset.version, "resolution": cfg["probe_resolution"],
                          "duration_s": MOTION_TEST_S, "prompt": composed.prompt, "negative": composed.negative,
                          "style_version": style.version if style else None,
                          "lighting": style.lighting_prefix if style else ""})
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
        req = GenRequest(prompt=v.params["prompt"], negative=v.params.get("negative", ""), seed=v.seed,
                         resolution=v.params["resolution"], duration_s=v.params["duration_s"],
                         extra={"emotion": "neutral", "camera": camera, "action": action,
                                "lighting": v.params.get("lighting", ""),
                                "characters": [asset.name] if asset.type == "character" else [],
                                "project_id": project_id,
                                "label": f"{asset.name} · hareket testi #{v.wave_no + 1}"})
        asset_id = str(asset.id)
        engine = v.engine
        source = references.motion_source(asset.references or {})
    if killswitch.is_frozen(project_id):
        _set(variation_id, status="failed", params={**v.params, "purge_reason": "frozen"})
        raise killswitch.ProjectFrozen(project_id)

    provider = get_provider(engine)
    _set(variation_id, status="running")
    result = None
    try:
        if not settings.dry_run and source:                   # ücretli: onaylı referanstan başla (yükleme ücretsiz)
            os.makedirs(settings.work_dir, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                local = storage.download(source, os.path.join(tmp, "start" + os.path.splitext(source)[1]))
                req = replace(req, image_url=provider.upload(local))
        estimate = provider.estimate(req)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with killswitch.spend(project_id, estimate, f"{engine}:{req.resolution}:motion_test",
                                      variation_id=variation_id, details={"attempt": attempt}) as charge:
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
