"""ÜCRETLİ: kare çizimi fal.ai'de (KEYFRAME_PLATFORM=fal; RunPod uç noktası silindi). Bu modül testlerde yalnızca sahte
istemciyle çalışır.

RunPod worker'ındaki ComfyUI iş akışlarının fal karşılıkları; çağıranlar (engine/storyboard.py, engine/references.py)
ve bütün kapılar (kimlik, kopya, ölçek, renk, çerçeve, sıçrama) değişmez, yalnızca çizimin hedefi değişir:
  keyframe.json (Qwen-Image-2512)            -> fal-ai/qwen-image-2512           0,02 $/MP (yukarı yuvarlanır)
  edit_keyframe.json (Qwen-Image-Edit-2511)  -> fal-ai/qwen-image-edit-2511      0,03 $/MP
  + Multiple Angles LoRA (node 170:180)       -> fal-ai/qwen-image-edit-2511/lora 0,035 $/MP, loras[{path: URL, scale}]

Açı karesi için /lora seçildi: istem LoRA'nın kendi biçimiyle başlar ("<sks> front-left quarter view low-angle shot
close-up", core/chain.angle_tokens) ve image 1 taban kare, image 2-3 karakter referanslarıdır. Hazır
fal-ai/qwen-image-edit-2511-multiple-angles uç noktası "<sks>" metni almaz: açıyı sayıyla ister (horizontal_angle,
vertical_angle, zoom) ve istemi kendisi kurar (additional_prompt yalnızca sona eklenir); karakter referanslarını da
taşımaz (tek görüntünün kamerasını döndürür).

Adım ayarlarının karşılığı (fal'de Lightning LoRA yok; fiyat adımdan bağımsız, megapiksel başına):
  full      40 adım, guidance 4, acceleration none
  medium    20 adım, guidance 4, acceleration regular
  lightning 10 adım, guidance 4, acceleration high (hız; RunPod'daki 4 adım cfg 1 Lightning LoRA'sının yerine)
"""
import logging
import math

from app.budget import pricing
from app.config import settings
from app.engine.providers import GenResult
from app.live import PaidCallBlocked, guard, no_image_generation
from app.live import fal_queue

log = logging.getLogger("hellgrind.live.fal_qwen")

SIZE = (1280, 704)                   # H3 tuvali: RunPod yolundaki kareyle aynı boy (kapılar ve kırp + iyileştir bunu bekler)
TIMEOUT_S = 300
QWEN_IMAGE = {"num_inference_steps": 50, "guidance_scale": 4.0, "acceleration": "none"}   # keyframe.json: 50 adım
EDIT = {"full": {"num_inference_steps": 40, "guidance_scale": 4.0, "acceleration": "none"},
        "medium": {"num_inference_steps": 20, "guidance_scale": 4.0, "acceleration": "regular"},
        "lightning": {"num_inference_steps": 10, "guidance_scale": 4.0, "acceleration": "high"}}
MAX_IMAGES = 3


def qwen_image_body(prompt: str, seed: int, width: int | None = None, height: int | None = None,
                    negative: str = "") -> tuple[str, dict]:
    w, h = (width or SIZE[0]), (height or SIZE[1])
    args = {"prompt": prompt, "image_size": {"width": int(w), "height": int(h)}, "seed": int(seed), "num_images": 1,
            "output_format": "png", "enable_safety_checker": True, **QWEN_IMAGE}
    if negative:
        args["negative_prompt"] = negative
    return settings.fal_qwen_image_endpoint, args


