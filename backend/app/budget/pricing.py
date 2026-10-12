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
        chosen = (settings.runpod_gpu_pools or "auto").split(",")[0].strip()
        primary = chosen if chosen and chosen != "auto" else (settings.runpod_gpu_types or "").split(",")[0].strip()
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


# ---------- fal.ai video motorları (MiniMax H3 Max, Seedance 2.5) ----------

FAL_VIDEO_ENGINES = ("h3max", "seedance25")


def fal_video(engine: str, prices: dict | None = None) -> dict | None:
    return ((prices or table()).get("fal_video") or {}).get(engine)


def h3max_cost(resolution: str, duration_s: float, images: int = 0, prices: dict | None = None, mode: str = "ref") -> dict:
    """MiniMax H3 Max: istenen süre × saniye ücreti (480P 0,05 / 768P 0,08 / 1080P 0,16) + referans token fazlası (her
    girdi görseli 1.024 token; 4.096 token dahil, fazlası 0,02 $ / 1000), × billing_factor (ölçülen fatura). Bilinmeyen
    çözünürlük: en pahalısı."""
    row = fal_video("h3max", prices)
    rates = row.get("per_second_i2v") if mode == "i2v" and row.get("per_second_i2v") else row["per_second"]
    rate = float(rates.get(resolution, max(rates.values())))
    tokens = int(images) * int(row.get("image_tokens", 1024)) if mode != "i2v" else 0   # token fazlası: reference-to-video
    extra_tokens = max(0, tokens - int(row.get("included_reference_tokens", 4096)))
    base = rate * float(duration_s) + extra_tokens / 1000.0 * float(row.get("extra_per_1k_tokens", 0.02))
    factor = float(row.get("billing_factor", 1.0))
    usd = round(base * factor, 4)
    return {"engine": "h3max", "mode": mode, "resolution": resolution, "duration_s": float(duration_s), "per_second_usd": rate,
            "images": int(images), "reference_tokens": tokens, "extra_tokens": extra_tokens, "base_usd": round(base, 4),
            "billing_factor": factor, "usd": usd, "reserve_usd": usd, "units": round(usd / 0.05, 2)}


def seedance25_tokens(resolution: str, out_seconds: float, in_video_seconds: float = 0.0, prices: dict | None = None,
                      size: tuple[int, int] | None = None) -> float:
    row = fal_video("seedance25", prices)
    width, height = size or row["size"].get(resolution) or max(row["size"].values(), key=lambda wh: wh[0] * wh[1])
    return height * width * (float(in_video_seconds) + float(out_seconds)) * int(row.get("fps", 24)) / \
        float(row.get("token_divisor", 1024))


def seedance25_cost(resolution: str, out_seconds: float, in_video_seconds: float = 0.0, prices: dict | None = None,
                    size: tuple[int, int] | None = None, measured: bool = False) -> dict:
    """Seedance 2.5: token = yükseklik × genişlik × (girdi videosu sn + çıktı sn) × 24 / 1024; 480p/720p 0,0214 $,
    1080p 0,0234 $ / 1000 token; video referansı varsa × 0,6 (görsel ve ses referansı ücretsiz). Rezervasyon tahmine
    %25 pay ekler (reserve_usd); measured: çıktının ölçülen boyu ve süresiyle işlenen tutar (× billing_factor)."""
    row = fal_video("seedance25", prices)
    rates = row["per_1k_tokens"]
    rate = float(rates.get(resolution, max(rates.values())))
    if in_video_seconds:
        rate *= float(row.get("video_reference_factor", 0.6))
    tokens = seedance25_tokens(resolution, out_seconds, in_video_seconds, prices, size)
    base = tokens / 1000.0 * rate
    margin, factor = float(row.get("margin", 0.25)), float(row.get("billing_factor", 1.0))
    return {"engine": "seedance25", "resolution": resolution, "duration_s": float(out_seconds),
            "input_video_s": float(in_video_seconds), "tokens": round(tokens), "per_1k_tokens_usd": rate,
            "base_usd": round(base, 4), "margin": margin, "billing_factor": factor,
            "usd": round(base * (factor if measured else 1 + margin), 4), "reserve_usd": round(base * (1 + margin), 4),
            "billed_tokens": round(tokens * factor) if measured else None}


