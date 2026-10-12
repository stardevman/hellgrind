"""Referans modunda bir parçanın üretimi (engine/scene_parts.py): tek istek, video kapıları, çekimlere kesme.

Görev çekim başınadır; parçanın çekimlerinden hangisinin görevi önce çalışırsa parça kilidini alır ve üretir, diğerleri
kilidi bekler ve hazır klibi kullanır (içerik-adresli: parçanın özeti aynıysa yeniden üretilmez, yeniden ücretlenmez).
Üretim bitince parçanın BÜTÜN çekimleri kendi kesimlerini kazanan olarak alır; görevi olmayan (bitmiş) kardeş çekimlerin
ses ve montaj aşamaları kuyruğa verilir.

Önceki klibin son karesi (continuous geçiş) yalnızca ffmpeg ile çıkarılır. Önceki çekim referans yoluyla üretiliyor ama
henüz üretilmediyse önce onun parçası üretilir (sırayla; kuyruğun boşta beklemesi yok).

Kalite: parça klibi videonun kendisi üzerinde ölçülür (gate.score_video, video_gates): 0,5 sn'de bir örnek karede kimlik
(insan SFace, diğerleri OpenCLIP), kopya karakter sayımı, ölçek (gerçek boyut); kalırsa `fal_clip_attempts` kadar yeni
tohumla yeniden üretilir, sonra yönetmene bırakılır. Renk sürekliliği ve otomatik eşleme montajdan sonra (tasks.continuity).
"""
import logging
import os
import subprocess
import tempfile
import uuid

from sqlalchemy import select

from app import storage
from app.budget import killswitch
from app.config import project_settings, settings
from app.core.hashing import seed_for, visual_fingerprint
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine import fal_plan, scene_parts
from app.engine.providers import GenRequest, ProviderError, TransientError, get_provider
from app.models import Project, Scene, Variation

log = logging.getLogger("hellgrind.part_render")

LOCK = "hg:part_lock:{}"
FAILED = "hg:part_failed:{}"   # planın özeti -> 4xx mesajı: kilidi bekleyen kardeş görevler parçayı yeniden üretmez
FAILED_TTL_S = 24 * 3600
LOCK_TTL_S = 3 * 3600
LOCAL_RETRIES = 3              # yerel/geçici hata: aynı deneme yeniden (fal kuyruğu kendi yeniden denemesini yapar)
MAX_DEPTH = 20                 # önceki parçaların zinciri


