"""Ayarlar: ücretli servislerin bağlantıları (anahtar, model ve uç noktalar), ücretsiz/ücretli mod ve fiyat tablosu.

Her şey `.env` dosyasına (ve fiyatlar `pricing.yaml`'a) yazılır; bkz. app.runtime_config. Anahtarlar yanıtlarda yalnızca
maskeli görünür. Ücretli moda geçiş açık onay ister ve üretim sürerken yapılmaz.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import runtime_config as rc
from app.budget import pricing
from app.config import settings
from app.db import get_session
from app.models import Project, Scene

router = APIRouter(tags=["settings"])

WORKING = ("storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing")

SERVICES = {
    "vast": {"name": "MiniMax H3 · vast.ai", "purpose": "Video üretimi: MiniMax H3, kendi GPU makinenizde (SGLang ya da ComfyUI)",
             "key": "VAST_API_KEY", "console": "https://cloud.vast.ai/instances/",
             "used_by": ["Kurgu: görüntü üretimi (test ve tam tur; FL2VA ilk kareden, Ref2VA karakter referanslarıyla)",
                         "Storyboard ilk kareleri (ComfyUI keyframe.json iş akışı varsa)",
                         "Varlıklar: hareket testi ve karakter dönüş videosu", "Elle düzeltme: düzenlenmiş ilk kareden video",
                         "Makine yönetimi: boşta durdurma, otomatik başlatma, boşta bekleme bedeli",
                         "Yeni ve yedek makine: teklif arama, onaylı kiralama, setup.sh ile kurulum, eski makineyi silme",
                         "Çıktı güvencesi: her çıktı indirilip yerel depoda doğrulanır; eksik çıktılı makine silinmez"]},
    "fal": {"name": "fal.ai", "purpose": "İsteğe bağlı: Seedance, Flux kareleri, previs aktarımı, dudak senkronu",
            "key": "FAL_KEY", "console": "https://fal.ai/dashboard",
            "used_by": ["Kurgu: yönetmenin Seedance seçtiği çekimler (ilk kare Flux'tan)", "Storyboard kareleri (yoksa yerel çizilir)",
                        "Varlıklar: mekân ve nesne referans görseli", "Yöntem: previs rehberinden video",
                        "Dudak senkronu (yoksa video senkronsuz kullanılır)"]},
    "elevenlabs": {"name": "ElevenLabs", "purpose": "Türkçe diyalog seslendirme", "key": "ELEVENLABS_API_KEY",
                   "console": "https://elevenlabs.io/app",
                   "used_by": ["Ses: diyalog kanalları (konuşma motoru elevenlabs iken)"]},
    "anthropic": {"name": "Anthropic (Hermes)", "purpose": "Senaryo ayrıştırma (LLM modu)", "key": "ANTHROPIC_API_KEY",
                  "console": "https://console.anthropic.com",
                  "used_by": ["Senaryo: ayrıştırma (Hermes llm modundayken)"]},
}


def _key(env: str) -> dict:
    """Anahtarın maskeli durumu; değer yalnızca app.live içinden okunur (tembel içe aktarım)."""
    from app.live import key_state
    return key_state(rc.BY_ENV[env].attr)


def _active(service: str) -> bool:
    """Servis ücretli modda gerçekten çağrılır mı (anahtar dolu ve ilgili motor seçili)."""
    has_key = _key(SERVICES[service]["key"])["set"]
    if service == "vast":
        return has_key and bool(settings.h3_endpoint_url) and bool(settings.vast_instance_id)
    if service == "elevenlabs":
        return settings.tts_engine == "elevenlabs" and has_key
    if service == "anthropic":
        return settings.hermes_mode == "llm" and has_key
    return has_key


MODE_KEYS = {False: "real", True: "virtual", None: "unknown"}


def _spend(s: Session) -> dict[str, dict[str, float]]:
    """Servis başına işlenen tutar, gerçek ve sanal ayrı (servis geçmişinden; silinmiş projelerin harcaması dahil)."""
    out: dict[str, dict[str, float]] = {}
    for service, dry_run, usd in s.execute(text("""SELECT service, dry_run, SUM(amount_usd) FROM service_calls
                                                   WHERE status = 'succeeded' GROUP BY 1, 2""")).all():
        row = out.setdefault(service, {"real": 0.0, "virtual": 0.0, "unknown": 0.0})
        row[MODE_KEYS[dry_run]] += float(usd or 0)
    return out


def _sum(spend: dict[str, dict[str, float]], services, mode: str) -> float:
    return round(sum(v[mode] for k, v in spend.items() if services is None or k in services), 4)


def _busy(s: Session) -> list[str]:
    rows = s.execute(select(Project.title, Scene.scene_code).join(Project, Project.id == Scene.project_id)
                     .where(Scene.status.in_(WORKING)).order_by(Project.title, Scene.scene_code)).all()
    return [f"{t} {c}" for t, c in rows]


def _missing(service: str) -> list[str]:
    from app.live import missing_packages                    # tembel: app.live yalnızca gerektiğinde yüklenir
    return missing_packages(service)


def _needed(service: str) -> tuple[bool, str]:
    """Servis şu ayarlarla ücretli modda çağrılır mı; çağrılmıyorsa neden."""
    if service == "fal" and not _key("FAL_KEY")["set"]:
        return False, ("isteğe bağlı: anahtar yoksa Seedance seçilemez, storyboard karesi yerel çizilir ve dudak senkronu "
                       "atlanır; video MiniMax H3 ile üretilir")
    if service == "elevenlabs" and settings.tts_engine != "elevenlabs":
        return False, f"konuşma motoru {settings.tts_engine}: ElevenLabs çağrılmaz, anahtar gerekmez"
    if service == "anthropic" and settings.hermes_mode != "llm":
        return False, "senaryo ayrıştırma kural tabanlı: Anthropic çağrılmaz, anahtar gerekmez"
    return True, ""


def _scorer() -> dict:
    """Ücretli modda kalite kapısı gerçek skorlayıcıyı ister; yoksa üretim başlamaz."""
    from app.quality import scorer
    if settings.quality_scorer == "dry":
        return {"ready": False, "message": "QUALITY_SCORER=dry: ücretli modda üretim başlamaz (auto ya da real olmalı)"}
    reason = scorer.unavailable()
    return {"ready": reason is None, "message": reason or "Gerçek kalite skorlayıcısı hazır (OpenCLIP + SFace, yerel ve ücretsiz)"}


def _translator() -> dict:
    """Ücretli modda Türkçe tarifler sağlayıcıya gitmeden yerel modelle İngilizceye çevrilir (hermes/translate.py)."""
    from app.hermes import translate
    if settings.prompt_translation == "off":
        return {"ready": False, "message": "PROMPT_TRANSLATION=off: Türkçe tarifler çevrilmez; senaryoyu İngilizce yazın"}
    reason = translate.unavailable()
    if reason:
        return {"ready": False, "message": f"kullanılamıyor ({reason}): Türkçe senaryoyla ücretli üretim başlamaz, İngilizce senaryo çalışır"}
    return {"ready": True, "message": "Türkçe tarifler sağlayıcıya gitmeden İngilizceye çevrilir (opus-mt-tr-en, yerel ve ücretsiz)"}


def state(s: Session) -> dict:
    rc.refresh_if_changed()
    spend = _spend(s)
    services = []
    for sid, meta in SERVICES.items():
        fields = []
        for f in rc.FIELDS:
            if f.service != sid:
                continue
            item = {"env": f.env, "label": f.label, "kind": f.kind}
            if f.kind == "secret":
                item.update(_key(f.env))
            else:
                value = rc.display(getattr(settings, f.attr))      # seçenekler metin: sayılar ve açık/kapalı da
                default = rc.display(type(settings).model_fields[f.attr].default)
                item.update({"value": value, "default": default, "choices": list(f.choices),
                             "url": f.url.format(value) if f.url else None, "minimum": f.minimum})
            fields.append(item)
        needed, why = _needed(sid)
        services.append({"id": sid, "name": meta["name"], "key_env": meta["key"], "needed": needed, "not_needed_reason": why,
                         "missing_packages": _missing(sid), "purpose": meta["purpose"], "console": meta["console"],
                         "used_by": meta["used_by"], "fields": fields, "active": _active(sid),
                         "key_set": _key(meta["key"])["set"], "spent_usd": _sum(spend, {sid}, "real"),
                         "virtual_usd": _sum(spend, {sid}, "virtual"), "unknown_usd": _sum(spend, {sid}, "unknown")})
    return {
        "dry_run": settings.dry_run, "editable": bool(rc.env_path()), "env_file": rc.env_path(),
        "confirmation": rc.PAID_CONFIRMATION, "busy": _busy(s), "services": services,
        "spent_usd": _sum(spend, None, "real"), "virtual_usd": _sum(spend, None, "virtual"),
        "unknown_usd": _sum(spend, None, "unknown"), "audit": rc.audit(),
        "pricing_file": settings.pricing_file, "scorer": _scorer(), "translator": _translator(),
        "comfy": _comfy(),
    }


def _comfy() -> dict:
    """ComfyUI iş akışlarının durumu (arka uç ComfyUI iken gerekir): dosya var mı, hangi yer tutucular var, eksik var mı."""
    from app.live import vast_h3                         # iş akışı denetimi saf; ağ yok
    flows = []
    for kind in vast_h3.COMFY_KINDS:
        path = vast_h3.workflow_path(kind)
        item = {"kind": kind, "present": os.path.exists(path), "required": list(vast_h3.COMFY_REQUIRED[kind]),
                "placeholders": [], "error": None}
        if item["present"]:
            try:
                with open(path, encoding="utf-8") as f:
                    item["placeholders"] = sorted(vast_h3.placeholders(vast_h3.validate_workflow(kind, f.read())))
            except (OSError, ValueError) as e:
                item["error"] = str(e)
        flows.append(item)
    return {"backend": settings.h3_backend, "dir": settings.h3_comfy_workflow_dir, "workflows": flows}


@router.get("/settings")
def get_settings(s: Session = Depends(get_session)):
    return state(s)


class SettingsIn(BaseModel):
    values: dict[str, str] = PField(min_length=1, max_length=20)


@router.patch("/settings")
def patch_settings(body: SettingsIn, s: Session = Depends(get_session)):
    """Beyaz listedeki ayarları .env'e yazar ve bütün süreçlerde tazeler. Boş anahtar değeri anahtarı siler."""
    try:
        changed = rc.update(body.values)
    except rc.ConfigError as e:
        raise HTTPException(422, str(e))
    return {"changed": [c["field"] for c in changed], **state(s)}


