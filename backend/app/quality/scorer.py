"""Gerçek kalite skoru: OpenCLIP ile stil ve istem uyumu, YuNet + SFace ile yüz kimliği; birleşik skor.

Ücretsiz ve yereldir: modeller imajın içindedir (/models, scripts/fetch_models.py); ağ ya da ücretli servis kullanılmaz.
Ağır paketler (torch, open_clip, cv2) yalnızca ilk skorlamada yüklenir ve süreç boyunca bellekte kalır.

Ölçüm (videoda eşit aralıkla SAMPLES kare, tek karede o kare):
  clip   Stil ve istem uyumu. Her kare, olumlu metinlerle (stil kılavuzunun ışık ve hareket dili; çekim tarifi) ve
         rakip metinlerle karşılaştırılır: kusur tarifleri (bulanık, bozuk, siyah, yazılı, tek renk, gürültü…) ve genel
         altyazılar ("an image", "a photo"…). Olumlu metnin rakiplerine karşı softmax olasılığı alınır (CLIP-IQA'daki zıt
         istem yöntemi), kareler ve olumlu metinler üzerinden ortalanır. Genel altyazılar, temiz ama tarifle ilgisiz bir
         görüntünün yüksek almasını önler: tarif, görüntüyü genel bir altyazıdan daha iyi açıklamalıdır. Model çok
         dillidir (XLM-RoBERTa): Hermes'in Türkçe çekim tarifleri de ölçülür. 0-1.
  face   Kimlik. Çekimdeki karakterlerin onaylı referans görsellerindeki yüzler ile karelerde bulunan yüzler arasındaki
         SFace kosinüs benzerliği; karakter başına en iyi eşleşme, OpenCV'nin aynı kişi eşiği (0,363) çevresinde lojistik
         eğriyle 0-1'e çevrilir. Karelerde yüz yoksa (geniş plan, arkası dönük) ya da karakterin referansında yüz yoksa
         yüz bileşeni yoktur ve skoru düşürmez.
  composite = clip; yüz bileşeni varsa (1 - w) * clip + w * face, w = settings.score_face_weight.
Eşikler proje ayarlarındaki clip_threshold ve excellent_threshold'dur. Kalibrasyon gerçek üretimle henüz sınanmadı: ilk
ücretli partide eşikler, olumsuz metinler ve ağırlık gözden geçirilmeli; kürasyon kayıtları (Faz 17) bunun verisidir.
"""
import importlib.util
import logging
import math
import os
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from app.config import settings

log = logging.getLogger("hellgrind.scorer")

CLIP_MODEL = "xlm-roberta-base-ViT-B-32"                  # scripts/fetch_models.py ile aynı
CLIP_FILE = "clip/open_clip_pytorch_model.bin"
YUNET_FILE = "face/face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face/face_recognition_sface_2021dec.onnx"
SCORER_ID = "openclip:xlm-roberta-base-ViT-B-32/laion5b_s13b_b90k+yunet-2023mar+sface-2021dec"
PACKAGES = ("torch", "open_clip", "cv2")

SAMPLES = 4                    # videodan örneklenen kare
FACE_SAME = 0.363              # OpenCV FaceRecognizerSF: aynı kişi için önerilen kosinüs eşiği
FACE_SLOPE = 0.05              # lojistik eğrinin genişliği: eşikte 0,5; +0,15'te ~0,95
DETECT_SCORE = 0.8             # YuNet güven eşiği
MIN_FACE_PX = 24               # bundan küçük yüzler kimlik için güvenilmez
DETECT_MAX_SIDE = 1024         # YuNet, kareyi dolduran büyük yüzleri (ör. 1500 px'lik portre) bulamıyor: önce küçültülür
TEXT_CACHE, REF_CACHE = 512, 256
GENERIC = "a detailed, well composed frame from an animated film"
ANCHORS = ("an image", "a picture", "a photo")   # Türkçe genel altyazı her şeye fazla benzer: ölçümü bozar
DEFECTS = (
    "a blurry, out of focus image",
    "a distorted image with deformed bodies and broken anatomy",
    "a glitched render full of artifacts",
    "a black, empty frame",
    "a single flat color",
    "random noise, static",
    "an image covered with text, captions and watermarks",
)
RIVALS = ANCHORS + DEFECTS                        # olumlu metnin karşısındaki metinler


@dataclass(frozen=True)
class Target:
    """Neye göre ölçülecek: stil metni, çekim tarifi ve karakterlerin referans görselleri ((ad, (anahtar, ...)), ...)."""
    style: str = ""
    content: str = ""
    faces: tuple = ()
    face_expected: bool = True        # yüz görünmeli mi (kimlik kapısı; core/h3mode.face_expected)


