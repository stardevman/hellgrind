"""Panelden düzenlenen ücretli servis ayarları.

Tek kaynak `.env` dosyasıdır (değişmez kural 8): panel yalnızca aşağıdaki beyaz listedeki anahtarları bu dosyaya yazar,
veritabanı ve depo ayarlarına dokunmaz. Dosya api, worker ve beat konteynerlerine `/config/.env` olarak bağlanır. Yazılınca
Redis'teki sürüm sayacı artar; her süreç bir sonraki istekte ya da görevde sayacı görür ve ayarları dosyadan tazeler
(yeniden başlatma gerekmez). Gizli anahtarlar hiçbir yanıtta, günlükte ya da değişiklik kaydında açık yazılmaz.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass

from pydantic import TypeAdapter

from app.config import settings

log = logging.getLogger(__name__)

VERSION_KEY = "hg:config:version"
AUDIT_KEY = "hg:config:audit"
AUDIT_LIMIT = 100
PAID_CONFIRMATION = "ücretli moda geç"
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,159}$")
SERVER_URL = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?$")
FREE_TEXT = re.compile(r"^[^\r\n\"'#\\]{0,160}$")
NUMBER = re.compile(r"^\d{1,6}(\.\d{1,4})?$")
SECRET_VALUE = re.compile(r"^[^\s\"'#\\]{8,400}$")


@dataclass(frozen=True)
class Field:
    env: str                    # .env adı
    attr: str                   # settings alanı
    kind: str                   # secret | model | url | text | number | choice | mode
    service: str                # vast | fal | elevenlabs | anthropic | core
    label: str
    choices: tuple[str, ...] = ()
    url: str | None = None      # model sayfası (doğrulamak için)


FIELDS: tuple[Field, ...] = (
    Field("DRY_RUN", "dry_run", "mode", "core", "Ücretsiz mod"),
    Field("VAST_API_KEY", "vast_api_key", "secret", "vast", "vast.ai API anahtarı"),
    Field("VAST_INSTANCE_ID", "vast_instance_id", "text", "vast", "Makine kimliği (instance ID)"),
    Field("H3_BACKEND", "h3_backend", "choice", "vast", "Arka uç (sglang: deneysel)", ("comfyui", "sglang")),
    Field("H3_ENDPOINT_URL", "h3_endpoint_url", "url", "vast", "Sunucu adresi (SGLang FL2VA ya da ComfyUI)"),
    Field("H3_REF_ENDPOINT_URL", "h3_ref_endpoint_url", "url", "vast", "Ref2VA sunucusu (SGLang; ComfyUI'de boş: aynı sunucu)"),
    Field("H3_API_KEY", "h3_api_key", "secret", "vast", "Portal belirteci (isteğe bağlı; SSH tünelinde gerekmez)"),
    Field("H3_IDLE_STOP_MINUTES", "h3_idle_stop_minutes", "choice", "vast", "Boşta durdurma (dakika; 0: kapalı)",
          ("0", "5", "10", "15", "30", "60")),
    Field("H3_AUTO_START", "h3_auto_start", "choice", "vast", "İş gelince durmuş makineyi başlat", ("false", "true")),
    Field("H3_MODEL", "h3_model", "model", "vast", "Model", url="https://huggingface.co/{}"),
    Field("H3_MODEL_REVISION", "h3_model_revision", "text", "vast", "Model sürümü (klip meta verisine yazılır)"),
    Field("H3_STEPS", "h3_steps", "choice", "vast", "Adım sayısı (sunucudaki kurulumla aynı)", ("50", "20", "9", "8", "5")),
    Field("H3_TERRITORY_AUTHORIZED", "h3_territory_authorized", "choice", "vast",
          "Bölge izni (AB, BK, G. Kore, ABD için MiniMax'ten izin alındı)", ("false", "true")),
    Field("H3_TLS_VERIFY", "h3_tls_verify", "choice", "vast", "Sunucu sertifikasını doğrula (yedek makinede false)",
          ("true", "false")),
    Field("H3_REPLACE_MODE", "h3_replace_mode", "choice", "vast",
          "Yedek makine (otomatik başlatma başarısız olursa): off, ask (her zaman sor), auto (tavan altındaysa)",
          ("ask", "auto", "off")),
    Field("H3_MAX_DPH", "h3_max_dph", "number", "vast", "Yedek makine fiyat tavanı ($/sa)"),
    Field("H3_MIN_RAM_GB", "h3_min_ram_gb", "number", "vast", "Yedek makine en az RAM (GB)"),
    Field("H3_MIN_DISK_GB", "h3_min_disk_gb", "number", "vast", "Yedek makine disk (GB)"),
    Field("H3_MIN_INET_MBPS", "h3_min_inet_mbps", "number", "vast", "Yedek makine en az indirme hızı (Mbps)"),
    Field("H3_GPU_NAME", "h3_gpu_name", "text", "vast", "Yedek makinenin GPU'su (boş: eskisiyle aynı)"),
    Field("H3_IMAGE_MODEL", "h3_image_model", "choice", "vast", "Storyboard görsel modeli (setup.sh kurar; Apache 2.0)",
          ("qwen-image-2512", "flux-schnell", "none")),
    Field("H3_BOX_IMAGE", "h3_box_image", "text", "vast", "Yedek makinenin Docker imajı"),
    Field("H3_SETUP_SCRIPT_URL", "h3_setup_script_url", "url", "vast", "setup.sh adresi (boş: isteğe gömülür)"),
    Field("FAL_KEY", "fal_key", "secret", "fal", "API anahtarı"),
    Field("FAL_FLUX_ENDPOINT", "fal_flux_endpoint", "model", "fal", "İlk kare ve storyboard (Flux LoRA)", url="https://fal.ai/models/{}"),
    Field("FAL_WAN_ENDPOINT", "fal_wan_endpoint", "model", "fal", "Video: Wan 2.2 (yalnızca eski çekimler)", url="https://fal.ai/models/{}"),
    Field("FAL_SEEDANCE_ENDPOINT", "fal_seedance_endpoint", "model", "fal", "Video: Seedance (yönetmenin seçtiği çekimler)", url="https://fal.ai/models/{}"),
    Field("FAL_LIPSYNC_ENDPOINT", "fal_lipsync_endpoint", "model", "fal", "Dudak senkronu", url="https://fal.ai/models/{}"),
    Field("FAL_WAN_V2V_ENDPOINT", "fal_wan_v2v_endpoint", "model", "fal", "Previs rehberinden video (v2v)", url="https://fal.ai/models/{}"),
    Field("FAL_KONTEXT_ENDPOINT", "fal_kontext_endpoint", "model", "fal", "Karakter referansıyla kare (Flux Kontext)", url="https://fal.ai/models/{}"),
    Field("ELEVENLABS_API_KEY", "elevenlabs_api_key", "secret", "elevenlabs", "API anahtarı"),
    Field("ELEVENLABS_MODEL", "elevenlabs_model", "model", "elevenlabs", "Ses modeli"),
    Field("TTS_ENGINE", "tts_engine", "choice", "elevenlabs", "Konuşma motoru", ("espeak", "tone", "elevenlabs")),
    Field("ANTHROPIC_API_KEY", "anthropic_api_key", "secret", "anthropic", "API anahtarı"),
    Field("HERMES_MODEL", "hermes_model", "model", "anthropic", "Hermes modeli"),
    Field("HERMES_MODE", "hermes_mode", "choice", "anthropic", "Senaryo ayrıştırma", ("rules", "llm")),
)
BY_ENV = {f.env: f for f in FIELDS}
_BOOL = TypeAdapter(bool)
_seen: str | None = None                                     # bu sürecin son uyguladığı sürüm


class ConfigError(ValueError):
    """Geçersiz ayar değeri ya da yazılamayan dosya (kullanıcıya Türkçe gösterilir)."""


def env_path() -> str | None:
    """Düzenlenen .env dosyası; tanımsızsa (ör. testler, CI) panel düzenlemesi kapalıdır."""
    return settings.config_env_file or None


def read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


def validate(env: str, value: str) -> str:
    f = BY_ENV.get(env)
    if f is None or f.kind == "mode":
        raise ConfigError(f"{env} panelden düzenlenemez")
    value = (value or "").strip()
    if f.kind == "secret":
        if value and not SECRET_VALUE.match(value):
            raise ConfigError(f"{env}: anahtar boşluk, tırnak ya da # içeremez ve en az 8 karakter olmalı")
    elif f.kind == "model":
        if not MODEL_ID.match(value):
            raise ConfigError(f"{f.label}: geçerli bir model ya da uç nokta kimliği girin (ör. fal-ai/flux-lora)")
    elif f.kind == "number":
        if not NUMBER.match(value) or float(value) <= 0:
            raise ConfigError(f"{f.label}: sıfırdan büyük bir sayı girin (ör. 0.95)")
    elif f.kind == "text":
        if not FREE_TEXT.match(value):
            raise ConfigError(f"{f.label}: en çok 160 karakter; satır sonu, tırnak ya da # içeremez")
    elif f.kind == "url":                                    # boş: tanımlı değil
        value = value.rstrip("/")
        if value and not SERVER_URL.match(value):
            raise ConfigError(f"{f.label}: http:// ya da https:// ile başlayan bir sunucu adresi girin "
                              "(ör. https://ornek.trycloudflare.com ya da http://1.2.3.4:40123)")
    elif f.kind == "choice" and value not in f.choices:
        raise ConfigError(f"{f.label}: {', '.join(f.choices)} değerlerinden biri olmalı")
    return value


def write_env(path: str, updates: dict[str, str]) -> None:
    """Satırları yerinde günceller (yorumlar ve sıra korunur); olmayan anahtarlar sona eklenir. Dosya tek dosyalık bir
    bağlama noktası olduğu için yeniden adlandırılamaz: yerinde yazılır."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    pending = dict(updates)
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in pending:
            lines[i] = f"{k}={pending.pop(k)}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Panelden düzenlenen ücretli servis ayarları")
        lines.extend(f"{k}={v}" for k, v in pending.items())
    try:
        with open(path, "r+" if os.path.exists(path) else "w", encoding="utf-8") as f:
            f.seek(0)
            f.write("\n".join(lines) + "\n")
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        raise ConfigError(f".env dosyasına yazılamadı ({e.strerror}). docker-compose.yml'de ./.env bağlı mı?") from e