def edit_body(prompt: str, seed: int, image_urls: list[str], negative: str = "", mode: str | None = None,
              angles: dict | None = None) -> tuple[str, dict]:
    """Qwen-Image-Edit-2511: image_urls sırası istemdeki "image 1, image 2, image 3" ile aynıdır. angles: {"lora"?,
    "strength"}: Multiple Angles LoRA (/lora uç noktası, loras[].path Hugging Face adresi)."""
    mode = mode if mode in EDIT else (settings.qwen_edit_mode if settings.qwen_edit_mode in EDIT else "full")
    args = {"prompt": prompt, "image_urls": list(image_urls)[:MAX_IMAGES], "seed": int(seed),
            "image_size": {"width": SIZE[0], "height": SIZE[1]}, "num_images": 1, "output_format": "png",
            "enable_safety_checker": True, **EDIT[mode]}
    if negative:
        args["negative_prompt"] = negative
    if angles:
        path = angles.get("url") or settings.qwen_edit_angles_lora_url
        args["loras"] = [{"path": path, "scale": float(angles.get("strength") or settings.qwen_edit_angles_strength)}]
        return settings.fal_qwen_edit_lora_endpoint, args
    return settings.fal_qwen_edit_endpoint, args


def cost(model: str, width: int | None = None, height: int | None = None) -> float:
    return pricing.estimate_fal_image(model, width or SIZE[0], height or SIZE[1])


def _image(endpoint: str, args: dict, project_id: str | None, model: str, noted: dict | None = None) -> GenResult:
    client = fal_queue.client()
    out = fal_queue.run(client, endpoint, args, timeout_s=TIMEOUT_S, project_id=project_id, info=noted)
    images = out.result.get("images") or []
    if not images or not images[0].get("url"):
        raise fal_queue.FalError(f"fal.ai yanıtında görsel yok ({out.request_id})")
    if any(out.result.get("has_nsfw_concepts") or []):
        raise fal_queue.FalError(f"fal.ai güvenlik denetimi kareyi kararttı ({out.request_id}); istemi gözden geçirin.")
    path = fal_queue.download(images[0]["url"], ".png" if args.get("output_format") == "png" else ".jpg",
                              images[0].get("file_size"))
    size = args.get("image_size") or {}
    return GenResult(local_path=path, request_id=out.request_id,
                     cost_usd=cost(model, size.get("width"), size.get("height")), engine="fal_qwen",
                     meta={"endpoint": endpoint, "seed": out.result.get("seed"), "model": model, **(noted or {})})


def keyframe_image(prompt: str, seed: int, project_id: str | None = None, width: int | None = None,
                   height: int | None = None, negative: str = "") -> GenResult:
    """Metinden kare (storyboard ilk karesi ya da varlık referansı). Çağıran `cost("qwen_image", w, h)` kadar
    rezervasyon almış olmalı."""
    no_image_generation("kare")
    guard("fal_key")
    endpoint, args = qwen_image_body(prompt, seed, width, height, negative)
    return _image(endpoint, args, project_id, "qwen_image")


def edit_keyframe_image(prompt: str, seed: int, references: list[dict], negative: str = "",
                        project_id: str | None = None, mode: str | None = None, angles: dict | None = None,
                        noted: dict | None = None) -> GenResult:
    """runpod_h3.edit_keyframe_image ile aynı arayüz: references [{"name", "key"}] (image 1..3), mode full | medium |
    lightning, angles {"lora", "strength"}. Görseller fal deposuna SHA önbellekli yüklenir."""
    no_image_generation("kare")
    guard("fal_key")
    if not references:
        raise PaidCallBlocked("Qwen-Image-Edit karesi için karakterin onaylı referans görseli yok.")
    client = fal_queue.client()
    urls = [fal_queue.upload_key(client, r["key"]) for r in references[:MAX_IMAGES]]
    endpoint, args = edit_body(prompt, seed, urls, negative, mode, angles)
    slots = [{"image": i, "name": r["name"], "key": r["key"]} for i, r in enumerate(references[:MAX_IMAGES], start=1)]
    return _image(endpoint, args, project_id, "qwen_edit_lora" if angles else "qwen_edit",
                  {"reference_keys": slots, **(noted or {})})


def megapixels(width: int, height: int) -> int:
    return max(1, math.ceil(width * height / 1_000_000 - 1e-9))