def target_for(scene, chars, style) -> Target:
    """Çekimin ölçütü: kılavuzun ışık ve hareket dili, çekim tarifi ve referans görseli olan karakterler."""
    from app.core.h3mode import face_expected
    return Target(style=" ".join(x for x in ((style.lighting_prefix or "") if style else "",
                                             (style.motion_style or "") if style else "") if x),
                  content=(scene.prompt_spec or {}).get("prompt", ""),
                  faces=tuple((ch.name, tuple(ch.reference_images or ())) for ch in chars if ch.reference_images),
                  face_expected=face_expected(scene, chars))


@dataclass
class Result:
    clip: float
    face: float | None
    composite: float
    detail: dict = field(default_factory=dict)


# ---------- hazır mı ----------

def model_paths(root: str | None = None) -> dict:
    root = root or settings.quality_model_dir
    return {name: os.path.join(root, rel) for name, rel in (("clip", CLIP_FILE), ("yunet", YUNET_FILE), ("sface", SFACE_FILE))}


def unavailable() -> str | None:
    """Kullanılamıyorsa nedeni (Türkçe), hazırsa None. Modelleri yüklemez."""
    for package in PACKAGES:
        if importlib.util.find_spec(package) is None:
            return f"{package} paketi kurulu değil (requirements-quality.txt)"
    for path in model_paths().values():
        if not os.path.exists(path):
            return f"model dosyası yok: {path} (scripts/fetch_models.py)"
    return None


def ensure_ready() -> None:
    """Para harcanmadan önce: paketler, model dosyaları ve modelin kendisi yüklenebiliyor mu."""
    reason = unavailable()
    if reason:
        raise NotImplementedError(f"Gerçek kalite skoru kullanılamıyor: {reason}. Ücretli modda üretim başlatılmaz.")
    models()


# ---------- modeller ----------

_models = None
_models_lock = threading.Lock()


def models() -> "Models":
    global _models
    with _models_lock:
        if _models is None:
            _models = Models(model_paths())
    return _models


class Models:
    """Süreç başına bir kez yüklenir. Çıkarım bir kilitle sıraya girer (üretim dalgası iş parçacıklıdır)."""

    def __init__(self, paths: dict):
        import cv2
        import open_clip
        import torch
        self.torch = torch
        self.clip, _, self.preprocess = open_clip.create_model_and_transforms(CLIP_MODEL, pretrained=paths["clip"])
        self.clip.eval()
        self.tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
        self.scale = float(self.clip.logit_scale.detach().exp())
        self.detector = cv2.FaceDetectorYN.create(paths["yunet"], "", (320, 320), DETECT_SCORE)
        self.recognizer = cv2.FaceRecognizerSF.create(paths["sface"], "")
        self.lock = threading.Lock()
        self.texts: OrderedDict[str, np.ndarray] = OrderedDict()
        self.refs: OrderedDict[str, np.ndarray | None] = OrderedDict()
        log.info("kalite skorlayıcısı yüklendi: %s", SCORER_ID)

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        batch = self.torch.stack([self.preprocess(im.convert("RGB")) for im in images])
        with self.lock, self.torch.no_grad():
            e = self.clip.encode_image(batch).float()
        return (e / e.norm(dim=-1, keepdim=True)).numpy()

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        with self.lock:
            missing = [t for t in dict.fromkeys(texts) if t not in self.texts]
            if missing:
                with self.torch.no_grad():
                    e = self.clip.encode_text(self.tokenizer(missing)).float()
                for t, v in zip(missing, (e / e.norm(dim=-1, keepdim=True)).numpy()):
                    self.texts[t] = v
                while len(self.texts) > TEXT_CACHE:
                    self.texts.popitem(last=False)
            return np.stack([self.texts[t] for t in texts])

    def faces(self, image: Image.Image) -> list[tuple[float, np.ndarray]]:
        """[(alan, birim uzunlukta 128 boyutlu kimlik vektörü)], büyükten küçüğe. Uzun kenarı DETECT_MAX_SIDE'dan büyük
        görsel önce küçültülür (alan küçültülmüş görseldedir)."""
        image = image.convert("RGB")
        if max(image.size) > DETECT_MAX_SIDE:
            r = DETECT_MAX_SIDE / max(image.size)
            image = image.resize((max(1, round(image.width * r)), max(1, round(image.height * r))), Image.LANCZOS)
        bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
        h, w = bgr.shape[:2]
        out = []
        with self.lock:
            self.detector.setInputSize((w, h))
            _, found = self.detector.detect(bgr)
            for row in found if found is not None else []:
                if min(row[2], row[3]) < MIN_FACE_PX:
                    continue
                feature = self.recognizer.feature(self.recognizer.alignCrop(bgr, row)).flatten().astype(np.float32)
                out.append((float(row[2] * row[3]), feature / np.linalg.norm(feature)))
        return sorted(out, key=lambda x: -x[0])

    def reference_face(self, key: str) -> np.ndarray | None:
        """Referans görseldeki en büyük yüzün vektörü (yüz yoksa None). Anahtarlar içerik-adreslidir: önbellek güvenlidir."""
        with self.lock:
            if key in self.refs:
                self.refs.move_to_end(key)
                return self.refs[key]
        from app import storage
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            path = storage.download(key, os.path.join(tmp, "ref" + os.path.splitext(key)[1]))
            with Image.open(path) as im:
                found = self.faces(im)
        vector = found[0][1] if found else None
        with self.lock:
            self.refs[key] = vector
            while len(self.refs) > REF_CACHE:
                self.refs.popitem(last=False)
        return vector


