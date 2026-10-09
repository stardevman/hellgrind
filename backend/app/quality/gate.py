"""Quality Gate: skorlama, eşik ve Rafine Havuz.

Ücretsiz modda CLIP skoru yerine seed'e bağlı tekrarlanabilir, Gauss dağılımlı sahte skor kullanılır; ücretli modda gerçek
skor (app/quality/scorer.py: OpenCLIP stil ve istem uyumu, YuNet + SFace yüz kimliği). Anomali tespiti her iki modda da
gerçektir (FFmpeg). Hangi skorun kullanılacağını `settings.quality_scorer` belirler (auto | dry | real).
"""
import logging
import random
from dataclasses import dataclass, field

from sqlalchemy import select, update

from app import storage
from app.config import settings
from app.db import session_scope
from app.models import Scene, Variation
from app.quality.anomaly import detect_anomalies, detect_frame_anomalies

log = logging.getLogger("hellgrind.gate")


@dataclass(frozen=True)
class Score:
    clip: float
    face: float | None
    composite: float
    anomalies: list[dict]
    passed: bool
    detail: dict = field(default_factory=dict)       # gerçek skorun dökümü (variations.params.score_detail)


def scorer_mode() -> str:
    """"dry" (sahte) ya da "real" (gerçek modeller)."""
    mode = settings.quality_scorer
    if mode == "auto":
        return "dry" if settings.dry_run else "real"
    if mode not in ("dry", "real"):
        raise ValueError(f"Bilinmeyen QUALITY_SCORER: {mode!r} (auto | dry | real)")
    return mode


def ensure_scorer() -> None:
    """Para harcanmadan önce. Sahte skor yalnızca ücretsiz modda geçerlidir: ücretli modda rastgele skorlar ücretli
    klipleri sessizce onaylar ya da reddederdi. Gerçek skorlayıcı kullanılamıyorsa (paket ya da model dosyası yok)
    üretim başlamaz."""
    if scorer_mode() == "dry":
        if not settings.dry_run:
            raise NotImplementedError("Sahte kalite skoru ücretli modda kullanılamaz: QUALITY_SCORER=auto ya da real olmalı.")
        return
    from app.quality import scorer                  # torch ve modeller yalnızca burada yüklenir
    scorer.ensure_ready()


def clip_score_dry(seed: int) -> float:
    rng = random.Random(seed ^ 0x5EED)
    return round(min(max(rng.gauss(settings.dry_run_score_mean, settings.dry_run_score_sd), 0.0), 1.0), 4)


IDENTITY_ANOMALIES = ("no_face", "low_identity", "low_likeness")
CHAIN_ANOMALIES = ("continuity_jump", "transition_jump")


def identity_anomalies(detail: dict, target, cfg: dict) -> list[dict]:
    """Kimlik kapısı (videoda ve karakter referansından çizilen ilk karede).
    İnsan karakter (target.faces) ve yüz bekleniyorsa: yüzü hiç görünmeyen aday (`no_face`) ya da bir karakterin kimlik
    skoru (0-1; SFace kosinüsünün aynı kişi eşiği çevresinde) `identity_threshold`'un altında kalan aday (`low_identity`)
    geçemez. Yüz göründü ama hepsi küçükse (kısa kenar < scorer.MEASURE_PX) kimlik ölçülemedi sayılır, eleme yok.
    Sırt çekimi, silüet, uzak ya da geniş aksiyon planı: face_expected false.
    İnsan olmayan karakter (target.looks: robot, hayvan): yüz aranmaz; karelerle referans arasındaki OpenCLIP görüntü
    benzerliği `look_threshold`'un altındaysa `low_likeness`."""
    if target is None or not detail:
        return []
    out = []
    if getattr(target, "faces", ()) and getattr(target, "face_expected", True) and "faces_seen" in detail:
        threshold = float(cfg.get("identity_threshold", 0.6))
        if not detail.get("faces_seen"):
            out.append({"type": "no_face", "faces_seen": 0, "characters": [name for name, _ in target.faces]})
        elif detail.get("faces_measured", detail["faces_seen"]):
            identity = detail.get("identity") or {}
            low = {name: identity.get(name) for name, _ in target.faces if (identity.get(name) or 0.0) < threshold}
            if low:
                out.append({"type": "low_identity", "identity": low, "threshold": threshold})
    if getattr(target, "looks", ()) and "look" in detail:
        threshold = float(cfg.get("look_threshold", 0.70))
        look = detail.get("look") or {}
        low = {name: look.get(name) for name, _ in target.looks if (look.get(name) or 0.0) < threshold}
        if low:
            out.append({"type": "low_likeness", "look": low, "threshold": threshold})
        elif "look_dino" in detail:               # DINOv2 (kırpımlar)
            threshold = float(cfg.get("look_dino_threshold", 0.78))
            dino = detail.get("look_dino") or {}
            low = {name: dino.get(name) for name, _ in target.looks if name in dino and dino[name] < threshold}
            if low:
                out.append({"type": "low_likeness", "look_dino": low, "threshold": threshold})
    return out