def seedance25_complete_cost(out_seconds: float, prices: dict | None = None) -> dict:
    """draft/complete: taslağın YALNIZCA 1080p tamamlanması, 0,0234 $ / 1000 token (≈ 1,164 $/sn), tahmine %20 pay."""
    row = (fal_video("seedance25", prices) or {}).get("complete") or {}
    width, height = row.get("size") or (1920, 1104)
    tokens = height * width * float(out_seconds) * 24 / 1024
    base = tokens / 1000.0 * float(row.get("per_1k_tokens", 0.0234))
    margin = float(row.get("margin", 0.2))
    return {"engine": "seedance25", "resolution": "1080p", "duration_s": float(out_seconds), "tokens": round(tokens),
            "per_1k_tokens_usd": float(row.get("per_1k_tokens", 0.0234)), "base_usd": round(base, 4), "margin": margin,
            "usd": round(base, 4), "reserve_usd": round(base * (1 + margin), 4)}


def fal_video_cost(engine: str, resolution: str, duration_s: float, images: int = 0, in_video_seconds: float = 0.0,
                   prices: dict | None = None, mode: str = "ref", draft: bool = False) -> dict:
    """Çağrıdan önceki tahmin (bütçe kapısı): {"usd", "reserve_usd", ...}. mode: H3 Max uç noktası (i2v | ref);
    draft: Seedance 2.5 taslağı (480p fiyatı)."""
    if engine == "h3max":
        return h3max_cost(resolution, duration_s, images, prices, mode)
    if engine == "seedance25":
        if draft:
            resolution = (fal_video("seedance25", prices) or {}).get("draft_resolution", "480p")
        return {**seedance25_cost(resolution, duration_s, in_video_seconds, prices), "draft": draft}
    raise KeyError(engine)


UPSCALE_SIZE = (1920, 1080)


def upscale_cost(endpoint: str, seconds: float, fps: float = 24.0, target: str = "1080p", tier: str = "standard",
                 prices: dict | None = None) -> dict:
    """Upscale tahmini (çıktı 1920x1080, kaynak fps). Bilinmeyen uç nokta: KeyError."""
    rows = (prices or table()).get("upscale") or {}
    row = rows[endpoint]
    mp = UPSCALE_SIZE[0] * UPSCALE_SIZE[1] / 1_000_000
    frames = float(seconds) * float(fps)
    if "per_second" in row:
        rate = float(row["per_second"].get(target, max(row["per_second"].values())))
        if fps > 30:
            rate *= float(row.get("high_fps_factor", 2))
        if tier == "pro":
            rate *= float(row.get("pro_factor", 10))
        usd = rate * float(seconds)
    elif "per_mp_frame" in row:
        usd = float(row["per_mp_frame"]) * mp * frames
    else:
        usd = float(row["per_mp"]) * mp * frames
    out = {"endpoint": endpoint, "seconds": float(seconds), "fps": float(fps), "target": target, "tier": tier,
           "usd": round(usd, 4), "reserve_usd": round(usd, 4)}
    if row.get("max_seconds") and seconds > float(row["max_seconds"]) + 1e-6:
        out["warning"] = f"{endpoint} en çok {row['max_seconds']} sn işler (klip {seconds:g} sn)"
    return out


def estimate_fal_image(model: str, width: int = 1280, height: int = 704, prices: dict | None = None) -> float:
    """fal.ai görsel modeli (qwen_image | qwen_edit | qwen_edit_lora): megapiksel başına, yukarı yuvarlanır (en az 1)."""
    rows = (prices or table()).get("fal_image") or {}
    per_mp = float(rows.get(model) or rows.get("qwen_edit") or 0.03)
    return round(math.ceil(width * height / 1_000_000 - 1e-9) * per_mp, 4)


