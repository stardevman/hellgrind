"""Kürasyon kayıtları (Faz 17, değişmez kural 7): yönetmenin her kararı kaydedilir.

Durum değiştiren her API ucu `@curated(eylem)` ile işaretlenir; başarılı her istek bir `curation_events` satırı yazar.
Payload: istek (gövde ve sorgu değerleri), yanıtın özeti ve kalite modeli eğitimi için bağlam (varyasyonun skoru, sırası,
anomalileri; sahnenin durumu, yöntemi, kesim katmanı). Eylem adı sabit bir dize ya da istekten ve yanıttan türeyen bir
fonksiyondur (ör. kare onayı: board_approve / board_reject / board_reset).

Kayıt, kararın kendisinden sonra ayrı bir işlemde yazılır. Yazılamazsa karar geri alınmaz ama hata günlüğe ERROR
olarak düşer.
"""
import functools
import inspect
import logging
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.db import session_scope
from app.models import Asset, CurationEvent, Scene, Sequence, TimelineTrack, Variation

log = logging.getLogger("hellgrind.curation")

# Spesifikasyonun adlandırdığı eylemler (panelde ve dışa aktarımda bu adlarla) ve diğer panel eylemleri.
ACTIONS_TR = {
    "promote": "Rafine havuzdan seçim", "upgrade_request": "İyileştirme işareti", "upgrade_cancel": "İyileştirme işareti kaldırıldı",
    "board_approve": "Kare onayı", "board_reject": "Kare için değişiklik isteği", "board_reset": "Kare kararı geri alındı",
    "asset_lock": "Varlık kilidi", "asset_unlock": "Varlık kilidi açıldı", "revision_upload": "Elle düzeltme yüklendi",
    "revision_activate": "Revizyon seçimi", "style_activate": "Stil sürümü etkinleştirildi", "style_create": "Stil sürümü kaydedildi",
    "force_rerender": "Baştan üretim", "render": "Üretim isteği", "render_all": "Bütün çekimleri üret",
    "project_create": "Proje oluşturuldu", "project_settings": "Proje ayarları", "script_upload": "Senaryo yüklendi",
    "character_create": "Karakter eklendi", "scene_create": "Sahne eklendi", "scene_edit": "Sahne düzenlendi",
    "audio_change": "Ses kanalı değişti", "music_change": "Müzik değişti", "assemble": "Film birleştirme",
    "killswitch_freeze": "Üretim durduruldu", "killswitch_resume": "Üretime devam edildi",
    "refund_idle": "Boşta bekleme kayıtları iade edildi", "budget_change": "Bütçe değiştirildi",
    "chain_reset": "Temiz başlangıç (klipler eski, zincir kareleri yeniden çizilecek)",
    "grade_auto": "Otomatik renk eşleştirme istendi", "grade_auto_undo": "Otomatik renk eşleştirmesi geri alındı",
    "camera_change": "Kesme kamerası değişti", "entry_change": "Karakter girişi seçildi",
    "board_approve_all": "Storyboard şeridi onaylandı (videolar başlar)", "end_board_redraw": "Son kare yeniden çizildi",
    "asset_create": "Varlık eklendi", "asset_edit": "Varlık düzenlendi", "asset_reference": "Referans yüklendi",
    "asset_approve_still": "Durağan referans onayı", "motion_test": "Hareket testi istendi",
    "asset_approve_motion": "Hareket testi onayı", "asset_link": "Sahneye varlık bağlandı", "asset_unlink": "Varlık bağı kaldırıldı",
    "sequence_edit": "Sekans düzenlendi", "track_change": "Film kanalı değişti", "track_remove": "Film kanalı kaldırıldı",
    "storyboard_prepare": "Storyboard kareleri istendi", "draft_pass": "Taslak geçiş", "upgrade_pass": "İyileştirme geçişi",
    "method_change": "Üretim yöntemi", "external_upload": "Dış çekim yüklendi", "guide_upload": "Previs rehberi yüklendi",
    "grade_change": "Renk ayarı", "lut_change": "Film LUT'u", "continuity_check": "Süreklilik denetimi",
    "keyframe_upload": "İlk kare yüklendi (H3)", "keyframe_clear": "Yüklenen ilk kare kaldırıldı (H3)",
    "plan_approve": "Kuru çalıştırma planı onaylandı", "upgrade_final": "Final motoruyla yükseltme onaylandı",
    "draft_complete": "Seedance taslağı 1080p'ye tamamlandı (onaylı)", "upscale": "Upscale (1080p) onaylandı",
    "generation_adopt": "Bütün sahne tek üretime geçirildi", "scene_engine": "Sahnenin motoru değişti (yeniden üretim)",
    "shot_engine": "Çekim ayrı üretildi (sahne içi model değişimi)",
    "prompt_override": "Yönetmen istemi kaydedildi", "prompt_write": "Sahne istemi LLM ile yazdırıldı",
}
ID_FIELDS = ("project_id", "scene_id", "variation_id", "asset_id", "sequence_id", "track_id")
QUERY_FIELDS = ("kind", "filename", "note", "force_visual", "start_s", "end_s", "gain_db", "fade_in_s", "fade_out_s")
RESULT_FIELDS = ("stages", "blocked", "awaiting_board", "awaiting_plan", "plan_hash", "total_usd", "status", "version", "method", "regraded", "drafted", "planned",
                 "unblocked", "active_version", "upgrade_requested", "board_status")


