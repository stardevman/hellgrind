"""Ücretli kare çiziminin hedefi (ağ yok; ücretli modüller yalnızca fonksiyon içinde yüklenir).

KEYFRAME_PLATFORM=fal (varsayılan; RunPod uç noktası silindi): Qwen-Image-2512 ve Qwen-Image-Edit-2511 fal.ai'de
(app/live/fal_qwen.py). runpod: aynı modeller RunPod worker'ındaki ComfyUI iş akışlarıyla (app/live/runpod_h3.py;
yalnızca H3_PLATFORM=runpod iken). Çağıranlar (storyboard, referans görselleri) ve kalite kapıları değişmez.
"""
from app.budget import pricing
from app.config import settings
from app.engine import paid


def platform() -> str:
    return "runpod" if settings.keyframe_platform == "runpod" else "fal"


def key_name() -> str:
    return "runpod_api_key" if platform() == "runpod" else "fal_key"


def ready(kind: str = "keyframe") -> bool:
    """Bu tür kare (keyframe: metinden; edit_keyframe: referanstan) ücretli modda çizilebilir mi."""
    if platform() == "fal":
        return paid.available("fal_key")
    if settings.h3_platform != "runpod" or not paid.available("runpod_api_key"):
        return False
    from app.live import runpod_h3              # ücretli modül: yalnızca fonksiyon içinde
    return runpod_h3.keyframes_ready(kind)


def estimate(model: str = "qwen", edit_mode: str = "full", angles: bool = False, width: int | None = None,
             height: int | None = None) -> float:
    """Tek karenin rezervasyonu. fal: megapiksel başına (boy verilirse ona göre); RunPod: worker süresi."""
    if platform() == "fal":
        name = "qwen_image" if model == "qwen" else "qwen_edit_lora" if angles else "qwen_edit"
        return pricing.estimate_fal_image(name, width or 1280, height or 704)
    from app.live import runpod_h3              # ücretli modül: yalnızca fonksiyon içinde
    if model == "qwen":
        return runpod_h3.estimate_keyframe()
    return runpod_h3.estimate_keyframe("qwen_edit", edit_mode)


def keyframe_image(prompt: str, seed: int, project_id: str | None = None, width: int | None = None,
                   height: int | None = None):
    if platform() == "fal":
        from app.live import fal_qwen           # ücretli modül: yalnızca fonksiyon içinde
        return fal_qwen.keyframe_image(prompt, seed, project_id, width=width, height=height)
    from app.live import runpod_h3
    return runpod_h3.keyframe_image(prompt, seed, project_id, width=width, height=height)


def edit_keyframe_image(prompt: str, seed: int, references: list[dict], negative: str = "",
                        project_id: str | None = None, mode: str | None = None, angles: dict | None = None,
                        noted: dict | None = None):
    if platform() == "fal":
        from app.live import fal_qwen           # ücretli modül: yalnızca fonksiyon içinde
        return fal_qwen.edit_keyframe_image(prompt, seed, references, negative, project_id, mode, angles=angles,
                                            noted=noted)
    from app.live import runpod_h3
    return runpod_h3.edit_keyframe_image(prompt, seed, references, negative, project_id, mode, angles=angles,
                                         noted=noted)