def identity_review(detail: dict, target, cfg: dict) -> list[str]:
    """Sınırda kalan benzerlik (eşiğin üstünde ama `look_review_margin` içinde): elenmez, "inceleme önerilir" notuyla
    geçer. Kıvılcım zincir koşusu: gözle doğru Bolt kareleri OpenCLIP 0,71-0,80 / DINOv2-kırpım 0,80-0,88 aldı; iki
    antenli eski kare 0,76 / 0,82 almıştı. Bu ölçümler ayrıntıyı (anten sayısı) güvenilir ayıramıyor: sınırdakiler
    yönetmene gösterilir."""
    if target is None or not detail or not getattr(target, "looks", ()):
        return []
    margin = float(cfg.get("look_review_margin", 0.05))
    notes = []
    for key, name_tr, threshold in (("look", "OpenCLIP", float(cfg.get("look_threshold", 0.70))),
                                    ("look_dino", "DINOv2", float(cfg.get("look_dino_threshold", 0.78)))):
        for name, _ in target.looks:
            value = (detail.get(key) or {}).get(name)
            if value is not None and threshold <= value < threshold + margin:
                notes.append(f"{name} benzerliği sınırda ({name_tr} {value:.2f}, eşik {threshold:.2f})".replace(".", ","))
    return notes


def _judge(measured, seed: int, anomalies: list[dict], cfg: dict, target=None, check_identity: bool = False) -> Score:
    """Birleşik skor + anomaliler. Eşik altı birleşik skor da `low_clip` anomalisi sayılır; geçmek = anomali yok.
    measured: gerçek ölçüm (scorer.Result), okunamayan medyada None; sahte modda kullanılmaz."""
    if scorer_mode() == "dry":
        clip = clip_score_dry(seed)
        face, composite, detail = None, clip, {}
    elif measured is None:
        clip, face, composite, detail = 0.0, None, 0.0, {"scorer": "real", "error": "kare okunamadı"}
    else:
        clip, face, composite, detail = measured.clip, measured.face, measured.composite, measured.detail
    if composite < cfg["clip_threshold"]:
        anomalies.append({"type": "low_clip", "score": composite, "threshold": cfg["clip_threshold"]})
    if check_identity:
        anomalies.extend(identity_anomalies(detail, target, cfg))
        if not anomalies and (review := identity_review(detail, target, cfg)):
            detail = {**detail, "review": review}             # geçer, ama yönetmen bakmalı
    return Score(clip=clip, face=face, composite=composite, anomalies=anomalies, passed=not anomalies, detail=detail)


def score_video(path: str, seed: int, cfg: dict, target=None) -> Score:
    """target: scorer.Target (stil metni, çekim tarifi, karakter referansları); yalnızca gerçek skorda kullanılır."""
    anomalies = detect_anomalies(path)
    measured = None
    if scorer_mode() == "real":
        from app.quality import scorer
        measured = scorer.score_video(path, target or scorer.Target())
        if measured is not None and getattr(target, "next_frame", None):   # FL2VA last_frame: son kare = K(n+1)
            value = scorer.transition(path, target.next_frame)
            if value is not None:
                measured.detail["transition"] = value
                threshold = float(cfg.get("transition_threshold", 0.8))
                if value < threshold:
                    anomalies.append({"type": "transition_jump", "score": value, "threshold": threshold})
    return _judge(measured, seed, anomalies, cfg, target, check_identity=True)