class ModeIn(BaseModel):
    paid: bool
    confirm: str | None = PField(None, max_length=100)


@router.post("/settings/mode")
def set_mode(body: ModeIn, s: Session = Depends(get_session)):
    """Ücretli moda geçiş: onay ifadesi ve üretimin durmuş olması gerekir. Ücretsiz moda dönüş her zaman serbesttir."""
    if body.paid and not _key("VAST_API_KEY")["set"]:
        raise HTTPException(422, "vast.ai API anahtarı tanımlı değil: video MiniMax H3 ile vast.ai makinesinde üretilir. "
                                 "Önce Ayarlar › MiniMax H3'ten anahtarı girin; makineyi sonra \"Yeni makine kirala\" ile "
                                 "kurabilir ya da kimliğini ve adresini elle girebilirsiniz.")
    note = None
    if not body.paid and not settings.dry_run and _active("vast") and int(settings.h3_idle_stop_minutes or 0) > 0:
        note = _stop_box_before_free_mode()
    if body.paid:
        missing = [m for sid in SERVICES if _needed(sid)[0] for m in _missing(sid)]
        if missing:
            raise HTTPException(422, f"Ücretli paketler kurulu değil ({', '.join(missing)}): imajı yeniden derleyin "
                                     "(docker compose up -d --build).")
    try:
        result = rc.set_mode(body.paid, body.confirm, _busy(s) if body.paid else [])
    except rc.ConfigError as e:
        raise HTTPException(409 if "Üretim sürerken" in str(e) else 422, str(e))
    return {**result, "vast_note": note, **state(s)}


