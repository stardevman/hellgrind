"""ComfyUI 0.39.0'ın resmî şablonlarından üretilen iş akışları (comfy-workflows/i2v, r2v, keyframe): üreteçle aynı,
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


@pytest.mark.parametrize("kind", ["t2v", "i2v", "r2v", "keyframe"])
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
    f = vast_h3.fill(flow("i2v"), values)
    node = f["105:104"]["inputs"]
    assert (node["width"], node["height"], f["105:15"]["inputs"]["noise_seed"], f["105:111"]["inputs"]["value"]) == (1280, 704, 7, 5)
    assert f["114"]["inputs"]["image"] == "hg_1.jpg" and "{{" not in json.dumps(f)
    assert f["105:6"]["inputs"]["unet_name"] == "minimax_h3_fl2va_pruned_fp8_scaled.safetensors"