def display(value) -> str:
    """Ayar değerinin .env ve paneldeki metin hâli (açık/kapalı: true/false)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _typed(f: Field, raw: str):
    """Dosyadaki metni ayarın türüne çevirir (mod ve açık/kapalı seçimleri bool, sayılar int)."""
    annotation = type(settings).model_fields[f.attr].annotation
    if f.kind == "mode" or annotation is bool:
        return _BOOL.validate_python(raw if raw != "" else f.kind == "mode")
    if annotation is int:
        return int(float(raw))
    if annotation is float:
        return float(raw)
    return raw


def apply(values: dict[str, str]) -> None:
    """Dosyadaki beyaz liste değerlerini bu sürecin ayarlarına uygular (yalnızca beyaz liste: DB ve depo ayarlarına
    dokunulmaz)."""
    for f in FIELDS:
        if f.env not in values:
            continue
        raw = values[f.env]
        try:
            setattr(settings, f.attr, _typed(f, raw))
        except ValueError:
            log.warning("%s geçersiz (%r); eski değer korunuyor", f.env, raw if f.kind != "secret" else "***")


def _redis():
    from app.budget import killswitch
    return killswitch.r()


def refresh_if_changed() -> None:
    """Başka bir süreç ayarları değiştirdiyse dosyadan yeniden okur. İlk çağrıda her zaman okur (konteyner ortamı
    dosyanın eski hâlinden oluşmuş olabilir). Redis ya da dosya yoksa sessizce geçer."""
    global _seen
    path = env_path()
    if not path or not os.path.exists(path):
        return
    try:
        version = _redis().get(VERSION_KEY)
    except Exception:                                          # Redis yoksa ayarlar dosyadan bir kez okunur
        version = _seen or "0"
    version = version.decode() if isinstance(version, bytes) else str(version or "0")
    if version == _seen:
        return
    try:
        apply(read_env(path))
    except OSError:
        return
    from app.budget import pricing
    pricing.table.cache_clear()
    _seen = version


def bump(entries: list[dict]) -> None:
    """Değişikliği yayınlar (bütün süreçler tazelenir) ve değişiklik kaydına yazar. Kayıtta değer yoktur."""
    global _seen
    r = _redis()
    pipe = r.pipeline()
    pipe.incr(VERSION_KEY)
    for e in entries:
        pipe.lpush(AUDIT_KEY, json.dumps({"ts": time.time(), **e}, ensure_ascii=False))
    pipe.ltrim(AUDIT_KEY, 0, AUDIT_LIMIT - 1)
    pipe.execute()
    _seen = None                                               # bu süreç de hemen yeniden okusun
    refresh_if_changed()


def audit(limit: int = 30) -> list[dict]:
    try:
        return [json.loads(x) for x in _redis().lrange(AUDIT_KEY, 0, limit - 1)]
    except Exception:
        return []


def update(values: dict[str, str]) -> list[dict]:
    """Değerleri doğrular, .env'e yazar ve yayınlar. Dönen kayıtlar (değer içermez) değişiklik kaydına yazılmıştır."""
    path = env_path()
    if not path:
        raise ConfigError("Ayar dosyası bağlı değil (CONFIG_ENV_FILE boş); panelden düzenleme kapalı.")
    clean = {env: validate(env, v) for env, v in values.items()}
    current = read_env(path) if os.path.exists(path) else {}
    # Gizli anahtarlar yalnızca dosyadakiyle karşılaştırılır (değerleri app.live dışında okunmaz, kural 1).
    changed = {k: v for k, v in clean.items()
               if current.get(k, "" if BY_ENV[k].kind == "secret" else display(getattr(settings, BY_ENV[k].attr))) != v}
    if not changed:
        return []
    write_env(path, changed)
    entries = []
    for env, v in changed.items():
        f = BY_ENV[env]
        if f.kind == "secret":
            entries.append({"field": env, "action": "cleared" if not v else "set",
                            "detail": "anahtar silindi" if not v else f"anahtar değişti (••••{v[-4:]})"})
        else:
            entries.append({"field": env, "action": "set", "detail": f"{f.label}: {v}"})
    bump(entries)
    return entries


def set_mode(paid: bool, confirm: str | None, busy: list[str]) -> dict:
    """Ücretli moda geçiş açık onay ister ve üretim sürerken yapılmaz; ücretsiz moda dönüş her zaman serbesttir."""
    path = env_path()
    if not path:
        raise ConfigError("Ayar dosyası bağlı değil (CONFIG_ENV_FILE boş); mod panelden değiştirilemez.")
    if paid:
        if (confirm or "").strip().casefold() != PAID_CONFIRMATION.casefold():
            raise ConfigError(f"Onay için kutuya tam olarak \"{PAID_CONFIRMATION}\" yazın.")
        if busy:
            raise ConfigError(f"Üretim sürerken ücretli moda geçilmez; önce bitmesini bekleyin ya da durdurun: {', '.join(busy[:8])}.")
    if settings.dry_run == (not paid):
        return {"dry_run": settings.dry_run, "changed": False}
    write_env(path, {"DRY_RUN": "0" if paid else "1"})
    bump([{"field": "DRY_RUN", "action": "mode", "detail": "ücretli moda geçildi" if paid else "ücretsiz moda dönüldü"}])
    return {"dry_run": settings.dry_run, "changed": True}
