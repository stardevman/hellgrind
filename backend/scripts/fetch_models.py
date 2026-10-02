"""Gerçek kalite skorlayıcısının model dosyalarını indirir ve doğrular (imaj kurulurken bir kez çalışır).

Kullanım: python scripts/fetch_models.py /models          dosyaları indirir, SHA-256 özetlerini denetler
          python scripts/fetch_models.py /models --warm   modeli bir kez kurar: metin kodlayıcının yapılandırması ve
                                                          sözcük ayırıcısı HF önbelleğine (HF_HOME) iner; çalışma anında
                                                          ağ gerekmez (HF_HUB_OFFLINE=1)

Hepsi ücretsizdir ve ticari kullanıma açık lisanslıdır:
  OpenCLIP xlm-roberta-base-ViT-B-32 (LAION-5B)   MIT          çok dilli: Türkçe çekim tarifleri de ölçülür
  YuNet yüz bulucu                                MIT          OpenCV Zoo
  SFace yüz tanıma                                Apache 2.0   OpenCV Zoo
InsightFace'in hazır modelleri (buffalo_l) yalnızca ticari olmayan araştırma lisanslı olduğu için kullanılmaz.
Dosyalar belirli bir sürüme sabitlenmiştir; özet tutmazsa kurulum durur.
"""
import hashlib
import os
import sys
import urllib.request

HF = "https://huggingface.co/laion/CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k/resolve/506d40eb551f4801a1c27fc20a31c7b8f590deda"
ZOO = "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models"

CLIP_MODEL = "xlm-roberta-base-ViT-B-32"
CLIP_FILE = "clip/open_clip_pytorch_model.bin"
YUNET_FILE = "face/face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face/face_recognition_sface_2021dec.onnx"
MODELS = {   # yerel yol -> (adres, sha256)
    CLIP_FILE: (f"{HF}/open_clip_pytorch_model.bin",
                 "a5412f19cca70db8d434f68361f7254bd322a553455a63dbb709bd6a650cf378"),
    YUNET_FILE: (f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    SFACE_FILE: (f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                 "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
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


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/models"
    if "--warm" in sys.argv:
        warm(target)
    else:
        fetch(target)