def _plain(value):
    """JSON'a yazılabilir hale getirir."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (UUID, Decimal)):
        return str(value) if isinstance(value, UUID) else float(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _ids(kw: dict, result) -> dict:
    ids = {k: str(kw[k]) for k in ID_FIELDS if kw.get(k)}
    if "project_id" not in ids and isinstance(result, dict) and result.get("id") and "settings" in result:
        ids["project_id"] = str(result["id"])          # proje oluşturma: kimlik yanıttadır
    return ids


def _context(s, ids: dict) -> dict:
    """Proje kimliğini çözer ve eğitim için bağlam ekler."""
    ctx = {}
    if ids.get("variation_id"):
        v = s.get(Variation, ids["variation_id"])
        if v is not None:
            ids.setdefault("scene_id", str(v.scene_id) if v.scene_id else None)
            ctx["variation"] = {"tier": v.tier, "status": v.status, "rank": v.refined_rank, "score": v.composite_score,
                                "seed": v.seed, "anomalies": sorted({a["type"] for a in (v.anomalies or [])}),
                                "source": (v.params or {}).get("source", "generate")}
            if v.asset_id and not ids.get("asset_id"):
                ids["asset_id"] = str(v.asset_id)
    if ids.get("track_id") and not ids.get("project_id"):
        t = s.get(TimelineTrack, ids["track_id"])
        if t is not None:
            ids["project_id"] = str(t.project_id)
    if ids.get("sequence_id") and not ids.get("project_id"):
        q = s.get(Sequence, ids["sequence_id"])
        if q is not None:
            ids["project_id"] = str(q.project_id)
    if ids.get("asset_id"):
        a = s.get(Asset, ids["asset_id"])
        if a is not None:
            ids.setdefault("project_id", str(a.project_id))
            ctx["asset"] = {"type": a.type, "name": a.name, "status": a.status, "version": a.version}
    if ids.get("scene_id"):
        sc = s.get(Scene, ids["scene_id"])
        if sc is not None:
            ids.setdefault("project_id", str(sc.project_id))
            ctx["scene"] = {"code": sc.scene_code, "status": sc.status, "method": sc.method, "cut_tier": sc.cut_tier,
                            "board_status": sc.board_status}
    return ctx


def record(project_id: str, action: str, scene_id=None, variation_id=None, payload: dict | None = None) -> None:
    with session_scope() as s:
        s.add(CurationEvent(project_id=project_id, scene_id=scene_id, variation_id=variation_id, action=action,
                            payload=_plain(payload or {})))


def _after(action, kw: dict, result) -> None:
    try:
        name = action(kw, result) if callable(action) else action
        if not name:
            return
        ids = _ids(kw, result)
        with session_scope() as s:
            ctx = _context(s, ids)
        if not ids.get("project_id"):
            log.error("kürasyon olayı yazılamadı (%s): proje kimliği çözülemedi", name)
            return
        payload = {"request": {**({"body": _plain(kw["body"])} if "body" in kw and kw["body"] is not None else {}),
                               **{k: _plain(kw[k]) for k in QUERY_FIELDS if kw.get(k) is not None}},
                   "result": {k: _plain(result[k]) for k in RESULT_FIELDS if isinstance(result, dict) and k in result},
                   **ctx, **{k: v for k, v in ids.items() if k not in ("project_id", "scene_id", "variation_id")}}
        record(ids["project_id"], name, ids.get("scene_id"), ids.get("variation_id"), payload)
    except Exception:                                   # noqa: BLE001 — karar uygulandı; kayıt hatası günlüğe
        log.exception("kürasyon olayı yazılamadı (%s)", action)


def curated(action):
    """Uç noktayı kürasyon olayı yazacak şekilde sarar. action: eylem adı ya da (kwargs, yanıt) -> ad."""
    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def inner(*args, **kwargs):
                result = await fn(*args, **kwargs)
                await run_in_threadpool(_after, action, kwargs, result)
                return result
        else:
            @functools.wraps(fn)
            def inner(*args, **kwargs):
                result = fn(*args, **kwargs)
                _after(action, kwargs, result)
                return result
        inner.__curation_action__ = action
        return inner
    return wrap
