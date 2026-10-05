"""Ayarlar: ücretli servislerin bağlantıları (anahtar, model ve uç noktalar), ücretsiz/ücretli mod ve fiyat tablosu.

Her şey `.env` dosyasına (ve fiyatlar `pricing.yaml`'a) yazılır; bkz. app.runtime_config. Anahtarlar yanıtlarda yalnızca
maskeli görünür. Ücretli moda geçiş açık onay ister ve üretim sürerken yapılmaz.
"""
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
    "fal": {"name": "fal.ai", "purpose": "İlk kare ve storyboard (Flux), video (Wan, Seedance), previs aktarımı, dudak senkronu",
            "key": "FAL_KEY", "console": "https://fal.ai/dashboard", "providers": ("fal_wan", "seedance", "higgsfield", "flux_lora", "v2v", "fal"),
            "used_by": ["Kurgu: görüntü üretimi (test ve tam tur)", "Storyboard kareleri", "Varlıklar: referans ve hareket testi",
                        "Yöntem: previs rehberinden video", "Elle düzeltme: ilk kareden video", "Dudak senkronu"]},
    "elevenlabs": {"name": "ElevenLabs", "purpose": "Türkçe diyalog seslendirme", "key": "ELEVENLABS_API_KEY",
                   "console": "https://elevenlabs.io/app", "providers": ("elevenlabs",),
                   "used_by": ["Ses: diyalog kanalları (konuşma motoru elevenlabs iken)"]},
    "anthropic": {"name": "Anthropic (Hermes)", "purpose": "Senaryo ayrıştırma (LLM modu)", "key": "ANTHROPIC_API_KEY",
                  "console": "https://console.anthropic.com", "providers": ("anthropic",),
                  "used_by": ["Senaryo: ayrıştırma (Hermes llm modundayken)"]},
}


def _key(env: str) -> dict:
    """Anahtarın maskeli durumu; değer yalnızca app.live içinden okunur (tembel içe aktarım)."""
    from app.live import key_state
    return key_state(rc.BY_ENV[env].attr)


def _active(service: str) -> bool:
    """Servis ücretli modda gerçekten çağrılır mı (anahtar dolu ve ilgili motor seçili)."""
    has_key = _key(SERVICES[service]["key"])["set"]
    if service == "elevenlabs":
        return settings.tts_engine == "elevenlabs" and has_key
    if service == "anthropic":
        return settings.hermes_mode == "llm" and has_key
    return has_key


def _spend(s: Session) -> dict[str, float]:
    rows = s.execute(text("SELECT split_part(provider, ':', 1), SUM(amount_usd) FROM cost_ledger "
                          "WHERE kind = 'commit' GROUP BY 1")).all()
    return {p: float(v or 0) for p, v in rows}


def _busy(s: Session) -> list[str]:
    rows = s.execute(select(Project.title, Scene.scene_code).join(Project, Project.id == Scene.project_id)
                     .where(Scene.status.in_(WORKING)).order_by(Project.title, Scene.scene_code)).all()
    return [f"{t} {c}" for t, c in rows]


def _missing(service: str) -> list[str]:
    from app.live import missing_packages                    # tembel: app.live yalnızca gerektiğinde yüklenir
    return missing_packages(service)


def _needed(service: str) -> tuple[bool, str]:
    """Servis şu ayarlarla ücretli modda çağrılır mı; çağrılmıyorsa neden."""
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
                value = getattr(settings, f.attr)
                default = type(settings).model_fields[f.attr].default
                item.update({"value": value, "default": default, "choices": list(f.choices),
                             "url": f.url.format(value) if f.url else None})
            fields.append(item)
        needed, why = _needed(sid)
        services.append({"id": sid, "name": meta["name"], "needed": needed, "not_needed_reason": why,
                         "missing_packages": _missing(sid), "purpose": meta["purpose"], "console": meta["console"],
                         "used_by": meta["used_by"], "fields": fields, "active": _active(sid),
                         "key_set": _key(meta["key"])["set"],
                         "spent_usd": round(sum(v for p, v in spend.items() if p in meta["providers"]), 4)})
    return {
        "dry_run": settings.dry_run, "editable": bool(rc.env_path()), "env_file": rc.env_path(),
        "confirmation": rc.PAID_CONFIRMATION, "busy": _busy(s), "services": services,
        "spent_usd": round(sum(spend.values()), 4), "audit": rc.audit(),
        "pricing_file": settings.pricing_file, "scorer": _scorer(),
    }


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
    if body.paid and not _key("FAL_KEY")["set"]:
        raise HTTPException(422, "fal.ai anahtarı tanımlı değil: görüntü ve video yalnızca fal.ai ile üretilir. Önce anahtarı girin.")
    if body.paid:
        missing = [m for sid in SERVICES if sid == "fal" or _needed(sid)[0] for m in _missing(sid)]
        if missing:
            raise HTTPException(422, f"Ücretli paketler kurulu değil ({', '.join(missing)}): imajı yeniden derleyin "
                                     "(docker compose up -d --build).")
    try:
        result = rc.set_mode(body.paid, body.confirm, _busy(s) if body.paid else [])
    except rc.ConfigError as e:
        raise HTTPException(409 if "Üretim sürerken" in str(e) else 422, str(e))
    return {**result, **state(s)}


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