def score_frame(path: str, seed: int, cfg: dict, target=None, check_identity: bool = False, previous: str | None = None,
                transition: str | None = None) -> Score:
    """Önce görsel (Faz 18): ilk kare videodan önce aynı eşikle ve aynı skorlayıcıyla puanlanır; geçemeyen kare için
    video hiç üretilmez. check_identity: kare karakter referansından çizildi (Qwen-Image-Edit): karedeki yüz referansa
    SFace ile ≥ identity_threshold benzemeli (metinden çizilen karede kimlik aranmaz)."""
    anomalies = detect_frame_anomalies(path)
    measured = None
    if scorer_mode() == "real":
        from app.quality import scorer
        measured = scorer.score_image(path, target or scorer.Target())
        if measured is not None and previous:          # zincir karesi: önceki çekimin karesiyle aynı mekân mı
            value = scorer.continuity(path, previous)
            if value is not None:
                measured.detail["continuity"] = value
                threshold = float(cfg.get("cut_continuity_threshold" if transition == "cut" else "continuity_threshold",
                                          0.6 if transition == "cut" else 0.8))
                if value < threshold:
                    anomalies.append({"type": "continuity_jump", "score": value, "threshold": threshold,
                                      "transition": transition or "continuous"})
    return _judge(measured, seed, anomalies, cfg, target, check_identity=check_identity)


# ---------- Rafine Havuz ----------

def excellent_count(scene_id, run_id: str, threshold: float) -> int:
    """Bu çalıştırmanın tam turunda eşiği geçip `threshold` üstünde skor alanların sayısı (erken durma için)."""
    with session_scope() as s:
        return len(s.scalars(select(Variation.id).where(
            Variation.scene_id == scene_id, Variation.tier == "full", Variation.status == "passed",
            Variation.params["run"].astext == run_id, Variation.composite_score >= threshold)).all())


def run_results(scene_id, run_id: str) -> list:
    """Bu çalıştırmanın tam tur adayları (needs_review gerekçesi için): geçti mi ve anomalileri."""
    from types import SimpleNamespace
    with session_scope() as s:
        rows = s.execute(select(Variation.status, Variation.anomalies).where(
            Variation.scene_id == scene_id, Variation.tier == "full", Variation.params["run"].astext == run_id)).all()
    return [SimpleNamespace(passed=st in ("passed", "refined"), anomalies=list(an or [])) for st, an in rows]


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


def finalize_draft(scene_id, variation_id: str, scene_update: dict) -> None:
    """Taslak geçiş: verilen (test turunda geçen ilk) varyasyon tek başına rank 1 olur. Önceki havuz emekli edilir;
    bu çalıştırmanın geçen diğer test klipleri `passed` olarak yerinde kalır ki iyileştirme geçişi yeniden kullansın."""
    with session_scope() as s:
        s.execute(update(Variation).where(Variation.scene_id == scene_id).values(refined_rank=None))
        s.flush()
        retire = []
        for v in s.scalars(select(Variation).where(Variation.scene_id == scene_id, Variation.status == "refined")):
            if str(v.id) != str(variation_id):
                retire.append((v.id, v.video_key, "superseded"))
                v.status = "purged"
                v.params = {**(v.params or {}), "purge_reason": "superseded"}
        winner = s.get(Variation, variation_id)
        winner.refined_rank, winner.status = 1, "refined"
        scene = s.get(Scene, scene_id)
        for field, value in scene_update.items():
            setattr(scene, field, value)
    purge_files(retire)


def set_single_winner(scene_id, variation_id: str, scene_update: dict) -> None:
    """Tek bir varyasyonu rank 1 yapar (dış çekim, previs rehberi). Önceki havuz silinmez: `passed` olarak geri çekilir;
    yöntem değişip üretime dönülürse klipler yeniden kullanılabilir."""
    with session_scope() as s:
        s.execute(update(Variation).where(Variation.scene_id == scene_id).values(refined_rank=None))
        s.flush()
        for v in s.scalars(select(Variation).where(Variation.scene_id == scene_id, Variation.status == "refined")):
            if str(v.id) != str(variation_id):
                v.status = "passed"
        winner = s.get(Variation, variation_id)
        winner.refined_rank, winner.status = 1, "refined"
        scene = s.get(Scene, scene_id)
        for field, value in scene_update.items():
            setattr(scene, field, value)


def purge_files(items: list[tuple]) -> None:
    """[(variation_id, video_key, neden)]: dosyayı karantinaya taşır (hard modda siler), satırı günceller."""
    for vid, key, reason in items:
        quarantined = storage.purge(key) if key else None
        with session_scope() as s:
            v = s.get(Variation, vid)
            v.video_key = None
            v.params = {**(v.params or {}), "quarantine_key": quarantined, "purge_reason": reason}
