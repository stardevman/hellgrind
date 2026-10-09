"""Gerçek kalite skorlayıcısının model dosyalarını indirir ve doğrular (imaj kurulurken bir kez çalışır).

Kullanım: python scripts/fetch_models.py /models          dosyaları indirir, SHA-256 özetlerini denetler
          python scripts/fetch_models.py /models --warm   modeli bir kez kurar: metin kodlayıcının yapılandırması ve
                                                          sözcük ayırıcısı HF önbelleğine (HF_HOME) iner; çalışma anında
                                                          ağ gerekmez (HF_HUB_OFFLINE=1)

Hepsi ücretsizdir ve ticari kullanıma açık lisanslıdır:
  OpenCLIP xlm-roberta-base-ViT-B-32 (LAION-5B)   MIT          çok dilli: Türkçe çekim tarifleri de ölçülür
  YuNet yüz bulucu                                MIT          OpenCV Zoo
  SFace yüz tanıma                                Apache 2.0   OpenCV Zoo
  Helsinki-NLP opus-mt-tr-en (MarianMT)           Apache 2.0   prompt çevirisi: görüntü modelleri İngilizce istemi izler
  facebook/dinov2-small (DINOv2)                  Apache 2.0   robot/hayvan karakter benzerliği, kareler arası süreklilik
InsightFace'in hazır modelleri (buffalo_l) yalnızca ticari olmayan araştırma lisanslı olduğu için kullanılmaz.
Dosyalar belirli bir sürüme sabitlenmiştir; özet tutmazsa kurulum durur.
"""
import hashlib
import os
import sys
import urllib.request

HF = "https://huggingface.co/laion/CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k/resolve/506d40eb551f4801a1c27fc20a31c7b8f590deda"
MT = "https://huggingface.co/Helsinki-NLP/opus-mt-tr-en/resolve/19c65427cc2af5f191337d4899e0348c4af25902"
ZOO = "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models"
DINO = "https://huggingface.co/facebook/dinov2-small/resolve/ed25f3a31f01632728cabb09d1542f84ab7b0056"

CLIP_MODEL = "xlm-roberta-base-ViT-B-32"
CLIP_FILE = "clip/open_clip_pytorch_model.bin"
YUNET_FILE = "face/face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face/face_recognition_sface_2021dec.onnx"
MT_DIR = "mt/opus-mt-tr-en"
MT_FILES = {   # dosya -> sha256
    "config.json": "58c3d18b384cebc9383f6d85a72dae0e02cc47695097b69ff130f8be4c0e07e3",
    "generation_config.json": "a633dc790450cf8c19347853cb323f9ea82f91e5f59e5b4be431a2248adfef11",
    "pytorch_model.bin": "e2b820f31c9348b0ae33d77ac23bd1686fa60a8afa0b9511bc7d9a35c125b534",
    "source.spm": "4a6356e3ee892f2896dbf0b5cabeb51fdf16e03c70b0702cefc8b847e504241c",
    "target.spm": "4cc15427e50b80e53b7dcf5f4d8a52edfe219b8d8e290c031f5578f5760b335f",
    "tokenizer_config.json": "3483b08070a3abacc4422dc1672ba9a175a9ce7503a17d1ca999fb34664dfea7",
    "vocab.json": "f6518b18a71fea3de99b5883cb80bcd188b74c33e7c6a5a597b7c64119f1b1fe",
}
DINO_DIR = "dinov2"
DINO_FILES = {   # dosya -> sha256
    "config.json": "1809f83e3bdb1609a501a610ad4a742f4fd8ae44d72ca4aa0df52d1f2ac8628d",
    "preprocessor_config.json": "14e780d86fa1861f8751f868d7f45425b5feb55c38ca26f152ca5097ab30f828",
    "model.safetensors": "ae1e99fcefd534ed978cdeb8326f08030c96e28b7a81ffcbc98a857c84d14be1",
}
MODELS = {   # yerel yol -> (adres, sha256)
    CLIP_FILE: (f"{HF}/open_clip_pytorch_model.bin",
                 "a5412f19cca70db8d434f68361f7254bd322a553455a63dbb709bd6a650cf378"),
    YUNET_FILE: (f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    SFACE_FILE: (f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                 "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    **{f"{MT_DIR}/{name}": (f"{MT}/{name}", digest) for name, digest in MT_FILES.items()},
    **{f"{DINO_DIR}/{name}": (f"{DINO}/{name}", digest) for name, digest in DINO_FILES.items()},
}


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(root: str) -> None:
    for rel, (url, digest) in MODELS.items():
        path = os.path.join(root, rel)
        if os.path.exists(path) and sha256(path) == digest:
            print(f"var: {rel}")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(f"indiriliyor: {rel}", flush=True)
        urllib.request.urlretrieve(url, path + ".part")
        got = sha256(path + ".part")
        if got != digest:
            os.remove(path + ".part")
            sys.exit(f"{rel}: SHA-256 tutmadı ({got} != {digest})")
        os.replace(path + ".part", path)


def warm(root: str) -> None:
    """Modeli bir kez kurar (önbelleği doldurur) ve küçük bir sağlama yapar."""
    import open_clip
    import torch
    model, _, _ = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=os.path.join(root, CLIP_FILE))
    tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
    with torch.no_grad():
        text = model.encode_text(tokenizer(["altın saat ışığı", "golden hour light"]))
    assert text.shape[0] == 2
    print("model hazır:", CLIP_MODEL)
    from transformers import MarianMTModel, MarianTokenizer
    path = os.path.join(root, MT_DIR)
    tok, mt = MarianTokenizer.from_pretrained(path), MarianMTModel.from_pretrained(path).eval()
    with torch.no_grad():
        out = tok.batch_decode(mt.generate(**tok(["Köprü sarsılır."], return_tensors="pt"), max_new_tokens=20),
                               skip_special_tokens=True)
    assert "bridge" in out[0].lower(), out
    print("çeviri modeli hazır:", MT_DIR, "->", out[0])
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModel
    path = os.path.join(root, DINO_DIR)
    processor, dino = AutoImageProcessor.from_pretrained(path), AutoModel.from_pretrained(path).eval()
    with torch.no_grad():
        vec = dino(**processor(images=Image.new("RGB", (64, 64), (120, 90, 60)), return_tensors="pt")).pooler_output
    assert vec.shape[-1] == 384, vec.shape
    print("DINOv2 hazır:", DINO_DIR)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/models"
    if "--warm" in sys.argv:
        warm(target)
    else:
        fetch(target)