class PartStop(Exception):
    """Parça üretilemez: (durum, Türkçe gerekçe). Çekim bu durumda kalır, hiçbir şey harcanmaz."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def render_shot(project_id: str, scene_id: str) -> str:
    """Çekimin parçasını hazırlar (gerekirse üretir) ve kazananları yazar. Dönen: refined | needs_review | plan_review |
    blocked."""
    try:
        ok = ensure_part(project_id, scene_id)
    except PartStop as e:
        advance_scene(project_id, scene_id, status=e.status, last_error=e.reason)
        killswitch.event(project_id, "part_stopped", scene_id=scene_id, status=e.status, reason=e.reason[:200])
        return e.status
    except ProviderError as e:                         # ör. fal.ai 422: yeniden denenmez, mesaj olduğu gibi
        advance_scene(project_id, scene_id, status="failed", last_error=str(e))
        killswitch.event(project_id, "provider_rejected", scene_id=scene_id, error=str(e)[:300])
        return "failed"
    return "refined" if ok else "needs_review"


def clear_failure(plan_hash: str) -> None:
    """Yönetmen yeniden üretim istedi: parçanın 4xx işareti kalkar (aynı plan yeniden denenebilir)."""
    killswitch.r().delete(FAILED.format(plan_hash))


def _checks(s, sc, plan, cfg) -> None:
    if plan.missing:
        raise PartStop("blocked", "Eksik referans: " + ", ".join(m["name"] for m in plan.missing) +
                       ". Varlıklar sayfasından referans görselini yükleyin; görseli olmayan varlıkla video üretilmez.")
    if scene_parts.plan_required(cfg) and not plan.approved():
        raise PartStop("plan_review", f"Plan onayı bekliyor ({', '.join(x.scene_code for x in plan.shots)}): kuru "
                                      "çalıştırma planını denetçide inceleyip onaylayın.")


def ensure_part(project_id: str, scene_id: str, depth: int = 0, caller: str | None = None) -> bool:
    """Parçanın geçen klibi var mı (yoksa üretir); kazananlar yazılır. Geçen klip yoksa False."""
    if depth > MAX_DEPTH:
        raise PartStop("needs_review", "Süreklilik zinciri çok uzun: önceki parçalar üretilemedi.")
    caller = caller or scene_id
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        cfg = project_settings(s.get(Project, project_id).settings)
        plan = scene_parts.plan_part(s, sc)
        _checks(s, sc, plan, cfg)
        cont = dict(plan.continuity)
        previous = None
        if cont.get("previous_active"):
            prev = s.get(Scene, cont["previous_id"])
            # önceki çekimin geçerli kazananı varsa onun son karesi; kazananı yoksa ya da yeniden üretim için
            # işaretliyse (görsel özeti sıfırlandı, ör. "Bu çekimi ayrı üret") önce o parça üretilir
            if not cont.get("winner_id") or prev.visual_hash is None:
                previous = (cont["previous_id"], cont["previous"])
    frame_key = None
    if cont.get("expected"):
        if previous is not None:                       # önceki parça güncel planıyla hazır mı (değilse önce o; sırayla)
            try:
                ok = ensure_part(project_id, previous[0], depth + 1, caller=caller)
            except PartStop as e:
                raise PartStop(e.status if e.status == "plan_review" else "needs_review",
                               f"Önceki parça ({previous[1]}) hazır değil: {e.reason}") from e
            if not ok:
                raise PartStop("needs_review", f"Önceki parça ({previous[1]}) kalite kapısından geçmedi: son karesi "
                                               "olmadan bu parça sürekli başlayamaz. Önce onu düzeltin.")
        frame_key = _last_frame(project_id, scene_id)
    with session_scope() as s:
        plan = scene_parts.plan_part(s, s.get(Scene, scene_id), frame_key=frame_key)
        snapshot = _snapshot(s, plan, cfg)
    lock = killswitch.r().lock(LOCK.format(plan.part_hash), timeout=LOCK_TTL_S, blocking_timeout=LOCK_TTL_S)
    if not lock.acquire():
        raise PartStop("needs_review", "Parça kilidi alınamadı (başka bir görev üretiyor olabilir); yeniden deneyin.")
    try:
        failed = killswitch.r().get(FAILED.format(plan.plan_hash))
        if failed:                                     # başka bir çekimin görevi 4xx aldı: yeniden deneme yok
            raise PartStop("failed", failed.decode() if isinstance(failed, bytes) else failed)
        winners = _passed(plan.part_hash)
        if not winners:
            winners = _generate(project_id, plan, snapshot, cfg, caller)
    finally:
        try:
            lock.release()
        except Exception:                               # noqa: BLE001 — süresi dolmuş kilit
            pass
    if winners and _already_final(winners):           # önceki parçanın hazır kazananları: yeniden yazılmaz
        return True
    if not winners:
        for sid in snapshot["ids"]:
            advance_scene(project_id, sid, status="needs_review",
                          last_error=f"{fal_plan.ENGINE_TR[plan.engine]}: parça ({', '.join(snapshot['codes'])}) "
                                     f"{snapshot['attempts']} denemede kalite kapısından geçmedi. Adaylardan birini "
                                     "yönetmen seçimiyle kabul edin ya da yeniden üretin.")
        return False
    _finalize(project_id, plan, snapshot, winners, caller)
    return True


def _already_final(winners: dict[str, str]) -> bool:
    with session_scope() as s:
        return all((v := s.get(Variation, vid)) is not None and v.refined_rank == 1 for vid in winners.values())


def _last_frame(project_id: str, scene_id: str) -> str | None:
    """Parçanın ilk çekiminden önceki çekimin kazanan klibinin son karesi (ffmpeg; seam/{varyasyon}.jpg)."""
    from app.engine.iteration import seam_frame
    with session_scope() as s:
        plan = scene_parts.plan_part(s, s.get(Scene, scene_id))
        winner_id = plan.continuity.get("winner_id")
        if not winner_id:
            return None
        winner = s.get(Variation, winner_id)
        s.expunge(winner)
    return seam_frame(winner)


def _snapshot(s, plan, cfg) -> dict:
    from app.engine.storyboard import scale_expectations
    from app.quality import scorer
    from app.style import bible
    style = bible.active(s, plan.leader.project_id)
    chars, assets = scene_parts.union(s, plan.shots)
    version = style.version if style else None
    return {"ids": [str(x.id) for x in plan.shots], "codes": [x.scene_code for x in plan.shots],
            "leader": str(plan.leader.id), "timeline": list(plan.timeline),
            "fps": {str(x.id): visual_fingerprint(x, load_characters(s, x.id), load_assets(s, x.id), version)
                    for x in plan.shots},
            "target": scorer.target_for(plan.leader, chars, style), "scales": scale_expectations(chars, assets),
            "tier": fal_plan.tier_of(plan.engine, cfg), "attempts": max(1, int(cfg.get("fal_clip_attempts", 2))),
            "render": {"emotion": plan.leader.emotion, "camera": dict(plan.leader.camera or {}),
                       "characters": [ch.name for ch in chars], "action": plan.leader.action_complexity,
                       "lighting": style.lighting_prefix if style else ""},
            "statuses": {str(x.id): x.status for x in plan.shots}}


def _passed(part_hash: str) -> dict[str, str]:
    """Bu parça özetinin geçen kesimleri: {çekim: varyasyon} (en son geçen deneme)."""
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.params["part"].astext == part_hash,
                                                 Variation.status.in_(("passed", "refined")))
                         .order_by(Variation.created_at)).all()
        out = {}
        for v in rows:
            out[str(v.scene_id)] = str(v.id)
        return out


def _tried(part_hash: str) -> set[int]:
    with session_scope() as s:
        return {int((v.params or {}).get("attempt", -1)) for v in s.scalars(select(Variation).where(
            Variation.params["part"].astext == part_hash))}


def _generate(project_id: str, plan, snap: dict, cfg: dict, caller: str | None = None) -> dict[str, str]:
    """Parçayı en çok `attempts` kez üretir; her denemenin klibi çekimlere kesilir ve kaydedilir. Geçen ilk denemenin
    kesimleri döner."""
    from app.quality import gate
    provider = get_provider(plan.engine)
    tried = _tried(plan.part_hash)
    for attempt in range(snap["attempts"]):
        if attempt in tried:                          # bu deneme yapılmış ve kalmış: yeniden ödenmez
            continue
        if killswitch.is_frozen(project_id):
            raise killswitch.ProjectFrozen(project_id)
        advance_scene(project_id, caller or snap["leader"], status="generating")   # kardeşler kendi durumunda kalır
        seed = seed_for(plan.part_hash, attempt)
        req = _request(plan, snap, seed, provider)
        result = None
        for retry in range(1, LOCAL_RETRIES + 1):
            try:
                with killswitch.spend(project_id, provider.estimate(req), f"{plan.engine}:{plan.resolution}",
                                      scene_id=snap["leader"],
                                      details={"attempt": attempt, "retry": retry, "part": snap["codes"]}) as charge:
                    result = provider.generate(req)
                    charge.actual = result.cost_usd
                break
            except TransientError as e:
                log.warning("parça geçici hata (deneme %d, %d/%d): %s", attempt, retry, LOCAL_RETRIES, e)
            except ProviderError as e:                 # 4xx: parçanın bütün çekimleri hemen failed, yeniden deneme yok
                killswitch.r().set(FAILED.format(plan.plan_hash), str(e), ex=FAILED_TTL_S)
                for sid in snap["ids"]:
                    advance_scene(project_id, sid, status="failed", last_error=str(e))
                raise
        if result is None:
            continue
        clip_key = f"parts/{plan.part_hash}/{attempt}.mp4"
        try:
            storage.put_file(result.local_path, clip_key)
            score = gate.score_video(result.local_path, seed, cfg, snap["target"], video_gates_on=True,
                                     scales=snap["scales"])
            rows = _cut(result.local_path, plan, snap, seed, attempt, clip_key, score, result)
        finally:
            if os.path.exists(result.local_path):
                os.remove(result.local_path)
        killswitch.event(project_id, "part_clip", scene_id=snap["leader"], engine=plan.engine, attempt=attempt + 1,
                         passed=score.passed, shots=snap["codes"])
        if score.passed:
            return rows
    return _passed(plan.part_hash)


def _request(plan, snap: dict, seed: int, provider) -> GenRequest:
    """Gönderilecek istek: kuru çalıştırmanın gövdesiyle aynı alanlar. Seedance: son kare image_urls'ün başında;
    H3 Max: image_url (ücretli modda fal deposuna yüklenir)."""
    frame = plan.continuity.get("frame_key")
    refs = [r.key for r in plan.refs]
    image_url = None
    if frame and plan.engine == "seedance25":
        refs = [frame, *refs]
    elif frame and hasattr(provider, "upload"):
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            image_url = provider.upload(storage.download(frame, os.path.join(tmp, "first.jpg")))
    images = len(refs) + (1 if plan.engine == "h3max" and frame else 0)
    return GenRequest(prompt=plan.prompt, seed=seed, resolution=plan.resolution, duration_s=plan.duration_s,
                      image_url=image_url,
                      extra={**snap["render"], "label": f"{'+'.join(snap['codes'])}  {plan.engine}",
                             "fal_mode": "ref", "fal_refs": refs, "fal_ref_names": [r.name for r in plan.refs],
                             "fal_images": images, "fal_audio": plan.audio, "fal_draft": plan.draft,
                             "scene_id": snap["leader"],
                             "project_id": str(plan.leader.project_id)})


CUT_THRESHOLD = 0.3            # FFmpeg sahne değişimi skoru (0-1): bunun üstü kesme sayılır
CUT_TOLERANCE = 0.4            # planlı sınıra en çok bu oranda (komşu çekimlerin kısasının) uzak kesme eşlenir


def detect_cuts(path: str, threshold: float = CUT_THRESHOLD) -> list[float]:
    """Klipteki gerçek kesmeler (sn): FFmpeg `select='gt(scene,eşik)'` + showinfo (PySceneDetect ContentDetector'ın
    karşılığı, ek bağımlılık yok)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vf", f"select='gt(scene,{threshold})',showinfo",
                           "-an", "-f", "null", "-"], capture_output=True, text=True)
    import re
    return [round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def boundaries(planned: list[tuple], duration: float, cuts: list[float]) -> tuple[list[tuple], list[str], str | None]:
    """Senaryodaki çekim sınırlarını klipteki gerçek kesmelere eşler. planned: [(kod, başlangıç, bitiş)] (istenen süreye
    göre); duration: klibin gerçek süresi (H3 Max ~0,7 sn uzun olabilir: planlı sınırlar orana göre ölçeklenir).
    Her iç sınır için tolerans içindeki en yakın kesme alınır (sıra korunur); bulunamazsa ölçeklenmiş planlı sınır.
    (segmentler, sınır kaynakları ["cut" | "planned"], uyarı)."""
    total = planned[-1][2] if planned else 0.0
    scale = (duration / total) if total and duration else 1.0
    inner = [round(end * scale, 3) for _, _, end in planned[:-1]]
    lengths = [(end - start) * scale for _, start, end in planned]
    chosen, sources, used = [], [], set()
    lower = 0.0
    for i, b in enumerate(inner):
        tol = CUT_TOLERANCE * min(lengths[i], lengths[i + 1])
        options = [c for c in cuts if c > lower + 0.2 and abs(c - b) <= tol and c not in used]
        if options:
            c = min(options, key=lambda c: abs(c - b))
            used.add(c)
            chosen.append(c)
            sources.append("cut")
        else:
            chosen.append(b)
            sources.append("planned")
        lower = chosen[-1]
    edges = [0.0, *chosen, round(duration or total, 3)]
    segments = [(code, edges[i], edges[i + 1]) for i, (code, _, _) in enumerate(planned)]
    warning = None
    if planned and len(planned) > 1 and (len(cuts) != len(inner) or "planned" in sources):
        warning = (f"Klipte {len(cuts)} kesme bulundu, senaryoda {len(inner)} çekim sınırı var: "
                   + ", ".join(f"{planned[i + 1][0]} {'kesmeden' if s == 'cut' else 'en yakın planlı sınırdan'} "
                               f"({chosen[i]:.2f} sn)".replace(".", ",") for i, s in enumerate(sources))
                   + ". Bölmeyi Kurgu'da kontrol edin.")
    return segments, sources, warning


def _cut(path: str, plan, snap: dict, seed: int, attempt: int, clip_key: str, score, result) -> dict[str, str]:
    """Parça klibini klipteki GERÇEK kesmelerden keser (sahne değişimi tespiti; sabit saniyeden değil): kesmeler
    senaryodaki çekim sırasına eşlenir, sayı uymazsa en yakın planlı sınır kullanılır ve uyarı yazılır. Her çekime bir
    varyasyon (geçen: passed, kalan: karantina)."""
    from app.engine.finishing import probe as probe_media
    from app.engine.iteration import make_thumb
    duration = probe_media(path)["duration_s"] or plan.duration_s
    total = max(duration, 1e-6)
    cuts = detect_cuts(path) if len(snap["timeline"]) > 1 else []
    segments, sources, split_warning = boundaries(snap["timeline"], duration, cuts)
    out = {}
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        for i, ((code, start, end), sid) in enumerate(zip(segments, snap["ids"])):
            vid = uuid.uuid4()
            seg = os.path.join(tmp, f"{code}.mp4")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", path, "-t", f"{end - start:.3f}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-c:a", "aac",
                            "-movflags", "+faststart", seg], check=True, capture_output=True)
            key = f"variations/{sid}/{vid}.mp4"
            storage.put_file(seg, key)
            thumb = make_thumb(seg, f"thumbs/{sid}/{vid}.jpg")
            share = round(float(result.cost_usd) * (end - start) / total, 4)
            params = {"part": plan.part_hash, "part_codes": snap["codes"], "part_clip": clip_key, "attempt": attempt,
                      "request_id": result.request_id,
                      "segment": [start, end], "segment_planned": [snap["timeline"][i][1], snap["timeline"][i][2]],
                      "split": {"cuts": cuts, "sources": sources, "warning": split_warning},
                      "resolution": plan.resolution, "duration_s": end - start,
                      "prompt": plan.prompt, "input_mode": "reference", "render_mode": settings.scene_render_mode,
                      "references": [{"image": r.number, "name": r.name, "kind": r.kind} for r in plan.refs],
                      "continuity": {k: plan.continuity.get(k) for k in ("transition", "previous", "frame_key")},
                      "part_cost_usd": float(result.cost_usd), "provider_meta": result.meta,
                      **({"draft_id": (result.meta or {}).get("draft_id") or
                          (f"local-draft-{plan.part_hash[:12]}-{attempt}" if settings.dry_run else None)}
                         if plan.draft else {}),
                      **({"score_detail": score.detail} if score.detail else {})}
            if not score.passed:
                params.update(purge_reason="quality_gate", quarantine_key=storage.purge(key))
            with session_scope() as s:
                s.add(Variation(id=vid, scene_id=sid, tier=snap["tier"], wave_no=0, engine=plan.engine, seed=seed,
                                prompt_revision=0, status="passed" if score.passed else "purged",
                                video_key=key if score.passed else None, keyframe_key=thumb,
                                # istek kimliği benzersizdir: parçanın ilk çekiminde (bedeli işlenen), diğerlerinde params
                                external_request_id=result.request_id if sid == snap["leader"] else None,
                                cost_usd=share, clip_score=score.clip,
                                face_score=score.face, composite_score=score.composite, anomalies=score.anomalies,
                                params=params))
            out[sid] = str(vid)
    return out