def estimate_video(engine: str, resolution: str, duration_s: float, prices: dict | None = None, mode: str | None = None,
                   cold: bool = False) -> float:
    """mode: H3 koşulu (fl2va | ref2va | t2va). cold: worker soğuk (açılış + model yükleme bu işe eklenir)."""
    prices = prices or table()
    if engine in FAL_VIDEO_ENGINES and fal_video(engine, prices) is not None:
        from app.engine.fal_plan import billed_seconds, norm_resolution    # tur çözünürlüğü -> motorun değeri
        return fal_video_cost(engine, norm_resolution(engine, resolution), billed_seconds(engine, duration_s),
                              prices=prices)["reserve_usd"]
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
                      edit_mode: str = "full", angles: bool = False) -> float:
    """Tek ilk karenin tahmini bedeli. KEYFRAME_PLATFORM=fal: fal.ai'de Qwen-Image-2512 / Qwen-Image-Edit-2511 (açı
    LoRA'sıyla /lora), megapiksel başına (kalite modu fiyatı değiştirmez). Değilse H3 platformunda (ComfyUI iş akışıyla);
    cold: soğuk worker."""
    if settings.keyframe_platform == "fal" and (prices or table()).get("fal_image"):
        name = "qwen_image" if model == "qwen" else "qwen_edit_lora" if angles else "qwen_edit"
        return estimate_fal_image(name, prices=prices)
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


def llm_rate(model: str | None = None, input_tokens: int = 0, prices: dict | None = None) -> dict:
    """Modelin 1M belirteç fiyatı ({"input", "output"}): llm_models tablosu (uzun istem kademesiyle), yoksa
    llm_per_million_tokens yedeği. model verilmezse Hermes modeli."""
    from app.config import settings
    prices = prices or table()
    row = (prices.get("llm_models") or {}).get(model or settings.hermes_model)
    if not row:
        return prices["llm_per_million_tokens"]
    if row.get("long_threshold") and input_tokens > row["long_threshold"]:
        return {"input": row.get("long_input", row["input"]), "output": row.get("long_output", row["output"])}
    return {"input": row["input"], "output": row["output"]}


def estimate_llm(input_tokens: int, output_tokens: int, prices: dict | None = None, model: str | None = None) -> float:
    rate = llm_rate(model, input_tokens, prices)
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
    fal = data.get("fal_video") or {}
    if not isinstance(fal, dict):
        raise PricingError("fal_video: motor -> fiyat satırı sözlüğü olmalı")
    if "h3max" in fal:
        for res, rate in ((fal["h3max"] or {}).get("per_second") or {}).items():
            _positive(rate, f"fal_video.h3max.per_second.{res}")
        if not (fal["h3max"] or {}).get("per_second"):
            raise PricingError("fal_video.h3max.per_second eksik (çözünürlük: saniye ücreti)")
    if "seedance25" in fal:
        row = fal["seedance25"] or {}
        if not row.get("per_1k_tokens") or not row.get("size"):
            raise PricingError("fal_video.seedance25: per_1k_tokens ve size gerekli")
        for res, rate in row["per_1k_tokens"].items():
            _positive(rate, f"fal_video.seedance25.per_1k_tokens.{res}")
    for name, rate in (data.get("fal_image") or {}).items():
        _positive(rate, f"fal_image.{name}")
    _positive((data.get("image_per_megapixel") or {}).get("flux_lora"), "image_per_megapixel.flux_lora")
    _positive((data.get("image_per_image") or {}).get("flux_kontext"), "image_per_image.flux_kontext")
    _positive(data.get("lipsync_per_minute"), "lipsync_per_minute")
    _positive(data.get("tts_per_1k_chars"), "tts_per_1k_chars")
    llm = data.get("llm_per_million_tokens") or {}
    _positive(llm.get("input"), "llm_per_million_tokens.input")
    _positive(llm.get("output"), "llm_per_million_tokens.output")
    for name, row in (data.get("llm_models") or {}).items():
        for part in ("input", "output", *(("long_input", "long_output") if (row or {}).get("long_threshold") else ())):
            _positive((row or {}).get(part), f"llm_models.{name}.{part}")
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
