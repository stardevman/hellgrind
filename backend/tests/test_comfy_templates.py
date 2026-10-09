"""ComfyUI 0.39.0'ın resmî şablonlarından üretilen iş akışları (comfy-workflows/i2v, r2v, keyframe, edit_keyframe): üreteçle aynı,
volume'daki dosyalarla uyumlu, bağlantıları tam; referans yuvaları gönderilen görsel sayısına göre budanır. Ağ yok."""
import copy
import importlib.util
import json
import pathlib

import pytest

from app.engine.providers import GenRequest
from app.live import vast_h3

ROOT = pathlib.Path(__file__).resolve().parents[2]
FLOWS = ROOT / "comfy-workflows"
if not (FLOWS / "exported").exists():                        # konteynerde depo kökü /comfy-workflows'a bağlanır
    FLOWS = pathlib.Path("/comfy-workflows")
pytestmark = pytest.mark.skipif(not (FLOWS / "exported" / "make_templates.py").exists(), reason="iş akışları bağlı değil")


def flow(kind: str) -> dict:
    return json.loads((FLOWS / f"{kind}.json").read_text())


def generator():
    spec = importlib.util.spec_from_file_location("hg_make_templates", FLOWS / "exported" / "make_templates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_committed_workflows_are_exactly_what_the_generator_makes():
    gen = generator()
    for kind, made in gen.build().items():
        assert (FLOWS / f"{kind}.json").read_text() == gen.render(made), f"{kind}.json elle değiştirilmiş: üreteci çalıştırın"


@pytest.mark.parametrize("kind", ["t2v", "i2v", "r2v", "keyframe", "edit_keyframe"])
def test_workflows_validate_and_every_link_points_to_a_node(kind):
    f = vast_h3.validate_workflow(kind, (FLOWS / f"{kind}.json").read_text())
    for node_id, node in f.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in f, f"{kind}: {node_id}.{key} -> {value[0]}"
    classes = {n["class_type"] for n in f.values()}
    assert "ResolutionSelector" not in classes and {"filename_prefix", "seed", "width", "height"} <= vast_h3.placeholders(f)


def test_the_volume_files_and_turbo_are_used():
    t2v, i2v, r2v, kf = flow("t2v"), flow("i2v"), flow("r2v"), flow("keyframe")
    assert (i2v["105:11"]["inputs"]["vae_name"] == r2v["119"]["inputs"]["vae_name"] == t2v["140:119"]["inputs"]["vae_name"]
            == "minimax_h3_video_vae_fp16.safetensors")
    assert i2v["105:126"]["inputs"]["value"] is True and r2v["146"]["inputs"]["value"] is True        # turbo LoRA
    assert t2v["140:139"]["inputs"]["value"] is True and "first_frame" not in t2v["140:131"]["inputs"]
    assert t2v["140:133"]["inputs"]["value"] == "{{duration}}" and t2v["140:129"]["inputs"]["noise_seed"] == "{{seed}}"
    unets = [i2v["105:6"], t2v["140:127"], r2v["127"]]
    assert [u["inputs"]["unet_name"] for u in unets] == ["minimax_h3_fl2va_pruned_{{unet_variant}}.safetensors"] * 2 + [
        "minimax_h3_ref2va_pruned_{{unet_variant}}.safetensors"]
    assert "119" not in i2v and "120" not in i2v                                                     # bağlantısız düğümler
    assert "Lightning" not in json.dumps(kf) and kf["238:222"]["inputs"]["model"] == ["238:226", 0]
    assert (kf["238:230"]["inputs"]["steps"], kf["238:230"]["inputs"]["cfg"]) == (50, 4)
    assert kf["238:228"]["inputs"]["text"].startswith("低分辨率")                                       # Qwen negatifi korunur
    assert i2v["105:111"]["inputs"]["value"] == r2v["132"]["inputs"]["value"] == "{{duration}}"


@pytest.mark.parametrize("n,kept", [(1, ["137"]), (2, ["137", "139"]), (9, None)])
def test_unused_reference_slots_are_pruned(n, kept):
    f = vast_h3.prune_references(copy.deepcopy(flow("r2v")), n)
    refs = sorted(k for k in f["136"]["inputs"] if k.startswith("ref_images."))
    assert len(refs) == n and all(f["136"]["inputs"][k][0] in f for k in refs)
    if kept:
        assert [k for k, v in f.items() if v["class_type"] == "LoadImage"] == kept


def test_filled_values_land_as_numbers_and_the_first_frame_is_named_in_the_prompt():
    job = vast_h3.build_job(GenRequest(prompt="Kerem crosses the bridge", seed=7, resolution="720p", duration_s=5.0,
                                       image_url="data:image/jpeg;base64,AAAA"), server="runpod://ep")
    assert job.prompt.endswith(vast_h3.FIRST_FRAME_NOTE)
    values = {"prompt": job.prompt, "seed": job.seed, "width": job.width, "height": job.height, "duration": job.seconds,
              "first_frame": "hg_1.jpg", "filename_prefix": "hellgrind/i2v_x", "unet_variant": "fp8_scaled"}
    f = vast_h3.fill(vast_h3.prune_unfilled(flow("i2v"), values), values)     # son kare gönderilmedi: düğüm çıkar
    node = f["105:104"]["inputs"]
    assert (node["width"], node["height"], f["105:15"]["inputs"]["noise_seed"], f["105:111"]["inputs"]["value"]) == (1280, 704, 7, 5)
    assert f["114"]["inputs"]["image"] == "hg_1.jpg" and "{{" not in json.dumps(f)
    assert f["105:6"]["inputs"]["unet_name"] == "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"


# ---------- Qwen-Image-Edit-2511: karakter referansından ilk kare ----------

EDIT_ENCODERS = ("170:151", "170:149")


def test_the_edit_keyframe_draws_on_the_h3_canvas_not_on_image_1():
    """Must-fix 1: latent, 1. görselin VAEEncode'u değil, H3 tuvalinde boş latent (720p 1280x704)."""
    f = flow("edit_keyframe")
    assert "170:156" not in f                                           # 1. görselin latent'i çıkarıldı
    latent = f[f["170:169"]["inputs"]["latent_image"][0]]
    assert latent["class_type"] == "EmptySD3LatentImage" and latent["inputs"] == {
        "width": "{{width}}", "height": "{{height}}", "batch_size": 1}
    assert f["170:160"]["inputs"]["image"] == ["41", 0]                 # 1. görsel koşul için ölçeklenir (korunur)
    assert f["170:169"]["inputs"]["denoise"] == 1 and f["170:169"]["inputs"]["seed"] == "{{seed}}"


def test_every_reference_goes_to_both_text_encoders_in_the_same_order():
    """Must-fix 2: 41 = image 1, 83 = image 2, 171 = image 3; her görsel iki kodlayıcıya da aynı sırayla."""
    f = flow("edit_keyframe")
    images = {k: f[k]["inputs"]["image"] for k in ("41", "83", "171")}
    assert images == {"41": "{{reference_1}}", "83": "{{reference_2}}", "171": "{{reference_3}}"}
    for enc in EDIT_ENCODERS:
        inputs = f[enc]["inputs"]
        assert (inputs["image1"], inputs["image2"], inputs["image3"]) == (["170:160", 0], ["83", 0], ["171", 0])
    assert f["170:151"]["inputs"]["prompt"] == "{{prompt}}" and f["170:149"]["inputs"]["prompt"] == "{{negative}}"


def test_the_lightning_switch_and_the_volume_files():
    f = flow("edit_keyframe")
    assert f["170:168"]["inputs"]["value"] == "{{lightning}}"
    assert (f["170:166"]["inputs"]["value"], f["170:154"]["inputs"]["value"]) == ("{{edit_steps}}", 4)   # kapalı: 40/20 adım, cfg 4
    assert (f["170:165"]["inputs"]["value"], f["170:155"]["inputs"]["value"]) == (4, 1)           # açık: 4 adım, cfg 1
    assert f["170:161"]["inputs"]["unet_name"] == "qwen_image_edit_2511_fp8mixed.safetensors"
    assert f["170:153"]["inputs"]["lora_name"] == "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
    assert f["170:162"]["inputs"]["clip_name"] == "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    assert f["170:146"]["inputs"]["vae_name"] == "qwen_image_vae.safetensors"
    assert f["9"]["inputs"]["filename_prefix"] == "{{filename_prefix}}"


@pytest.mark.parametrize("n,kept", [(1, ["41"]), (2, ["41", "83"]), (3, ["41", "83", "171"])])
def test_unsent_edit_images_and_their_encoder_inputs_are_pruned(n, kept):
    f = vast_h3.prune_references(copy.deepcopy(flow("edit_keyframe")), n)
    assert {k for k, v in f.items() if v["class_type"] == "LoadImage"} == set(kept)
    for enc in EDIT_ENCODERS:
        assert [k for k in ("image1", "image2", "image3") if k in f[enc]["inputs"]] == ["image1", "image2", "image3"][:n]
    filled = vast_h3.fill(f, {"lightning": False, "width": 1280, "height": 704, "seed": 5})
    assert filled["170:168"]["inputs"]["value"] is False and filled["172"]["inputs"]["width"] == 1280
