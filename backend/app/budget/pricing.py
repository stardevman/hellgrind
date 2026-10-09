import math
from functools import lru_cache

import yaml

from app.config import settings


@lru_cache(maxsize=1)
def table(path: str | None = None) -> dict:
    with open(path or settings.pricing_file) as f:
        return yaml.safe_load(f)


def self_hosted(engine: str, prices: dict | None = None) -> dict | None:
    """Yönetmenin kendi GPU sunucusunda çalışan motor (ör. vast.ai'de MiniMax H3): saatlik ücret ve video saniyesi başına
    GPU süresi. Diğer motorlar sağlayıcının video saniyesi fiyatıyla hesaplanır."""
    return ((prices or table()).get("self_hosted") or {}).get(engine)


def serverless(prices: dict | None = None) -> dict | None:
    """RunPod Serverless (MiniMax H3'ün varsayılan platformu): GPU tipine göre saniye ücreti, video saniyesi başına GPU
    süresi, ilk kare ve soğuk başlatma süreleri."""
    return ((prices or table()).get("serverless") or {}).get("runpod")


def _runpod(engine: str, prices: dict | None) -> dict | None:
    if engine != "h3" or settings.h3_platform != "runpod":
        return None
    return serverless(prices)


def _squash(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def runpod_rate(gpu: str | None = None, prices: dict | None = None) -> float:
    """GPU tipinin saniye ücreti. Ad ya da RunPod havuz kimliği (ADA_32_PRO, BLACKWELL_96) kabul edilir. Bilinmiyorsa
    uç noktanın birincil GPU tipi (Ayarlar'daki ilk GPU; varsayılan RTX 5090): işler çoğunlukla orada çalışır, ölçülen
    bedele yakın tahmin. "NVIDIA GeForce RTX 5090" -> "RTX 5090"; "NVIDIA RTX PRO 6000 Blackwell ..." -> "RTX PRO 6000"."""
    table_ = serverless(prices)
    rates, pools = table_["per_second"], table_.get("pools") or {}
    if gpu and gpu in pools:
        gpu = pools[gpu]
    for name, rate in sorted(rates.items(), key=lambda kv: -len(kv[0])):
        if gpu and _squash(name) in _squash(gpu):
            return float(rate)
    if gpu is None:
        primary = (settings.runpod_gpu_types or "").split(",")[0].strip()
        if primary and primary != gpu:
            for name, rate in rates.items():
                if _squash(name) == _squash(pools.get(primary, primary)):
                    return float(rate)
    return float(max(rates.values()))


def render_rows(rp: dict, mode: str | None) -> dict:
    """H3 koşuluna göre video saniyesi başına GPU saniyesi (t2va FL2VA DiT'iyle çalışır). Eski düz biçim de okunur."""
    rows = rp["render_seconds"]
    if rows and all(isinstance(v, dict) for v in rows.values()):
        return rows.get("ref2va" if mode == "ref2va" else "fl2va") or next(iter(rows.values()))
    return rows


def estimate_runpod(seconds: float, gpu: str | None = None, prices: dict | None = None) -> float:
    """RunPod worker süresinin bedeli: saniye ücreti × süre (soğuk başlatma ve boşta bekleme dahil, ne kadarsa)."""
    return round(runpod_rate(gpu, prices) * max(0.0, seconds), 4)


def estimate_video(engine: str, resolution: str, duration_s: float, prices: dict | None = None, mode: str | None = None,
                   cold: bool = False) -> float:
    """mode: H3 koşulu (fl2va | ref2va | t2va). cold: worker soğuk (açılış + model yükleme bu işe eklenir)."""
    prices = prices or table()
    if (rp := _runpod(engine, prices)) is not None:            # birincil GPU tipiyle; işlenen tutar gerçek süreyle
        rows = render_rows(rp, mode)
        seconds = rows.get(resolution, max(rows.values())) * duration_s
        return estimate_runpod(seconds + (float(rp.get("cold_start_seconds", 0)) if cold else 0.0), None, prices)
    if (hosted := self_hosted(engine, prices)) is not None:
        rows = hosted["render_seconds"]
        gpu_seconds = rows.get(resolution, max(rows.values())) * duration_s      # bilinmeyen çözünürlük: en yavaşı
        return estimate_gpu(engine, gpu_seconds, prices)
    per_engine = prices["video_per_second"][engine]
    per_second = per_engine.get(resolution, max(per_engine.values()))  # bilinmeyen çözünürlük: en pahalısı
    return round(per_second * duration_s, 4)


EDIT_MODE_ROWS = {"full": "keyframe_edit_seconds", "medium": "keyframe_edit_medium_seconds",
                  "lightning": "keyframe_edit_lightning_seconds"}
EDIT_MODE_SHARE = {"full": 1.0, "medium": 0.5, "lightning": 0.15}   # satırı olmayan eski tablo: tam modun payı


def keyframe_seconds(rp: dict, model: str = "qwen", cold: bool = False, edit_mode: str = "full") -> float:
    """İlk karenin faturalanan worker süresi. model: qwen (Qwen-Image, metinden) ya da qwen_edit (Qwen-Image-Edit-2511,
    karakter referansından; edit_mode full | medium | lightning, her birinin kendi satırı; tabloda yoksa tam modun payı,
    o da yoksa Qwen-Image'ınki). Soğuk worker'da açılış + model yükleme farkı eklenir (keyframe_cold_seconds -
    keyframe_seconds; ikisi de aynı boyda Qwen modelleri)."""
    warm = float(rp.get("keyframe_seconds", 10))
    if model == "qwen_edit":
        mode = edit_mode if edit_mode in EDIT_MODE_ROWS else "full"
        full = float(rp.get("keyframe_edit_seconds", warm))
        seconds = float(rp.get(EDIT_MODE_ROWS[mode], full * EDIT_MODE_SHARE[mode]))
    else:
        seconds = warm
    return seconds + (max(0.0, float(rp.get("keyframe_cold_seconds", warm)) - warm) if cold else 0.0)


def estimate_keyframe(engine: str = "h3", prices: dict | None = None, cold: bool = False, model: str = "qwen",
                      edit_mode: str = "full") -> float:
    """H3 platformunda (ComfyUI iş akışıyla) çizilen tek ilk karenin tahmini bedeli; cold: soğuk worker."""
    if (rp := _runpod(engine, prices)) is not None:
        return estimate_runpod(keyframe_seconds(rp, model, cold, edit_mode), None, prices)
    return estimate_gpu(engine, self_hosted(engine, prices).get("keyframe_seconds", 10), prices)


def estimate_gpu(engine: str, gpu_seconds: float, prices: dict | None = None) -> float:
    """Kendi sunucusunda geçen GPU süresinin bedeli: saatlik ücret × süre. Ücretli modda işlenen tutar budur (sunucunun
    bildirdiği süreyle); makinenin boşta geçen süresi bütçeye yansımaz."""
    return round(self_hosted(engine, prices)["gpu_hourly_usd"] / 3600.0 * max(0.0, gpu_seconds), 4)


def estimate_lipsync(duration_s: float, prices: dict | None = None) -> float:
    return round((prices or table())["lipsync_per_minute"] * duration_s / 60.0, 4)


def estimate_image(megapixels: float, prices: dict | None = None) -> float:
    """Flux LoRA: megapiksel başına, yukarı yuvarlanır (en az 1 MP) — tahmin bilerek tutucudur."""
    return round(math.ceil(megapixels) * (prices or table())["image_per_megapixel"]["flux_lora"], 4)


def estimate_reference_image(prices: dict | None = None) -> float:
    """Flux Kontext: referans görselden bir kare, görsel başına."""
    return round((prices or table())["image_per_image"]["flux_kontext"], 4)


def estimate_frame(reference: bool, megapixels: float = 1.0, prices: dict | None = None) -> float:
    """İlk kare ya da storyboard karesi: karakter referansı varsa Kontext, yoksa Flux LoRA."""
    return estimate_reference_image(prices) if reference else estimate_image(megapixels, prices)


def estimate_tts(text: str, prices: dict | None = None) -> float:
    return round(len(text or "") / 1000.0 * (prices or table())["tts_per_1k_chars"], 4)


def estimate_llm(input_tokens: int, output_tokens: int, prices: dict | None = None) -> float:
    rate = (prices or table())["llm_per_million_tokens"]
    return round((input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000, 6)


# ---------- panelden düzenleme ----------

REQUIRED_ENGINES = ("fal_wan", "seedance", "wan_v2v")      # kodun fiyat sorduğu video motorları
REQUIRED_SELF_HOSTED = ("h3",)                             # kendi sunucusunda çalışan motorlar (saatlik GPU ücreti)


class PricingError(ValueError):
    """Fiyat tablosu geçersiz (kullanıcıya Türkçe gösterilir)."""


def _positive(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise PricingError(f"{where}: sıfırdan büyük bir sayı olmalı (gelen: {value!r})")
    return float(value)


def validate_table(text: str) -> dict:
    """YAML'ı okur ve kodun kullandığı her fiyatın var ve pozitif olduğunu denetler. Yorumlar dosyada korunur."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PricingError(f"YAML okunamadı: {e}") from e
    if not isinstance(data, dict):
        raise PricingError("Fiyat tablosu bir sözlük olmalı")
    video = data.get("video_per_second")
    if not isinstance(video, dict):
        raise PricingError("video_per_second eksik")
    for engine in REQUIRED_ENGINES:
        if not isinstance(video.get(engine), dict) or not video[engine]:
            raise PricingError(f"video_per_second.{engine} eksik (çözünürlük: fiyat)")
    for engine, rows in video.items():
        if not isinstance(rows, dict):
            raise PricingError(f"video_per_second.{engine}: çözünürlük -> fiyat sözlüğü olmalı")
        for res, price in rows.items():
            _positive(price, f"video_per_second.{engine}.{res}")
    hosted = data.get("self_hosted") or {}
    if not isinstance(hosted, dict):
        raise PricingError("self_hosted: motor -> {gpu_hourly_usd, render_seconds} sözlüğü olmalı")
    for engine in REQUIRED_SELF_HOSTED:
        if not isinstance(hosted.get(engine), dict):
            raise PricingError(f"self_hosted.{engine} eksik (gpu_hourly_usd ve render_seconds)")
    for engine, row in hosted.items():
        if not isinstance(row, dict):
            raise PricingError(f"self_hosted.{engine}: gpu_hourly_usd ve render_seconds içeren bir sözlük olmalı")
        _positive(row.get("gpu_hourly_usd"), f"self_hosted.{engine}.gpu_hourly_usd")
        seconds = row.get("render_seconds")
        if not isinstance(seconds, dict) or not seconds:
            raise PricingError(f"self_hosted.{engine}.render_seconds eksik (çözünürlük: video saniyesi başına GPU saniyesi)")
        for res, value in seconds.items():
            _positive(value, f"self_hosted.{engine}.render_seconds.{res}")
        if "keyframe_seconds" in row:
            _positive(row["keyframe_seconds"], f"self_hosted.{engine}.keyframe_seconds")
    rp = (data.get("serverless") or {}).get("runpod")
    if rp is not None:
        if not isinstance(rp, dict) or not isinstance(rp.get("per_second"), dict) or not rp["per_second"]:
            raise PricingError("serverless.runpod.per_second eksik (GPU tipi: saniye ücreti)")
        for gpu, rate in rp["per_second"].items():
            _positive(rate, f"serverless.runpod.per_second.{gpu}")
        seconds = rp.get("render_seconds")
        if not isinstance(seconds, dict) or not seconds:
            raise PricingError("serverless.runpod.render_seconds eksik (koşul -> çözünürlük -> video saniyesi başına GPU saniyesi)")
        for key, value in seconds.items():
            if isinstance(value, dict):
                for res, n in value.items():
                    _positive(n, f"serverless.runpod.render_seconds.{key}.{res}")
            else:
                _positive(value, f"serverless.runpod.render_seconds.{key}")
        for gpu in (rp.get("pools") or {}).values():
            if gpu not in rp["per_second"]:
                raise PricingError(f"serverless.runpod.pools: {gpu} için per_second satırı yok")
        for name in ("keyframe_seconds", "keyframe_cold_seconds", *EDIT_MODE_ROWS.values(), "cold_start_seconds"):
            if name in rp:
                _positive(rp[name], f"serverless.runpod.{name}")
    _positive((data.get("image_per_megapixel") or {}).get("flux_lora"), "image_per_megapixel.flux_lora")
    _positive((data.get("image_per_image") or {}).get("flux_kontext"), "image_per_image.flux_kontext")
    _positive(data.get("lipsync_per_minute"), "lipsync_per_minute")
    _positive(data.get("tts_per_1k_chars"), "tts_per_1k_chars")
    llm = data.get("llm_per_million_tokens") or {}
    _positive(llm.get("input"), "llm_per_million_tokens.input")
    _positive(llm.get("output"), "llm_per_million_tokens.output")
    return data


def save_table(text: str) -> dict:
    """Doğrular ve dosyayı yerinde yazar (bağlı tek dosya yeniden adlandırılamaz); önbelleği boşaltır."""
    data = validate_table(text)
    try:
        with open(settings.pricing_file, "r+", encoding="utf-8") as f:
            f.seek(0)
            f.write(text if text.endswith("\n") else text + "\n")
            f.truncate()
    except OSError as e:
        raise PricingError(f"Fiyat dosyasına yazılamadı ({e.strerror}): {settings.pricing_file}") from e
    table.cache_clear()
    return data