def _stop_box_before_free_mode() -> str | None:
    """Ücretsiz moda dönerken (hâlâ ücretli moddayken) çalışan H3 makinesi durdurulur: ücretsiz modda hiçbir şey onu
    kullanmaz ama vast.ai saatle faturalamaya devam ederdi. Başarısızlık mod değişimini engellemez."""
    from app.live import vast_api                       # ücretli modül: yalnızca fonksiyon içinde
    try:
        if not vast_api.instance().running:
            return None
        vast_api.set_state("stopped")
        return "vast.ai makinesi durduruldu (ücretsiz modda kullanılmaz)."
    except Exception as e:                              # noqa: BLE001
        return f"vast.ai makinesi durdurulamadı; vast.ai panelinden durdurun ({type(e).__name__})."


class InstanceIn(BaseModel):
    action: str = PField(pattern="^(start|stop)$")


@router.post("/settings/vast/instance")
def instance_action(body: InstanceIn):
    """H3 makinesini başlatır ya da durdurur (yalnızca ücretli modda: vast.ai API'si ücretli kilitten geçer)."""
    rc.refresh_if_changed()
    if settings.dry_run:
        raise HTTPException(409, "Ücretsiz modda makine başlatılıp durdurulamaz (vast.ai API'si ücretli kilitten geçer); "
                                 "vast.ai panelini kullanın.")
    from app.live import PaidCallBlocked, vast_api       # ücretli modül: yalnızca fonksiyon içinde
    try:
        vast_api.set_state("running" if body.action == "start" else "stopped")
        box = vast_api.instance()
    except PaidCallBlocked as e:
        raise HTTPException(409, str(e))
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(502, f"vast.ai yanıt vermedi: {type(e).__name__}")
    return {"status": box.status, "intended": box.intended, "geolocation": box.geolocation, "dph": box.dph}