# ---------- ölçüm ----------

def identity(cosine: float) -> float:
    """SFace kosinüsünü 0-1'e çevirir: aynı kişi eşiğinde 0,5."""
    return 1.0 / (1.0 + math.exp(-(cosine - FACE_SAME) / FACE_SLOPE))


def positives(target: Target) -> dict[str, str]:
    texts = {"style": " ".join((target.style or "").split()), "content": " ".join((target.content or "").split())}
    texts = {k: v for k, v in texts.items() if v}
    return texts or {"generic": GENERIC}


def alignment(image_vectors: np.ndarray, text_vectors: np.ndarray, n_positive: int, scale: float) -> list[float]:
    """Her olumlu metin için: kareler üzerinden ortalama softmax olasılığı (o metin, rakip metinlere karşı).
    text_vectors: önce n_positive olumlu metin, sonra rakipler."""
    logits = scale * image_vectors @ text_vectors.T
    negatives = logits[:, n_positive:]
    out = []
    for i in range(n_positive):
        row = np.concatenate([logits[:, i:i + 1], negatives], axis=1)
        row = np.exp(row - row.max(axis=1, keepdims=True))
        out.append(float((row[:, 0] / row.sum(axis=1)).mean()))
    return out


def combine(clip: float, face: float | None, weight: float) -> float:
    return clip if face is None else (1.0 - weight) * clip + weight * face


def score_images(images: list[Image.Image], target: Target) -> Result:
    m = models()
    texts = positives(target)
    probs = alignment(m.encode_images(images), m.encode_texts(list(texts.values()) + list(RIVALS)), len(texts), m.scale)
    clip = float(np.mean(probs))
    seen = [vector for im in images for _, vector in m.faces(im)]
    best: dict[str, float] = {}
    for name, keys in target.faces:
        refs = [r for r in (m.reference_face(k) for k in keys) if r is not None]
        if refs and seen:
            best[name] = max(float(v @ r) for v in seen for r in refs)
    face = float(np.mean([identity(c) for c in best.values()])) if best else None
    composite = combine(clip, face, settings.score_face_weight)
    detail = {"scorer": SCORER_ID, "alignment": {k: round(p, 4) for k, p in zip(texts, probs)},
              "faces_seen": len(seen), "identity": {k: round(c, 4) for k, c in best.items()}}
    return Result(clip=round(clip, 4), face=None if face is None else round(face, 4), composite=round(composite, 4),
                  detail=detail)


def video_frames(path: str, n: int = SAMPLES) -> list[Image.Image]:
    """Videonun içinden eşit aralıkla n kare, tam çözünürlükte (yüzler küçülmesin): (i + 0,5) / n konumlarından."""
    from app.quality.anomaly import probe_duration
    duration = probe_duration(path)
    frames = []
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        for i in range(n):
            out = os.path.join(tmp, f"{i}.png")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{duration * (i + 0.5) / n:.3f}", "-i", path,
                            "-frames:v", "1", out], capture_output=True)
            if os.path.exists(out):
                with Image.open(out) as im:
                    frames.append(im.convert("RGB"))
    return frames


def score_video(path: str, target: Target) -> Result | None:
    """Okunabilir kare yoksa None (anomali tespiti zaten eler)."""
    frames = video_frames(path)
    return score_images(frames, target) if frames else None


def score_image(path: str, target: Target) -> Result | None:
    try:
        with Image.open(path) as im:
            image = im.convert("RGB")
    except Exception:                                   # noqa: BLE001 — bozuk kare: anomali tespiti eler
        return None
    return score_images([image], target)
