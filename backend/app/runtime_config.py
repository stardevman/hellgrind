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
    service: str                # runpod | vast | fal | elevenlabs | anthropic | core
    label: str
    choices: tuple[str, ...] = ()
    url: str | None = None      # model sayfası (doğrulamak için)
    minimum: float | None = None   # sayılar: izin verilen en küçük değer (yoksa sıfırdan büyük)


FIELDS: tuple[Field, ...] = (
    Field("DRY_RUN", "dry_run", "mode", "core", "Ücretsiz mod"),
    Field("RUNPOD_API_KEY", "runpod_api_key", "secret", "runpod", "RunPod API anahtarı"),
    Field("H3_PLATFORM", "h3_platform", "choice", "runpod",
          "Platform: kapali (varsayılan; RunPod uç noktası silindi, video fal.ai'de), runpod, vast (deneysel)",
          ("kapali", "runpod", "vast")),
    Field("RUNPOD_ENDPOINT_ID", "runpod_endpoint_id", "text", "runpod", "Serverless uç nokta kimliği (adı değil)"),
    Field("RUNPOD_NETWORK_VOLUME_ID", "runpod_network_volume_id", "text", "runpod", "Network Volume kimliği (modeller; adı değil)"),
    Field("RUNPOD_DATACENTER", "runpod_datacenter", "choice", "runpod",
          "Veri merkezi (volume'un merkezi; EUR-* yalnızca Kuzey izni açıkken)",
          ("CA-MTL-1", "CA-MTL-2", "CA-MTL-3", "EUR-NO-1", "EUR-IS-1", "EUR-IS-2", "EUR-IS-3")),
    Field("RUNPOD_GPU_TYPES", "runpod_gpu_types", "text", "runpod", "GPU tipleri (virgülle; RTX 5090, RTX PRO 6000)"),
    Field("RUNPOD_GPU_POOLS", "runpod_gpu_pools", "choice", "runpod",
          "Uç noktaya yazılan GPU havuzları (auto: GPU tiplerinden; ADA_32_PRO = RTX 5090, BLACKWELL_96 = RTX PRO 6000)",
          ("auto", "ADA_32_PRO", "BLACKWELL_96", "ADA_32_PRO,BLACKWELL_96")),
    Field("RUNPOD_WORKERS_MAX", "runpod_workers_max", "choice", "runpod",
          "En çok worker (aynı anda çalışan GPU): keep uç noktadaki değeri değiştirmez; 1-3 'Ayarları uç noktaya uygula' ile "
          "yazılır (projenin bekleyen iş sınırı RUNPOD_MAX_PENDING_JOBS bundan küçük olmamalı)", ("keep", "1", "2", "3")),
    Field("RUNPOD_IDLE_TIMEOUT_S", "runpod_idle_timeout_s", "number", "runpod",
          "Boşta bekleme (sn; son işten sonra worker açık kalır ve faturalanır)", minimum=5),
    Field("RUNPOD_REVIEW_IDLE_S", "runpod_review_idle_s", "number", "runpod",
          "Storyboard şeridi incelemesinde boşta bekleme (sn; kare işinden sonra 15 dk geçerli, art arda yeniden çizimler "
          "soğuk başlatma ödemesin; 0: kapalı)", minimum=0),
    Field("RUNPOD_MIN_BALANCE_USD", "runpod_min_balance_usd", "number", "runpod",
          "Bakiye eşiği ($; altında yeni iş başlamaz)", minimum=1),
    Field("RUNPOD_MAX_PENDING_JOBS", "runpod_max_pending_jobs", "number", "runpod",
          "Projede aynı anda en çok bekleyen RunPod işi (fazlası yer açılana dek gönderilmez)", minimum=1),
    Field("H3_ALLOW_NORDIC", "h3_allow_nordic", "choice", "runpod",
          "Kuzey veri merkezleri de izinli (RunPod EUR-NO-1, EUR-IS-*; vast.ai Norveç, İzlanda). Varsayılan: yalnızca Kanada",
          ("false", "true")),
    Field("H3_UNET_VARIANT", "h3_unet_variant", "choice", "runpod",
          "H3 DiT türü: fp8_scaled (cu128 worker) ya da int8_convrot (cu130 torch'lu worker gerekir)",
          ("fp8_scaled", "int8_convrot")),
    Field("KEYFRAME_CHARACTER_PROVIDER", "keyframe_character_provider", "choice", "runpod",
          "Karakterli çekimin ilk karesi: qwen_edit (Qwen-Image-Edit-2511, karakterin onaylı referansından) ya da qwen "
          "(Qwen-Image, yalnızca metinden; kimlik kayabilir)", ("qwen_edit", "qwen")),
    Field("QWEN_EDIT_ANGLES", "qwen_edit_angles", "choice", "runpod",
          "Multiple Angles LoRA (kesmede açı değişimi; dosya volume'da models/loras/ altında olmalı)", ("off", "on")),
    Field("QWEN_EDIT_ANGLES_LORA", "qwen_edit_angles_lora", "model", "runpod", "Multiple Angles LoRA dosya adı",
          url="https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA"),
    Field("QWEN_EDIT_ANGLES_STRENGTH", "qwen_edit_angles_strength", "number", "runpod",
          "Multiple Angles LoRA gücü (önerilen 0,8-1,0)", minimum=0.1),
    Field("QWEN_EDIT_ANGLES_MODE", "qwen_edit_angles_mode", "choice", "runpod",
          "Açı karesinin kalitesi (Lightning ile uyumu bilinmiyor: ilk koşuda karşılaştırın)", ("full", "medium", "lightning")),
    Field("QWEN_EDIT_MODE", "qwen_edit_mode", "choice", "runpod",
          "Qwen-Image-Edit kalitesi (karakterli ilk kare): full 40 adım cfg 4 (~195 sn), medium 20 adım cfg 4 (~100 sn), "
          "lightning 4 adım cfg 1 (~15 sn). Proje ayarı (Senaryo › Yoğunluk) bunun önüne geçer", ("full", "medium", "lightning")),
    Field("H3_KEYFRAME_REFERENCE", "h3_keyframe_reference", "choice", "runpod",
          "fal.ai kancası: flux_kontext ise ilk kare karakter referansından Flux Kontext ile çizilir (görsel başına ücretli; "
          "Qwen'in önüne geçer). Varsayılan off", ("off", "flux_kontext")),
    Field("H3_MODEL", "h3_model", "model", "runpod", "Model", url="https://huggingface.co/{}"),
    Field("H3_MODEL_REVISION", "h3_model_revision", "text", "runpod", "Model sürümü (klip meta verisine yazılır)"),
    Field("H3_STEPS", "h3_steps", "choice", "runpod", "Adım sayısı (sunucudaki kurulumla aynı)", ("50", "20", "9", "8", "5")),
    Field("VAST_API_KEY", "vast_api_key", "secret", "vast", "vast.ai API anahtarı"),
    Field("VAST_INSTANCE_ID", "vast_instance_id", "text", "vast", "Makine kimliği (instance ID)"),
    Field("H3_BACKEND", "h3_backend", "choice", "vast", "Arka uç (sglang: deneysel)", ("comfyui", "sglang")),
    Field("H3_ENDPOINT_URL", "h3_endpoint_url", "url", "vast", "Sunucu adresi (SGLang FL2VA ya da ComfyUI)"),
    Field("H3_REF_ENDPOINT_URL", "h3_ref_endpoint_url", "url", "vast", "Ref2VA sunucusu (SGLang; ComfyUI'de boş: aynı sunucu)"),
    Field("H3_API_KEY", "h3_api_key", "secret", "vast", "Portal belirteci (isteğe bağlı; SSH tünelinde gerekmez)"),
    Field("H3_IDLE_STOP_MINUTES", "h3_idle_stop_minutes", "choice", "vast", "Boşta durdurma (dakika; 0: kapalı)",
          ("0", "5", "10", "15", "30", "60")),
    Field("H3_AUTO_START", "h3_auto_start", "choice", "vast", "İş gelince durmuş makineyi başlat", ("false", "true")),
    Field("H3_TLS_MODE", "h3_tls_mode", "choice", "vast",
          "Sunucu sertifikası: verify (doğrula), pin (ilk bağlantıda parmak izini sabitle; yeni makine), off (doğrulama yok)",
          ("verify", "pin", "off")),
    Field("H3_REPLACE_MODE", "h3_replace_mode", "choice", "vast",
          "Yedek makine (otomatik başlatma başarısız olursa): off, ask (her zaman sor), auto (tavan altındaysa)",
          ("ask", "auto", "off")),
    Field("H3_MAX_DPH", "h3_max_dph", "number", "vast", "Yeni makine fiyat tavanı ($/sa)"),
    Field("H3_MIN_RAM_GB", "h3_min_ram_gb", "number", "vast",
          "Yeni makine en az RAM (GB; önerilen 128, en az 96: yalnızca H3 ~94 GB kullandı)", minimum=96),
    Field("H3_MIN_DISK_GB", "h3_min_disk_gb", "number", "vast", "Yeni makine disk (GB; en az 150: modeller ~105 GB)",
          minimum=150),
    Field("H3_MIN_INET_MBPS", "h3_min_inet_mbps", "number", "vast",
          "Yeni makine en az indirme hızı (Mbps; 1000 = 1 Gbps)", minimum=100),
    Field("H3_GPU_NAME", "h3_gpu_name", "text", "vast", "Yeni makinenin GPU'su (boş: eskisiyle aynı; ilk makinede RTX 5090)"),
    Field("H3_IMAGE_MODEL", "h3_image_model", "choice", "vast", "Storyboard görsel modeli (setup.sh kurar; Apache 2.0)",
          ("qwen-image-2512", "flux-schnell", "none")),
    Field("H3_BOX_IMAGE", "h3_box_image", "text", "vast", "Yeni makinenin Docker imajı"),
    Field("H3_SETUP_SCRIPT_URL", "h3_setup_script_url", "url", "vast", "setup.sh adresi (boş: isteğe gömülür)"),
    Field("FAL_KEY", "fal_key", "secret", "fal", "API anahtarı"),
    Field("REFERENCE_FAL_FALLBACK", "reference_fal_fallback", "choice", "fal",
          "Referans görseli: RunPod'da Qwen-Image yoksa Flux'a düş (FLUX.1 [dev] ağırlıkları ticari olmayan lisanslı; "
          "varsayılan false)", ("false", "true")),
    Field("FAL_FLUX_ENDPOINT", "fal_flux_endpoint", "model", "fal", "İlk kare ve storyboard (Flux LoRA)", url="https://fal.ai/models/{}"),
    Field("FAL_WAN_ENDPOINT", "fal_wan_endpoint", "model", "fal", "Video: Wan 2.2 (yalnızca eski çekimler)", url="https://fal.ai/models/{}"),
    Field("VIDEO_INPUT_MODE", "video_input_mode", "choice", "fal",
          "Video girdisi: reference (yalnızca onaylı referans görselleri + senaryo; hiçbir kare ya da görsel üretilmez) ya da "
          "keyframe (storyboard karesinden; Qwen)", ("reference", "keyframe")),
    Field("SCENE_RENDER_MODE", "scene_render_mode", "choice", "fal",
          "Sahne üretimi: whole_scene (bütün çekimler tek istekte; H3 Max 15 sn, Seedance 2.5 30 sn sınırında parçalara "
          "bölünür) ya da per_shot (her çekim ayrı istek)", ("whole_scene", "per_shot")),
    Field("VIDEO_DRAFT_ENGINE", "video_draft_engine", "choice", "fal",
          "Taslak video motoru (yeni çekimler; proje ayarı önce gelir)", ("h3max", "seedance25", "seedance", "h3")),
    Field("VIDEO_FINAL_ENGINE", "video_final_engine", "choice", "fal",
          "Final video motoru (\"yükselt\")", ("seedance25", "h3max", "seedance")),
    Field("FAL_H3MAX_I2V_ENDPOINT", "fal_h3max_i2v_endpoint", "model", "fal", "H3 Max image-to-video",
          url="https://fal.ai/models/{}"),
    Field("FAL_H3MAX_REF_ENDPOINT", "fal_h3max_ref_endpoint", "model", "fal", "H3 Max reference-to-video",
          url="https://fal.ai/models/{}"),
    Field("H3MAX_RESOLUTION", "h3max_resolution", "choice", "fal",
          "H3 Max çözünürlüğü (reference-to-video 480P 0,05 $/sn, 768P 0,08, 1080P 0,16; image-to-video 0,03 / 0,048 / 0,096)",
          ("480P", "768P", "1080P")),
    Field("H3MAX_PROMPT_EXPANSION", "h3max_prompt_expansion", "choice", "fal",
          "H3 Max istem genişletme (zorunlu alan; disabled: istem olduğu gibi)", ("disabled", "balanced", "quality")),
    Field("FAL_SEEDANCE25_I2V_ENDPOINT", "fal_seedance25_i2v_endpoint", "model", "fal", "Seedance 2.5 image-to-video",
          url="https://fal.ai/models/{}"),
    Field("FAL_SEEDANCE25_REF_ENDPOINT", "fal_seedance25_ref_endpoint", "model", "fal", "Seedance 2.5 reference-to-video",
          url="https://fal.ai/models/{}"),
    Field("FAL_SEEDANCE25_DRAFT_COMPLETE_ENDPOINT", "fal_seedance25_draft_complete_endpoint", "model", "fal",
          "Seedance 2.5 taslağı 1080p'ye tamamlama", url="https://fal.ai/models/{}"),
    Field("SEEDANCE25_DRAFT", "seedance25_draft", "choice", "fal",
          "Seedance 2.5 taslak (true: 480p önizleme + draft_id; 7 gün içinde 1080p'ye tamamlanır)", ("true", "false")),
    Field("SEEDANCE25_RESOLUTION", "seedance25_resolution", "choice", "fal",
          "Seedance 2.5 çözünürlüğü, yalnızca taslak kapalıyken (720p ≈ 0,47 $/sn; tahmine %25 pay)", ("720p", "480p", "1080p")),
    Field("SEEDANCE25_AUDIO", "seedance25_audio", "choice", "fal",
          "Seedance 2.5 model sesi (filmde kullanılmaz; fiyatı değiştirmez)", ("false", "true")),
    Field("UPSCALE_ENDPOINT", "upscale_endpoint", "choice", "fal",
          "Upscale (1080p): bytedance-upscaler 0,0072 $/sn; kandinsky6-vsr, seedvr, flashvsr megapiksel başına; flux-video-upscale 0,14 $/sn",
          ("fal-ai/bytedance-upscaler/upscale/video", "fal-ai/kandinsky6-vsr", "fal-ai/seedvr/upscale/video",
           "fal-ai/flashvsr/upscale/video", "blackforestlabs/flux-video-upscale")),
    Field("UPSCALE_TIER", "upscale_tier", "choice", "fal", "Bytedance upscaler kademesi (pro 10 kat fiyat)",
          ("standard", "fast", "pro")),
    Field("KEYFRAME_PLATFORM", "keyframe_platform", "choice", "fal",
          "Kare çizimi: fal (Qwen-Image / Qwen-Image-Edit fal.ai'de) ya da runpod (ComfyUI worker'ı)", ("fal", "runpod")),
    Field("FAL_QWEN_IMAGE_ENDPOINT", "fal_qwen_image_endpoint", "model", "fal", "Kare: metinden (Qwen-Image-2512)",
          url="https://fal.ai/models/{}"),
    Field("FAL_QWEN_EDIT_ENDPOINT", "fal_qwen_edit_endpoint", "model", "fal", "Kare: referanstan ve zincir (Qwen-Image-Edit-2511)",
          url="https://fal.ai/models/{}"),
    Field("FAL_QWEN_EDIT_LORA_ENDPOINT", "fal_qwen_edit_lora_endpoint", "model", "fal",
          "Kare: açı değişimi (Qwen-Image-Edit-2511 + Multiple Angles LoRA)", url="https://fal.ai/models/{}"),
    Field("QWEN_EDIT_ANGLES_LORA_URL", "qwen_edit_angles_lora_url", "url", "fal", "Multiple Angles LoRA adresi (fal loras[].path)"),
    Field("FAL_SEEDANCE_ENDPOINT", "fal_seedance_endpoint", "model", "fal", "Video: Seedance 2.0 fast (yönetmenin seçtiği çekimler)", url="https://fal.ai/models/{}"),
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
        if f.minimum is not None and float(value) < f.minimum:
            raise ConfigError(f"{f.label}: en az {f.minimum:g} olmalı")
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
