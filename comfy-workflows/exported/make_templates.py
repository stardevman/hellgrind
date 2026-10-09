"""ComfyUI'nin resmî şablonlarından (Export (API), ComfyUI 0.39.0; bu klasördeki *_api.json) Hell Grind iş akışlarını üretir:
../t2v.json (yalnızca metin), ../i2v.json (FL2VA), ../r2v.json (Ref2VA), ../keyframe.json (Qwen-Image-2512),
../edit_keyframe.json (Qwen-Image-Edit-2511: karakter referansından ilk kare).

Şablonlara uygulananlar (NOTES: "Must-fix before use"):
1. Video VAE: volume'daki minimax_h3_video_vae_fp16 (int8_convrot dosyası volume'da yok; cu130 da gerektirmez).
2. Turbo LoRA açık: FL2VA ve T2V fl2v_turbo_8step + 8 adım, Ref2VA ref2v_turbo_4step + 4 adım.
3. Qwen anahtar karesi: volume'da olmayan Lightning LoRA ve anahtar düğümleri çıkarılır; 50 adım, cfg 4 doğrudan.
4. Tuval: ResolutionSelector (115) yerine {{width}} / {{height}} (480p 864x480, 720p 1280x704; 32'nin katları).
5. FL2VA'daki bağlantısız 119 / 120 düğümleri silinir. FL2VA'ya isteğe bağlı son kare eklenir: 116 = {{last_frame}} ->
   MiniMaxH3ImageToVideo.last_frame (anahtar kare zinciri: çekimin son karesi sonraki çekimin ilk karesi); gönderilmezse
   Hell Grind düğümü ve girdiyi istekten çıkarır.
6. H3 DiT dosyası türe bağlıdır: minimax_h3_{fl2va,ref2va}_pruned_{{unet_variant}}.safetensors. Hell Grind
   {{unet_variant}}'ı Ayarlar'daki H3_UNET_VARIANT'tan doldurur: fp8_scaled (varsayılan; cu128 torch'lu worker) ya da
   int8_convrot (Comfy-Org'un önerisi; cu130 torch ister). Metin kodlayıcı nvfp4_awq, video VAE fp16 kalır.
7. Qwen-Image-Edit-2511 (image_qwen_image_edit_2511, workflow-templates v0.11.78): gizli latent EmptySD3LatentImage
   (H3 tuvali {{width}} x {{height}}; şablonda 1. görselin VAEEncode'u, çıktı onun boyutunu alırdı), 41 = image 1 (ana
   karakter), 83 = image 2, yeni 171 = image 3 (ikinci karakter ya da mekân); her görsel iki metin kodlayıcısına
   (170:151 olumlu, 170:149 olumsuz) aynı sırayla gider. Lightning LoRA anahtarı {{lightning}} (açık: 4 adım, cfg 1;
   kapalı: {{edit_steps}} adım — tam 40, orta 20 —, cfg 4); LoRA dosyası volume'dadır.
Değişen değerler Hell Grind yer tutucularıyla doldurulur: {{prompt}}, {{seed}}, {{duration}} (kare sayısını iş akışı
17k+5 olarak hesaplar), {{first_frame}}, {{reference_1}}..{{reference_9}}, {{width}}, {{height}}, {{filename_prefix}}; Qwen düzenleme karesinde
ayrıca {{negative}} ve {{lightning}}.
Ref2VA'ya 9 referans yuvası eklenir (ComfyUI 0.39.0'da ref_images autogrow, en çok 9); Hell Grind gönderilen görselden
fazla yuvayı istekten önce çıkarır. Aynı düğüm ileride ses referansları için ref_videos (en çok 3; 24 fps, 2-15 sn),
ref_video_audios ve ref_audios (en çok 3) da alır; şimdilik kullanılmıyor.

    python comfy-workflows/exported/make_templates.py      # yeniden dışa aktarınca tekrar çalıştırın
"""
import copy
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
UNET = {"fl2va": "minimax_h3_fl2va_pruned_{{unet_variant}}.safetensors",
        "ref2va": "minimax_h3_ref2va_pruned_{{unet_variant}}.safetensors"}
MAX_REFERENCES = 9


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def drop(flow: dict, *ids: str) -> None:
    for node_id in ids:
        flow.pop(node_id)


def check(flow: dict, name: str, lightning: bool = False) -> dict:
    """Her bağlantı var olan bir düğüme gitmeli; LoRA/VAE adları volume'dakilerle uyumlu. lightning: Qwen-Image-Edit'in
    Lightning LoRA'sı volume'dadır (Qwen-Image-2512'ninki değil)."""
    for node_id, node in flow.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in flow, f"{name}: {node_id}.{key} -> {value[0]} yok"
    unets = [n["inputs"]["unet_name"] for n in flow.values() if n["class_type"] == "UNETLoader"]
    assert all("{{unet_variant}}" in u for u in unets if "minimax_h3" in u), f"{name}: H3 DiT türü sabit kaldı"
    vaes = [n["inputs"]["vae_name"] for n in flow.values() if n["class_type"] == "VAELoader"]
    assert not any("int8" in v for v in vaes), f"{name}: volume'da olmayan int8 video VAE kaldı"
    assert lightning or "Lightning-4steps" not in json.dumps(flow), f"{name}: volume'da olmayan LoRA kaldı"
    return flow