def _finalize(project_id: str, plan, snap: dict, winners: dict[str, str], caller: str) -> None:
    """Parçanın bütün çekimlerine kendi kesimini kazanan yazar; görevi olmayan (bitmiş) kardeşlerin ses ve montajı
    kuyruğa verilir."""
    from app.quality import gate
    with session_scope() as s:                        # kazanan yazılmadan önceki durum: kuyrukta kendi görevi olan
        current = {sid: s.get(Scene, sid).status for sid in snap["ids"]}   # kardeş "pending"dir
    for sid in snap["ids"]:
        vid = winners.get(sid)
        if vid is None:
            continue
        gate.set_single_winner(sid, vid, {
            "status": "refined", "visual_hash": snap["fps"][sid], "lipsync_video_key": None, "final_video_key": None,
            "last_error": None, "cut_tier": "final" if snap["tier"] == "full" else "draft", "upgrade_requested": False})
    if plan.draft:                                     # Seedance taslağı: draft_id ve 7 günlük geçerlilik sahnelere
        from app.engine.finishing import remember_draft
        remember_draft(snap["ids"], snap["codes"], winners, plan.part_hash, plan.duration_s)
    from app.engine.finishing import _reset_auto_grade
    with session_scope() as s:                        # yeni klipler: renk eşlemesi montajdan sonra yeniden
        for sid in winners:
            _reset_auto_grade(s, s.get(Scene, sid))
    killswitch.event(project_id, "refined", scene_id=caller, pool=1, engine=plan.engine, part=snap["codes"])
    working = ("pending", "storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing")
    for sid in snap["ids"]:
        if sid == caller or current.get(sid) in working:
            continue                                   # kendi görevi zinciri sürdürür
        try:
            from app.api.plan import plan_and_dispatch
            with session_scope() as s:
                plan_and_dispatch(s, s.get(Scene, sid))
        except Exception as e:                         # noqa: BLE001 — kardeşin sesi elle de başlatılabilir
            log.warning("kardeş çekimin ses/montajı kuyruğa verilemedi (%s): %s", sid, e)