@router.get("/settings/vast/replacement")
def replacement_state():
    """Makine: onay bekleyen öneri, kiralama/kurulum durumu, çıktı güvencesi ve sertifika sabitleme (yalnızca kayıt
    okunur; vast.ai ve makine çağrılmaz)."""
    rc.refresh_if_changed()
    from app.live import h3_outputs, tls_pin, vast_box  # Redis ve veritabanı kaydı; ağ yok
    return {"mode": settings.h3_replace_mode, "proposal": vast_box.proposal(), "job": vast_box.job(),
            "states": vast_box.JOB_TR, "conditions": vast_box.conditions(), "outputs": h3_outputs.summary(),
            "retrieval": vast_box.retrieval(), "retrieval_states": vast_box.RETRIEVAL_TR, "tls": tls_pin.state(),
            "instance_id": settings.vast_instance_id or None}


@router.post("/settings/vast/provision")
def provision():
    """"Yeni makine kirala": uygun teklifi bulur ve onaya sunar (yedek makine akışı; hiçbir makine silinmez). Yalnızca
    ücretli modda: teklif arama ve kiralama vast.ai API'sinin ücretli kilidinden geçer."""
    rc.refresh_if_changed()
    if settings.dry_run:
        raise HTTPException(409, "Ücretsiz modda makine kiralanmaz: önce ücretli moda geçin.")
    from app.live import PaidCallBlocked, vast_box
    current = vast_box.job()
    if current and current.get("state") in vast_box.ACTIVE_JOB:
        raise HTTPException(409, f"Yeni makine zaten {vast_box.JOB_TR[current['state']]}.")
    try:
        prop = vast_box.propose_initial()
    except PaidCallBlocked as e:
        raise HTTPException(409, str(e))
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(502, f"vast.ai yanıt vermedi: {type(e).__name__}")
    return {"proposal": prop, "job": current}


@router.get("/settings/vast/setup-log")
def setup_log():
    """Kiralanan makinenin kurulum günlüğü (setup.sh; portalın kimlik doğrulamalı günlük portundan). Yalnızca ücretli modda."""
    rc.refresh_if_changed()
    if settings.dry_run:
        raise HTTPException(409, "Ücretsiz modda makineye bağlanılmaz.")
    from app.live import PaidCallBlocked, vast_box
    try:
        return vast_box.setup_log()
    except PaidCallBlocked as e:
        raise HTTPException(409, str(e))
    except Exception as e:                               # noqa: BLE001 — makine açılırken bağlantı hatası olağan
        return {"available": False, "lines": [], "message": f"Günlüğe ulaşılamadı ({type(e).__name__}); makine "
                                                            "açılıyor olabilir."}


class OutputsIn(BaseModel):
    action: str = PField(pattern="^(retrieve|abandon)$")
    instance_id: str = PField(min_length=1, max_length=40, pattern=r"^[0-9A-Za-z_-]+$")
    destroy_after: bool = False


@router.post("/settings/vast/outputs")
def outputs_action(body: OutputsIn):
    """Çıktı güvencesi: makinenin eksik çıktılarını indir (gerekirse makine başlatılır; destroy_after: hepsi inince eski
    makine silinir) ya da kaybı kabul edip eski makineyi sil. Yalnızca ücretli modda."""
    rc.refresh_if_changed()
    if settings.dry_run:
        raise HTTPException(409, "Ücretsiz modda makineye bağlanılmaz.")
    from app.live import PaidCallBlocked, vast_box
    if body.action == "retrieve":
        running = vast_box.retrieval()
        if running and running.get("state") in ("starting", "waiting", "downloading"):
            raise HTTPException(409, "Eksik çıktılar zaten indiriliyor.")
        return {"retrieval": vast_box.start_retrieval(body.instance_id, body.destroy_after)}
    try:
        return vast_box.abandon(body.instance_id)
    except PaidCallBlocked as e:
        raise HTTPException(409, str(e))
    except Exception as e:                               # noqa: BLE001
        raise HTTPException(502, f"vast.ai yanıt vermedi: {type(e).__name__}")