def t2v() -> dict:
    """video_minimax_h3_t2v: aynı MiniMaxH3ImageToVideo düğümü, ilk karesiz (FL2VA DiT + fl2v turbo LoRA)."""
    f = load("t2v_api.json")
    drop(f, "115")
    f["140:119"]["inputs"]["vae_name"] = VIDEO_VAE
    f["140:127"]["inputs"]["unet_name"] = UNET["fl2va"]
    f["140:139"]["inputs"]["value"] = True
    f["140:131"]["inputs"].update(prompt="{{prompt}}", width="{{width}}", height="{{height}}")
    f["140:129"]["inputs"]["noise_seed"] = "{{seed}}"
    f["140:133"]["inputs"]["value"] = "{{duration}}"
    f["92"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "t2v")


def i2v() -> dict:
    f = load("fl2va_api.json")
    drop(f, "115", "119", "120")
    f["105:11"]["inputs"]["vae_name"] = VIDEO_VAE
    f["105:6"]["inputs"]["unet_name"] = UNET["fl2va"]
    f["105:126"]["inputs"]["value"] = True
    node = f["105:104"]["inputs"]
    node.update(prompt="{{prompt}}", width="{{width}}", height="{{height}}")
    f["114"]["inputs"]["image"] = "{{first_frame}}"
    f["116"] = copy.deepcopy(f["114"])                           # isteğe bağlı son kare (sonraki çekimin ilk karesi)
    f["116"]["inputs"]["image"] = "{{last_frame}}"
    node["last_frame"] = ["116", 0]
    f["105:15"]["inputs"]["noise_seed"] = "{{seed}}"
    f["105:111"]["inputs"]["value"] = "{{duration}}"
    f["92"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "i2v")


def r2v() -> dict:
    f = load("ref2va_api.json")
    drop(f, "115")
    f["119"]["inputs"]["vae_name"] = VIDEO_VAE
    f["127"]["inputs"]["unet_name"] = UNET["ref2va"]
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


EDIT_LIGHTNING = "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
EDIT_ENCODERS = ("170:151", "170:149")          # olumlu, olumsuz: görseller ikisine de aynı sırayla gider


def edit_keyframe() -> dict:
    """Qwen-Image-Edit-2511: karakter referansından (image 1) H3 tuvalinde ilk kare. Görseller: 41 = image 1, 83 = image 2,
    171 = image 3; Hell Grind gönderilmeyen görselin düğümünü ve image2/image3 girdilerini istekten çıkarır."""
    f = load("qwen_edit_2511_api.json")
    assert f["170:153"]["inputs"]["lora_name"] == EDIT_LIGHTNING
    drop(f, "170:156")                                         # 1. görselin latent'i: çıktı onun boyutunu alırdı
    f["172"] = {"class_type": "EmptySD3LatentImage", "_meta": {"title": "H3 tuvali"},
                "inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": 1}}
    sampler = f["170:169"]["inputs"]
    sampler.update(seed="{{seed}}", latent_image=["172", 0])
    f["41"]["inputs"]["image"] = "{{reference_1}}"
    f["83"]["inputs"]["image"] = "{{reference_2}}"
    f["171"] = copy.deepcopy(f["83"])
    f["171"]["inputs"]["image"] = "{{reference_3}}"
    for enc in EDIT_ENCODERS:
        assert f[enc]["inputs"]["image1"] == ["170:160", 0] and f[enc]["inputs"]["image2"] == ["83", 0]
        f[enc]["inputs"]["image3"] = ["171", 0]
    f["170:151"]["inputs"]["prompt"] = "{{prompt}}"            # şablonun malzeme değiştirme örneği her zaman ezilir
    f["170:149"]["inputs"]["prompt"] = "{{negative}}"
    f["170:168"]["inputs"]["value"] = "{{lightning}}"
    f["170:166"]["inputs"]["value"] = "{{edit_steps}}"          # Lightning kapalıyken adım: tam 40, orta 20
    f["9"]["inputs"]["filename_prefix"] = "{{filename_prefix}}"
    return check(f, "edit_keyframe", lightning=True)


def build() -> dict[str, dict]:
    return {"t2v": t2v(), "i2v": i2v(), "r2v": r2v(), "keyframe": keyframe(), "edit_keyframe": edit_keyframe()}


def render(flow: dict) -> str:
    return json.dumps(flow, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    for kind, flow in build().items():
        (OUT / f"{kind}.json").write_text(render(flow), encoding="utf-8")
        print(f"yazıldı: {kind}.json ({len(flow)} düğüm)")
