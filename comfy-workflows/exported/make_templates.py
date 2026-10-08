"""ComfyUI'nin resmî şablonlarından (Export (API), ComfyUI 0.39.0; bu klasördeki *_api.json) Hell Grind iş akışlarını üretir:
../i2v.json (FL2VA), ../r2v.json (Ref2VA), ../keyframe.json (Qwen-Image-2512).

Şablonlara uygulananlar (NOTES: "Must-fix before use"):
1. Video VAE: volume'daki minimax_h3_video_vae_fp16 (int8_convrot dosyası volume'da yok; cu130 da gerektirmez).
2. Turbo LoRA açık: FL2VA fl2v_turbo_8step + 8 adım, Ref2VA ref2v_turbo_4step + 4 adım.
3. Qwen anahtar karesi: volume'da olmayan Lightning LoRA ve anahtar düğümleri çıkarılır; 50 adım, cfg 4 doğrudan.
4. Tuval: ResolutionSelector (115) yerine {{width}} / {{height}} (480p 864x480, 720p 1280x704; 32'nin katları).
5. FL2VA'daki bağlantısız 119 / 120 düğümleri silinir.
Değişen değerler Hell Grind yer tutucularıyla doldurulur: {{prompt}}, {{seed}}, {{duration}} (kare sayısını iş akışı
17k+5 olarak hesaplar), {{first_frame}}, {{reference_1}}..{{reference_9}}, {{width}}, {{height}}, {{filename_prefix}}.
Ref2VA'ya 9 referans yuvası eklenir; Hell Grind gönderilen görselden fazla yuvayı istekten önce çıkarır.

    python comfy-workflows/exported/make_templates.py      # yeniden dışa aktarınca tekrar çalıştırın
"""
import copy
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
MAX_REFERENCES = 9


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def drop(flow: dict, *ids: str) -> None:
    for node_id in ids:
        flow.pop(node_id)


def check(flow: dict, name: str) -> dict:
    """Her bağlantı var olan bir düğüme gitmeli; LoRA/VAE adları volume'dakilerle uyumlu."""
    for node_id, node in flow.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in flow, f"{name}: {node_id}.{key} -> {value[0]} yok"
    vaes = [n["inputs"]["vae_name"] for n in flow.values() if n["class_type"] == "VAELoader"]
    assert not any("int8" in v for v in vaes), f"{name}: volume'da olmayan int8 video VAE kaldı"
    assert "Lightning-4steps" not in json.dumps(flow), f"{name}: volume'da olmayan LoRA kaldı"
    return flow


def i2v() -> dict:
    f = load("fl2va_api.json")
    drop(f, "115", "119", "120")
    f["105:11"]["inputs"]["vae_name"] = VIDEO_VAE
    f["105:126"]["inputs"]["value"] = True
    node = f["105:104"]["inputs"]
    node.update(prompt="{{prompt}}", width="{{width}}", height="{{height}}")
    f["114"]["inputs"]["image"] = "{{first_frame}}"
    f["105:15"]["inputs"]["noise_seed"] = "{{seed}}"
    f["105:111"]["inputs"]["value"] = "{{duration}}"
    f["92"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "i2v")


def r2v() -> dict:
    f = load("ref2va_api.json")
    drop(f, "115")
    f["119"]["inputs"]["vae_name"] = VIDEO_VAE
    f["146"]["inputs"]["value"] = True
    node = f["136"]["inputs"]
    node.update(width="{{width}}", height="{{height}}")
    f["138"]["inputs"]["value"] = "{{prompt}}"
    f["137"]["inputs"]["image"] = "{{reference_1}}"
    f["139"]["inputs"]["image"] = "{{reference_2}}"
    template = f["137"]
    for i in range(3, MAX_REFERENCES + 1):                     # ek referans yuvaları: ref_image_2 .. ref_image_8
        node_id = str(147 + i)
        f[node_id] = copy.deepcopy(template)
        f[node_id]["inputs"]["image"] = f"{{{{reference_{i}}}}}"
        node[f"ref_images.ref_image_{i - 1}"] = [node_id, 0]
    f["129"]["inputs"]["noise_seed"] = "{{seed}}"
    f["132"]["inputs"]["value"] = "{{duration}}"
    f["92"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "r2v")


def keyframe() -> dict:
    f = load("keyframe_api.json")
    drop(f, "238:221", "238:233", "238:240", "238:243", "238:229", "238:225", "238:218", "238:224", "238:223")
    f["238:222"]["inputs"]["model"] = ["238:226", 0]
    sampler = f["238:230"]["inputs"]
    sampler.update(seed="{{seed}}", steps=50, cfg=4)
    f["238:227"]["inputs"]["text"] = "{{prompt}}"
    f["238:232"]["inputs"].update(width="{{width}}", height="{{height}}")
    f["60"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "keyframe")


def build() -> dict[str, dict]:
    return {"i2v": i2v(), "r2v": r2v(), "keyframe": keyframe()}


def render(flow: dict) -> str:
    return json.dumps(flow, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    for kind, flow in build().items():
        (OUT / f"{kind}.json").write_text(render(flow), encoding="utf-8")
        print(f"yazıldı: {kind}.json ({len(flow)} düğüm)")