@router.delete("/settings/vast/tls-pin")
def reset_pin():
    """Sunucu sertifikası sabitlemesini sıfırlar (makine bilerek yenilendiyse): bir sonraki başarılı bağlantı yeniden
    sabitler. Yalnızca kayıt silinir; ağ yok."""
    rc.refresh_if_changed()
    from app.live import tls_pin
    endpoint = tls_pin.state()["endpoint"]
    tls_pin.forget(endpoint) if endpoint else tls_pin.forget()
    try:
        rc.bump([{"field": "h3_tls_pin", "action": "clear", "detail": f"sertifika sabitlemesi sıfırlandı ({endpoint or 'hepsi'})"}])
    except Exception:                                     # Redis yoksa yalnızca kayıt eksik kalır
        pass
    return tls_pin.state()


class ReplacementIn(BaseModel):
    action: str = PField(pattern="^(approve|reject)$")


@router.post("/settings/vast/replacement")
def replacement_decision(body: ReplacementIn):
    """Yönetmenin kararı: öneriyi onaylarsa yeni makine kiralanır ve kurulur (yalnızca ücretli modda); reddederse öneri silinir."""
    rc.refresh_if_changed()
    from app.live import vast_box
    prop = vast_box.proposal()
    if prop is None:
        raise HTTPException(404, "Onay bekleyen yedek makine önerisi yok.")
    if body.action == "reject":
        vast_box._r().delete(vast_box._k("proposal"))
        return {"proposal": None, "job": vast_box.job()}
    if settings.dry_run:
        raise HTTPException(409, "Ücretsiz modda makine kiralanmaz.")
    current = vast_box.job()
    if current and current.get("state") in vast_box.ACTIVE_JOB:
        raise HTTPException(409, f"Yeni makine zaten {vast_box.JOB_TR[current['state']]}.")
    return {"proposal": prop, "job": vast_box.start_replacement(prop)}


class WorkflowIn(BaseModel):
    text: str = PField(min_length=2, max_length=2_000_000)


@router.put("/settings/comfy/{kind}")
def put_workflow(kind: str, body: WorkflowIn):
    """ComfyUI iş akışını (Export (API) JSON'u, yer tutuculu) doğrular ve iş akışı klasörüne yazar."""
    from app.live import vast_h3                         # iş akışı denetimi saf; ağ yok
    try:
        vast_h3.validate_workflow(kind, body.text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        os.makedirs(settings.h3_comfy_workflow_dir, exist_ok=True)
        with open(vast_h3.workflow_path(kind), "w", encoding="utf-8") as f:
            f.write(body.text)
    except OSError as e:
        raise HTTPException(422, f"İş akışı yazılamadı ({e.strerror}): {settings.h3_comfy_workflow_dir}")
    try:
        rc.bump([{"field": f"comfy/{kind}.json", "action": "set", "detail": f"ComfyUI iş akışı güncellendi: {kind}"}])
    except Exception:                                     # Redis yoksa yalnızca kayıt eksik kalır
        pass
    return _comfy()


@router.delete("/settings/comfy/{kind}")
def delete_workflow(kind: str):
    from app.live import vast_h3
    if kind not in vast_h3.COMFY_KINDS:
        raise HTTPException(404, "Bilinmeyen iş akışı")
    path = vast_h3.workflow_path(kind)
    if os.path.exists(path):
        os.remove(path)
    return _comfy()


@router.get("/settings/pricing")
def get_pricing():
    rc.refresh_if_changed()
    with open(settings.pricing_file, encoding="utf-8") as f:
        raw = f.read()
    return {"path": settings.pricing_file, "text": raw, "table": pricing.validate_table(raw)}


class PricingIn(BaseModel):
    text: str = PField(min_length=10, max_length=20000)


@router.put("/settings/pricing")
def put_pricing(body: PricingIn):
    """Doğrulanmış fiyat tablosunu yazar; bütçe rezervasyonları hemen yeni fiyatlarla yapılır."""
    try:
        data = pricing.save_table(body.text)
    except pricing.PricingError as e:
        raise HTTPException(422, str(e))
    try:
        rc.bump([{"field": "pricing.yaml", "action": "set", "detail": "fiyat tablosu güncellendi"}])
    except Exception:                                          # Redis yoksa yalnızca bu süreç güncel
        pass
    return {"path": settings.pricing_file, "text": body.text, "table": data}


@router.post("/settings/test/{service}")
def test_connection(service: str):
    """Anahtarın sağlayıcı tarafından kabul edilip edilmediği (ücretsiz doğrulama ucu; üretim yapmaz)."""
    if service not in SERVICES:
        raise HTTPException(404, "Bilinmeyen servis")
    rc.refresh_if_changed()
    from app.live import probe, probe_key                    # ağ kodu yalnızca app/live altında, tembel yüklenir
    return probe.check(service, probe_key(rc.BY_ENV[SERVICES[service]["key"]].attr))

