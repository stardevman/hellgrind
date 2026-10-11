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
DINO_DIR = "dinov2"                                        # facebook/dinov2-small (Apache 2.0); yoksa DINOv2 ölçümü atlanır
SCORER_ID = "openclip:xlm-roberta-base-ViT-B-32/laion5b_s13b_b90k+yunet-2023mar+sface-2021dec"
PACKAGES = ("torch", "open_clip", "cv2")

SAMPLES = 4                    # videodan örneklenen kare
FACE_SAME = 0.363              # OpenCV FaceRecognizerSF: aynı kişi için önerilen kosinüs eşiği
FACE_SLOPE = 0.05              # lojistik eğrinin genişliği: eşikte 0,5; +0,15'te ~0,95
DETECT_SCORE = 0.8             # YuNet güven eşiği
MIN_FACE_PX = 24               # bundan küçük yüzler kimlik için güvenilmez
MEASURE_PX = 80                # kimlik ancak kısa kenarı bu kadar olan yüzde ölçülür; küçük yüz "ölçülemedi" (eleme yok)
REF_MIN_SIDE = 1024            # küçük referans (ör. dönüş videosunun 384 px karesi) yüz aramadan önce büyütülür
LOOK_REFS = 4                  # insan olmayan karakterde görüntü benzerliğine giren en çok referans
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
    """Neye göre ölçülecek: stil metni, çekim tarifi ve karakterlerin referans görselleri ((ad, (anahtar, ...)), ...).
    faces: insan karakterler (SFace yüz kimliği); looks: insan olmayanlar (robot, hayvan: OpenCLIP görüntü benzerliği)."""
    style: str = ""
    content: str = ""
    faces: tuple = ()
    face_expected: bool = True        # yüz görünmeli mi (kimlik kapısı; core/h3mode.face_expected)
    looks: tuple = ()
    next_frame: str | None = None     # FL2VA last_frame hedefi: klibin son karesi ona benzemeli (varış skoru)
    arrival_kind: str | None = None   # next: sonraki çekimin ilk karesi K(n+1); end: çekimin son karesi Kn_end
    movement: str | None = None       # kamera hareketi (pan, dolly…): klipte görüldü mü kaydedilir
    style_frame: str | None = None    # yakın plan: stil metni yerine ölçek eşleşen onaylı kareye (frame_style) göre stil


def target_for(scene, chars, style) -> Target:
    """Çekimin ölçütü: kılavuzun ışık ve hareket dili, çekim tarifi ve referans görseli olan karakterler."""
    from app.core.chain import camera_of
    from app.core.h3mode import face_expected, is_human
    return Target(style=" ".join(x for x in ((style.lighting_prefix or "") if style else "",
                                             (style.motion_style or "") if style else "") if x),
                  content=(scene.prompt_spec or {}).get("prompt", ""),
                  faces=tuple((ch.name, ref_order(ch.reference_images)) for ch in chars
                              if ch.reference_images and is_human(ch)),
                  face_expected=face_expected(scene, chars),
                  movement=camera_of(scene).get("movement"),
                  looks=tuple((ch.name, ref_order(ch.reference_images)) for ch in chars
                              if ch.reference_images and not is_human(ch)))


def ref_order(keys) -> tuple:
    """Referanslar, en yüksek çözünürlüklü ve yüzü en net olandan: durağan görsel (Qwen-Image still, 1328 px), ifade
    sayfası, sonra dönüş videosu kareleri (384 px). Kimlik hepsine karşı ölçülür, en yüksek skor alınır."""
    rank = lambda k: 0 if "/still/" in k else 1 if "/expressions/" in k else 2      # noqa: E731
    return tuple(sorted(keys or (), key=rank))


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

    def faces(self, image: Image.Image) -> list[tuple[float, np.ndarray, float]]:
        """[(alan, birim uzunlukta 128 boyutlu kimlik vektörü, yüz kutusunun kısa kenarı — özgün çözünürlükte, px)],
        büyükten küçüğe. Uzun kenarı DETECT_MAX_SIDE'dan büyük görsel önce küçültülür (alan küçültülmüş görseldedir)."""
        image = image.convert("RGB")
        r = 1.0
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
                out.append((float(row[2] * row[3]), feature / np.linalg.norm(feature), float(min(row[2], row[3])) / r))
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
                im = im.convert("RGB")
                if max(im.size) < REF_MIN_SIDE:          # küçük kare: YuNet küçük yüzü bulamaz, SFace hizalaması bozulur
                    up = REF_MIN_SIDE / max(im.size)
                    im = im.resize((round(im.width * up), round(im.height * up)), Image.LANCZOS)
                found = self.faces(im)
        vector = found[0][1] if found else None
        with self.lock:
            self.refs[key] = vector
            while len(self.refs) > REF_CACHE:
                self.refs.popitem(last=False)
        return vector

    # ---------- DINOv2: nesne ve karakter kimliği, mekân sürekliliği ----------

    def _dino(self):
        """DINOv2 modeli (ilk kullanımda yüklenir); dosyaları yoksa None (ölçüm atlanır)."""
        if not hasattr(self, "_dino_model"):
            path = os.path.join(settings.quality_model_dir, DINO_DIR)
            self._dino_model = None
            if os.path.exists(os.path.join(path, "model.safetensors")):
                from transformers import AutoImageProcessor, AutoModel
                self._dino_model = (AutoImageProcessor.from_pretrained(path), AutoModel.from_pretrained(path).eval())
        return self._dino_model

    def encode_dino(self, images: list[Image.Image]) -> np.ndarray | None:
        dino = self._dino()
        if dino is None:
            return None
        processor, model = dino
        with self.lock, self.torch.no_grad():
            out = model(**processor(images=[im.convert("RGB") for im in images], return_tensors="pt")).pooler_output
        e = out.float().numpy()
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def reference_dino(self, key: str) -> np.ndarray | None:
        cache_key = "dino:" + key
        with self.lock:
            if cache_key in self.refs:
                return self.refs[cache_key]
        from app import storage
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            path = storage.download(key, os.path.join(tmp, "ref" + os.path.splitext(key)[1]))
            with Image.open(path) as im:
                vectors = self.encode_dino([im.convert("RGB")])
        vector = None if vectors is None else vectors[0]
        with self.lock:
            self.refs[cache_key] = vector
            while len(self.refs) > REF_CACHE:
                self.refs.popitem(last=False)
        return vector

    def reference_look(self, key: str) -> np.ndarray:
        """Referans görselin OpenCLIP görüntü gömmesi (insan olmayan karakterin görsel benzerliği için)."""
        cache_key = "look:" + key
        with self.lock:
            if cache_key in self.refs:
                self.refs.move_to_end(cache_key)
                return self.refs[cache_key]
        from app import storage
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            path = storage.download(key, os.path.join(tmp, "ref" + os.path.splitext(key)[1]))
            with Image.open(path) as im:
                vector = self.encode_images([im.convert("RGB")])[0]
        with self.lock:
            self.refs[cache_key] = vector
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


def crops(image: Image.Image) -> list[Image.Image]:
    """Karakteri karede bulmak için: 3x3 ızgarada yarım boy kırpımlar (karakter karenin bir bölümünü kaplar; tam kare
    gömmesi mekânı da taşır ve robotun anten sayısı gibi ayrıntıyı yakalamaz)."""
    w, h = image.size
    return [image.crop((int(x * w / 4), int(y * h / 4), int(x * w / 4 + w / 2), int(y * h / 4 + h / 2)))
            for x in range(3) for y in range(3)]


INSTANCE_MARGIN = 0.04          # ikinci örnek: en iyi kırpıma bu kadar yakın ve onunla örtüşmeyen kırpım


def crop_boxes() -> list[tuple[float, float, float, float]]:
    """crops() ile aynı sırada kırpım kutuları (0-1 koordinat): x0, y0, x1, y1."""
    return [(x / 4, y / 4, x / 4 + 0.5, y / 4 + 0.5) for x in range(3) for y in range(3)]


def _overlap(a, b) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 1e-6 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-6


def separate_instances(sims: list[float]) -> list[float]:
    """Bir karedeki kırpım benzerliklerinden ayrı örnekler: en iyiden başlayarak, en iyiye INSTANCE_MARGIN kadar yakın ve
    seçilenlerle örtüşmeyen kırpımlar (tek bir robotu kapsayan örtüşen kırpımlar tek örnek sayılır)."""
    boxes = crop_boxes()
    order = sorted(range(len(sims)), key=lambda i: -sims[i])
    if not order:
        return []
    best, picked = sims[order[0]], []
    for i in order:
        if sims[i] < best - INSTANCE_MARGIN:
            break
        if not any(_overlap(boxes[i], boxes[j]) for j in picked):
            picked.append(i)
    return [round(sims[i], 4) for i in picked]


def dino_scan(m, images: list[Image.Image], keys) -> tuple[float | None, list[list[float]]]:
    """İnsan olmayan karakter: (karelerin kırpımlarıyla referanslar arasındaki en yüksek DINOv2 benzerliği, kare başına ayrı
    örneklerin benzerlikleri). Kıvılcım SH05: kapı yalnızca en iyi eşleşmeye bakınca karedeki ikinci örneği görmüyordu."""
    refs = [r for r in (m.reference_dino(k) for k in keys[:LOOK_REFS * 3]) if r is not None]
    if not refs:
        return None, []
    vectors = m.encode_dino([c for im in images for c in crops(im)])
    if vectors is None:
        return None, []
    sims = [max(float(v @ r) for r in refs) for v in vectors]
    n = len(crop_boxes())
    per_image = [separate_instances(sims[k * n:(k + 1) * n]) for k in range(len(images))]
    return float(max(sims)), per_image


def dino_likeness(m, images: list[Image.Image], keys) -> float | None:
    """İnsan olmayan karakter: karelerin kırpımlarıyla referanslar arasındaki en yüksek DINOv2 benzerliği."""
    return dino_scan(m, images, keys)[0]


def character_mask(image: Image.Image, faces: list) -> Image.Image:
    """Karakterlerin kapladığı bölge griye boyanır: çerçevenin geniş orta-alt bölgesi (karakterler çoğunlukla orada; kadrajdaki
    karakter değişse de arka plan skoru düşmesin) ve iki karedeki yüzlerden tahmin edilen geniş gövde kutuları. Süreklilik
    mekânla (oda, yerleşim, nesneler, ışık) ölçülür. Kıvılcım zincir koşusu (dar -> geniş maske): SH02->SH03 0,67 -> 0,74
    (gözle aynı oda), SH01->SH02 0,85 -> 0,94, SH03->SH04 0,96 -> 0,97."""
    from PIL import ImageDraw
    out = image.convert("RGB").copy()
    w, h = out.size
    d = ImageDraw.Draw(out)
    d.rectangle([int(w * 0.12), int(h * 0.15), int(w * 0.88), h], fill=(128, 128, 128))
    for box in faces:
        x, y, fw, fh = box
        d.rectangle([int(x - 2 * fw), int(y - fh), int(x + 3 * fw), int(min(h, y + fh * 9))], fill=(128, 128, 128))
    return out


def face_boxes(m, image: Image.Image) -> list:
    """Yüz kutuları (x, y, w, h), özgün çözünürlükte."""
    image = image.convert("RGB")
    bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
    with m.lock:
        m.detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, found = m.detector.detect(bgr)
    return [tuple(float(v) for v in row[:4]) for row in (found if found is not None else [])]


def continuity(path: str, previous: str) -> float | None:
    """Kn ile K(n-1) arasında mekân benzerliği: karakter bölgeleri (iki karede de) griye boyanır, kalan görüntünün DINOv2
    gömmeleri karşılaştırılır (DINOv2 yoksa OpenCLIP); devam eşiği 0,70, kesme 0,60."""
    m = models()
    with Image.open(path) as a, Image.open(previous) as b:
        a, b = a.convert("RGB"), b.convert("RGB").resize(a.size)
    boxes = face_boxes(m, a) + face_boxes(m, b)
    pair = [character_mask(a, boxes), character_mask(b, boxes)]
    vectors = m.encode_dino(pair)
    if vectors is None:
        vectors = m.encode_images(pair)
    return round(float(vectors[0] @ vectors[1]), 4)


def last_frame(path: str) -> Image.Image | None:
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        out = os.path.join(tmp, "last.png")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.1", "-i", path, "-frames:v", "1", out],
                       capture_output=True)
        if not os.path.exists(out):
            return None
        with Image.open(out) as im:
            return im.convert("RGB")


def transition(path: str, next_key: str) -> float | None:
    """Klibin son karesi ile sonraki çekimin ilk karesi (K(n+1)) arasındaki DINOv2 benzerliği (yoksa OpenCLIP)."""
    from app import storage
    m = models()
    end = last_frame(path)
    if end is None:
        return None
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        with Image.open(storage.download(next_key, os.path.join(tmp, "next.jpg"))) as im:
            nxt = im.convert("RGB")
    vectors = m.encode_dino([end, nxt])
    if vectors is None:
        vectors = m.encode_images([end, nxt])
    return round(float(vectors[0] @ vectors[1]), 4)


ARRIVAL_DINO_WEIGHT = 0.7       # varış skoru: tam kare DINOv2 × 0,7 + son karedeki kimlik/benzerlik × 0,3


def arrival(path: str, target_key: str, target: Target) -> dict | None:
    """Klibin son karesi hedefe (K(n+1) ya da Kn_end) ne kadar vardı: {"score", "dino", "identity"}. dino: tam kare
    DINOv2 benzerliği (yoksa OpenCLIP); identity: son karedeki karakterlerin kimlik (insan) ya da benzerlik (insan olmayan)
    skorlarının ortalaması, karakter ya da ölçülebilir yüz yoksa None (skor = dino)."""
    from app import storage
    m = models()
    end = last_frame(path)
    if end is None:
        return None
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        with Image.open(storage.download(target_key, os.path.join(tmp, "target.jpg"))) as im:
            goal = im.convert("RGB")
    vectors = m.encode_dino([end, goal])
    if vectors is None:
        vectors = m.encode_images([end, goal])
    dino = round(float(vectors[0] @ vectors[1]), 4)
    ident = None
    if target.faces or target.looks:
        detail = score_images([end], target).detail
        values = [*(detail.get("identity") or {}).values(), *(detail.get("look") or {}).values()]
        ident = round(float(np.mean(values)), 4) if values else None
    score = dino if ident is None else ARRIVAL_DINO_WEIGHT * dino + (1 - ARRIVAL_DINO_WEIGHT) * ident
    return {"score": round(score, 4), "dino": dino, "identity": ident}


PATCH_MATCH = 0.55             # karakter yaması: referansın karakter yamalarına benzerliği bunun üstü ve arka plan yamalarından yüksek


def dino_patches(m, image: Image.Image, short: int = 224) -> np.ndarray | None:
    """DINOv2 yama gömmeleri (satır, sütun, boyut), kırpmasız (kısa kenar `short`)."""
    dino = m._dino()
    if dino is None:
        return None
    processor, model = dino
    inputs = processor(images=[image.convert("RGB")], do_center_crop=False, size={"shortest_edge": short},
                       return_tensors="pt")
    with m.lock, m.torch.no_grad():
        out = model(**inputs).last_hidden_state[0, 1:].float().numpy()
    h, w = inputs["pixel_values"].shape[2] // 14, inputs["pixel_values"].shape[3] // 14
    out = out[: h * w] / np.linalg.norm(out[: h * w], axis=1, keepdims=True)
    return out.reshape(h, w, -1)


def reference_patches(m, key: str):
    """Referansın (karakter ortada, tam boy) karakter yamaları (orta kutu) ve arka plan yamaları (sol ve üst şerit)."""
    cache_key = "patches:" + key
    with m.lock:
        if cache_key in m.refs:
            return m.refs[cache_key]
    from app import storage
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        with Image.open(storage.download(key, os.path.join(tmp, "ref" + os.path.splitext(key)[1]))) as im:
            p = dino_patches(m, im)
    value = None
    if p is not None:
        h, w, d = p.shape
        fg = p[int(h * .25):int(h * .9), int(w * .3):int(w * .75)].reshape(-1, d)
        bg = np.concatenate([p[:, : max(1, int(w * .2))].reshape(-1, d), p[: max(1, int(h * .2))].reshape(-1, d)])
        value = (fg, bg)
    with m.lock:
        m.refs[cache_key] = value
    return value


def patch_mask(m, image: Image.Image, key: str, short: int = 224) -> np.ndarray | None:
    """Karakter yamaları (satır x sütun, bool): referansın karakter yamalarına PATCH_MATCH'ten çok ve arka plan
    yamalarından daha çok benzeyenler."""
    try:
        ref = reference_patches(m, key)
        frame = dino_patches(m, image, short)
    except Exception as e:                               # noqa: BLE001
        log.warning("yama haritası çıkarılamadı: %s", e)
        return None
    if ref is None or frame is None:
        return None
    fg, bg = ref
    f = frame.reshape(-1, frame.shape[-1])
    s_fg, s_bg = (f @ fg.T).max(axis=1), (f @ bg.T).max(axis=1)
    return ((s_fg >= PATCH_MATCH) & (s_fg > s_bg)).reshape(frame.shape[:2])


def patch_coverage(m, image: Image.Image, key: str) -> float | None:
    """Karakterin kare alanına oranı (0-1): karakter yamalarının payı (patch_mask). Kıvılcım: SH03'teki küçük Bolt 0,09
    (eski kırpım ölçeği 0,36 diyordu), iki kişilik orta plan 0,10-0,16, Bolt'suz SH02 0,01."""
    mask = patch_mask(m, image, key)
    return None if mask is None else round(float(mask.mean()), 3)


def mask_box(mask: np.ndarray, width: int, height: int) -> dict | None:
    """Yama haritasındaki en büyük bağlı bölgenin kutusu (piksel) ve kutunun ne kadarının karakter olduğu:
    {"box": (x0, y0, x1, y1), "fill", "coverage"}. Bölge yoksa None."""
    import cv2
    if mask is None or not mask.any():
        return None
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if count < 2:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, area = (int(v) for v in stats[best][:5])
    sx, sy = width / mask.shape[1], height / mask.shape[0]
    return {"box": (round(x * sx), round(y * sy), round((x + w) * sx), round((y + h) * sy)),
            "fill": round(area / float(w * h), 3), "coverage": round(float(mask.mean()), 3)}


def character_box(image: Image.Image, target: Target, names=()) -> dict | None:
    """Karakterin karedeki kutusu (kırp + iyileştir için): {"name", "box": (x0, y0, x1, y1), "fill", "coverage",
    "kind": "patch" | "face", "head"?}. İnsan olmayan: DINOv2 yama haritasının en büyük bölgesi (448 kısa kenar: küçük
    karakter için daha ince ızgara). İnsan: en büyük yüzden gövde kutusu (3 x 7,5 yüz) ve baş-omuz kutusu (3 x 3,5 yüz).
    names verilirse yalnızca o karakterler; bulunamazsa None."""
    m = models()
    image = image.convert("RGB")
    w, h = image.size
    wanted = set(names or ())
    found = []
    for name, keys in target.looks:
        if wanted and name not in wanted or not keys:
            continue
        box = mask_box(patch_mask(m, image, keys[0], short=448), w, h)
        if box is not None:
            found.append({"name": name, "kind": "patch", **box})
    if any(name in wanted or not wanted for name, _ in target.faces):
        faces = sorted(face_boxes(m, image), key=lambda b: b[2] * b[3], reverse=True)
        if faces:
            x, y, fw, fh = faces[0]
            body = (max(0, round(x - fw)), max(0, round(y - 0.5 * fh)), min(w, round(x + 2 * fw)),
                    min(h, round(y + 7 * fh)))
            head = (max(0, round(x - fw)), max(0, round(y - 0.5 * fh)), min(w, round(x + 2 * fw)),
                    min(h, round(y + 3 * fh)))
            name = next((n for n, _ in target.faces if not wanted or n in wanted), "")
            found.append({"name": name, "kind": "face", "box": body, "head": head, "fill": 0.6,
                          "coverage": round((body[2] - body[0]) * (body[3] - body[1]) / float(w * h), 3)})
    if not found:
        return None
    return max(found, key=lambda b: (b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]))


FRAMING_SCALES = (1.0, 0.6, 0.35)   # insan olmayan karakter: referansa en çok benzeyen kırpımın boyu -> alan ≈ boy²


def framing(path: str, target: Target) -> float | None:
    """Kabaca karakter alanı / kare alanı (kadraj ölçeği kontrolü). İnsan: en büyük yüzden tahmin edilen gövde kutusu
    (genişlik 3 yüz, yükseklik 7,5 yüz, kare sınırında kesilir). İnsan olmayan: referansa (DINOv2) en çok benzeyen kırpım
    ölçeği. Karakter yoksa None."""
    m = models()
    with Image.open(path) as im:
        image = im.convert("RGB")
    w, h = image.size
    areas = []
    if target.faces:
        for x, y, fw, fh in face_boxes(m, image):
            bw = min(w, 3 * fw)
            bh = min(h - max(0.0, y - 0.5 * fh), 7.5 * fh)
            areas.append(max(0.0, bw * bh) / (w * h))
    for _, keys in target.looks:
        patch_area = patch_coverage(m, image, keys[0]) if keys else None
        if patch_area is not None:                       # DINOv2 yama haritası (Kıvılcım: küçük Bolt 0,09; iki kişilik 0,10-0,16)
            areas.append(patch_area)
            continue
        refs = [r for r in (m.reference_dino(k) for k in keys[:LOOK_REFS]) if r is not None]
        if not refs:
            continue
        best = None
        for side in FRAMING_SCALES:
            cw, ch = int(w * side), int(h * side)
            steps = [0.0, 0.5, 1.0] if side < 1.0 else [0.0]
            boxes = [(int((w - cw) * a), int((h - ch) * b)) for a in steps for b in steps]
            vectors = m.encode_dino([image.crop((x, y, x + cw, y + ch)) for x, y in boxes])
            if vectors is None:
                break
            value = max(float(v @ r) for v in vectors for r in refs)
            if best is None or value > best[0]:
                best = (value, side * side)
        if best is not None:
            areas.append(best[1])
    return round(max(areas), 3) if areas else None


def character_crop(image: Image.Image, keys) -> Image.Image:
    """Karakterin kırpımı: referansa (DINOv2; yoksa OpenCLIP) en çok benzeyen yarım boy kırpım. Referans yoksa kare."""
    m = models()
    pieces = crops(image)
    refs = [r for r in (m.reference_dino(k) for k in list(keys)[:LOOK_REFS]) if r is not None]
    vectors = m.encode_dino(pieces) if refs else None
    if vectors is None:
        refs = [m.reference_look(k) for k in list(keys)[:LOOK_REFS]]
        if not refs:
            return image
        vectors = m.encode_images(pieces)
    sims = [max(float(v @ r) for r in refs) for v in vectors]
    return pieces[int(np.argmax(sims))]


GLOW_REF = 0.6                  # parlama çekirdeği (‰, karakter kırpımında): bunun yarısının altı "kapalı" sayılır
GLOW_HUES = {"red": ((0, 15), (345, 360)), "orange": ((15, 40),), "amber": ((30, 45),), "yellow": ((45, 65),),
             "golden": ((40, 55),), "green": ((90, 150),), "cyan": ((170, 200),), "purple": ((260, 300),),
             "pink": ((300, 345),)}


def glow_fraction(image: Image.Image, color: str) -> float:
    """Karakter kırpımında ışık yayan renk çekirdeğinin payı (binde). Mavi: camgöbeği-beyaz çekirdek (G ≥ 215, B ≥ 235,
    R ≤ 200, B-R ≥ 50; yağmurlu pencerenin koyu mavisi sayılmaz). Diğer renkler: tonu tutan, doygun (S ≥ 0,35) ve çok
    parlak (V ≥ 0,92) pikseller; beyaz: doygunluğu düşük, V ≥ 0,97.
    Kıvılcım kalibrasyonu (Bolt kırpımı): iki göz karanlık 0,13; tek göz yanık 0,6-0,8; iki göz yanık 4,5-4,8; durağan
    görsel 5,9."""
    if color == "blue":
        a = np.asarray(image.convert("RGB")).astype(int)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        mask = (g >= 215) & (b >= 235) & (r <= 200) & (b - r >= 50)
        return round(float(lit_blobs(mask)) * 1000, 3)
    else:
        hsv = np.asarray(image.convert("HSV")).astype(float)
        h, s, v = hsv[..., 0] * 360 / 255, hsv[..., 1] / 255, hsv[..., 2] / 255
        if color == "white":
            mask = (s < 0.15) & (v >= 0.97)
        else:
            hue = np.zeros(h.shape, bool)
            for lo, hi in GLOW_HUES.get(color, ()):
                hue |= (h >= lo) & (h <= hi)
            mask = hue & (s >= 0.35) & (v >= 0.92)
    return round(float(lit_blobs(mask)) * 1000, 3)


BLOB_MIN = 0.0003              # bir ışık lekesinin kırpıma oranı en az bu kadar (binde 0,3): yanan göz yüzlerce piksel,
                               # karanlık camdaki pencere yansıması ve parıltı birkaç düzine


def lit_blobs(mask: np.ndarray) -> float:
    """Yalnızca ışık yayan parça olabilecek lekelerin payı: yeterince büyük (BLOB_MIN) ve toplu (en-boy 0,4-2,5, kutusunu
    en az %35 dolduran). Yağmur çizgileri ve yansıma parıltıları sayılmaz. Kıvılcım SH03 (yeni açı, iki göz karanlık):
    eski ölçü cam gözdeki yansımayı parlama saymıştı."""
    try:
        import cv2
    except ImportError:                                  # OpenCV yoksa eski ölçü (tüm çekirdek pikseller)
        return float(mask.mean())
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    total, keep = mask.size, 0
    for x, y, w, h, area in stats[1:n]:
        if area >= BLOB_MIN * total and 0.4 <= w / max(1, h) <= 2.5 and area / max(1, w * h) >= 0.35:
            keep += int(area)
    return keep / total


def state_probe(image: Image.Image, probes: list[dict]) -> dict[str, dict]:
    """Durum sondası (core/states.py): {etiket: {"score": 0-1, "glow": binde | None, "clip": 0-1}}; score 0,5 altı =
    istenen durum tutmadı.
    - Işık rengi biliniyorsa (değişmezde "glow blue"): karar karakter kırpımındaki parlama çekirdeğiyle verilir
      (kapalı: 1 - glow / GLOW_REF; açık: glow / GLOW_REF). Kıvılcım kalibrasyonunda OpenCLIP sıfır-örnek sondaları yanık ve
      karanlık gözleri ayıramadı (karanlık 0,32; tek göz yanık 0,10-0,14; iki göz yanık 0,17-0,28).
    - Bilinmiyorsa OpenCLIP: kırpımda istenen metnin karşıtına göre olasılığı (iki sınıflı softmax). Her iki değer de
      kaydedilir."""
    m = models()
    out = {}
    for probe in probes:
        crop = character_crop(image, probe.get("keys") or ())
        iv = m.encode_images([crop])[0]
        tv = m.encode_texts([probe["wanted"], probe["opposite"]])
        logits = np.array([float(iv @ tv[0]), float(iv @ tv[1])]) * float(m.scale)
        logits -= logits.max()
        clip = float((np.exp(logits) / np.exp(logits).sum())[0])
        glow = glow_fraction(crop, probe["color"]) if probe.get("color") else None
        if glow is None:
            score = clip
        elif probe.get("key") == "on":
            score = min(1.0, glow / GLOW_REF)
        else:
            score = max(0.0, 1.0 - glow / GLOW_REF)
        out[probe["label"]] = {"score": round(score, 4), "glow": glow, "clip": round(clip, 4)}
    return out


def state_probe_path(path: str, probes: list[dict]) -> dict[str, float]:
    try:
        with Image.open(path) as im:
            image = im.convert("RGB")
    except Exception:                                   # noqa: BLE001 — bozuk kare: anomali tespiti eler
        return {}
    return state_probe(image, probes)


def camera_change(previous_path: str, path: str) -> dict | None:
    """Jump cut ölçüsü (quality/camera.same_camera): iki kare aynı kamerayla mı."""
    from app.quality import camera
    try:
        with Image.open(previous_path) as a, Image.open(path) as b:
            return camera.same_camera(a.convert("RGB"), b.convert("RGB"))
    except Exception as e:                               # noqa: BLE001 — ölçülemedi: kapı uygulanmaz
        log.warning("kamera değişimi ölçülemedi: %s", e)
        return None


def cut_in_view(previous_path: str, path: str, rect: tuple = (0.0, 0.0, 1.0, 1.0)) -> dict | None:
    """Kırp + iyileştir kontrolü (quality/camera.cut_in_view): kare, önceki karenin kırpılan bölgesinin yakın görüntüsü mü."""
    from app.quality import camera
    try:
        with Image.open(previous_path) as a, Image.open(path) as b:
            return camera.cut_in_view(a.convert("RGB"), b.convert("RGB"), rect)
    except Exception as e:                               # noqa: BLE001 — ölçülemedi: kapı uygulanmaz
        log.warning("cut-in görünümü ölçülemedi: %s", e)
        return None


def clip_motion(path: str, movement: str) -> dict | None:
    """Klipte kamera hareketi (quality/camera.camera_motion): ilk ve son kare."""
    from app.quality import camera
    frames = video_frames(path, 2)
    if len(frames) < 2:
        return None
    try:
        return camera.camera_motion(frames[0], frames[-1], movement)
    except Exception as e:                               # noqa: BLE001
        log.warning("kamera hareketi ölçülemedi: %s", e)
        return None


# Kadraja duyarlı stil (Kıvılcım SH03): ışık dili metni ("warm tungsten desk lamp… rain light from the window") yakın
# planda görünmeyen ögeleri anlatır; OpenCLIP softmax'ı 0,06-0,33 verdi ve iyi klipler low_clip ile elendi. Yakın planda
# stil, çekimin onaylı karesine (ölçek eşleşen stil referansı) göre ölçülür: OpenCLIP görüntü benzerliği × LAB palet
# yakınlığı. Kalibrasyon: SH03 klipleri kendi karesine 0,92-0,94 / ΔE 1,5-2,0; geniş SH01 karesine 0,63-0,65; gri
# tonlanmış SH03 0,95 / ΔE 10 (benzerlik rengi görmez, palet yakalar); stüdyo görseli 0,84 / ΔE 25.
STYLE_SIM = (0.70, 0.90)        # görüntü benzerliği: 0,70 -> 0, 0,90 -> 1
STYLE_DE = (3.0, 9.0)           # LAB ortalama farkı: <= 3 -> 1, >= 9 -> 0
# Küçük karakter (Kıvılcım SH04/SH06 geniş plan, Bolt'un yama alanı 0,02-0,09): DINOv2 kırpım benzerliği doğru Bolt'ta
# 0,70-0,81 arasında dağılıyor; bu alanda "ölçülemedi" sayılır (yüzdeki 80 px kuralı gibi), OpenCLIP benzerliği yine geçmeli.
SMALL_LOOK_AREA = 0.10


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def lab_mean(image: Image.Image) -> np.ndarray:
    """Tam karenin LAB ortalaması (L 0-100, a/b ±128)."""
    import cv2
    a = cv2.cvtColor(np.asarray(image.convert("RGB").resize((640, 352))), cv2.COLOR_RGB2LAB).astype(float)
    a[..., 0] *= 100 / 255
    a[..., 1:] -= 128
    return a.reshape(-1, 3).mean(axis=0)


def frame_style(m, vectors: np.ndarray, images: list[Image.Image], reference: Image.Image) -> dict:
    """{"score", "similarity", "delta_e"}: karelerin onaylı kareye stil yakınlığı (0-1)."""
    ref_vector = m.encode_images([reference])[0]
    ref_lab = lab_mean(reference)
    sims = [float(v @ ref_vector) for v in vectors]
    des = [float(np.linalg.norm(lab_mean(im) - ref_lab)) for im in images]
    per = [_clip01((s - STYLE_SIM[0]) / (STYLE_SIM[1] - STYLE_SIM[0]))
           * _clip01(1 - (d - STYLE_DE[0]) / (STYLE_DE[1] - STYLE_DE[0])) for s, d in zip(sims, des)]
    return {"score": round(float(np.mean(per)), 4), "similarity": round(float(np.mean(sims)), 4),
            "delta_e": round(float(np.mean(des)), 2)}


def _style_reference(key: str) -> Image.Image | None:
    """Onaylı kare (anahtarı içerik-adresli değil: yeniden çizim aynı anahtara yazar, önbelleğe alınmaz)."""
    from app import storage
    os.makedirs(settings.work_dir, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            with Image.open(storage.download(key, os.path.join(tmp, "style" + os.path.splitext(key)[1]))) as im:
                return im.convert("RGB")
    except Exception as e:                               # noqa: BLE001 — okunamadı: metin stili kalır
        log.warning("stil referansı okunamadı: %s", e)
        return None


def score_images(images: list[Image.Image], target: Target) -> Result:
    """identity: insan karakter başına 0-1 kimlik skoru (SFace kosinüsünün aynı kişi eşiği 0,363 çevresinde lojistik
    eğrisi; kapı bununla identity_threshold'u karşılaştırır), identity_cosine: ham kosinüs. Yalnızca kısa kenarı
    MEASURE_PX'ten büyük yüzler ölçülür (faces_measured); daha küçük yüz "ölçülemedi" sayılır, eleme sebebi değildir.
    look: insan olmayan karakter başına karelerle referans görseller arasındaki en yüksek OpenCLIP görüntü benzerliği."""
    m = models()
    texts = positives(target)
    vectors = m.encode_images(images)
    probs = alignment(vectors, m.encode_texts(list(texts.values()) + list(RIVALS)), len(texts), m.scale)
    style_ref = None
    if target.style_frame and "style" in texts and (reference := _style_reference(target.style_frame)) is not None:
        i = list(texts).index("style")
        style_ref = {**frame_style(m, vectors, images, reference), "text": round(probs[i], 4)}
        probs[i] = style_ref["score"]
    clip = float(np.mean(probs))
    per_image = [m.faces(im) for im in images] if target.faces else []
    found = [f for fs in per_image for f in fs]
    measured = [vector for _, vector, side in found if side >= MEASURE_PX]
    face_refs = {name: [r for r in (m.reference_face(k) for k in keys) if r is not None] for name, keys in target.faces}
    best: dict[str, float] = {}
    for name, refs in face_refs.items():
        if refs and measured:
            best[name] = max(float(v @ r) for v in measured for r in refs)
    # Örnek sayısı: her ölçülebilir yüz en çok benzediği karaktere sayılır (kare başına kimlik skorları); kapı aynı
    # karaktere eşiği geçen birden fazla yüz görürse eler ("karede birden fazla").
    face_instances: dict[str, list[list[float]]] = {name: [] for name, refs in face_refs.items() if refs}
    for fs in per_image:
        counts: dict[str, list[float]] = {name: [] for name in face_instances}
        for _, vector, side in fs:
            scores = {name: max(float(vector @ r) for r in face_refs[name]) for name in face_instances}
            if side >= MEASURE_PX and scores:
                who = max(scores, key=scores.get)
                counts[who].append(round(identity(scores[who]), 4))
        for name, values in counts.items():
            face_instances[name].append(values)
    looks: dict[str, float] = {}
    dinos: dict[str, float] = {}
    look_instances: dict[str, list[list[float]]] = {}
    look_area: dict[str, float] = {}
    for name, keys in target.looks:
        areas = [a for a in (patch_coverage(m, im, keys[0]) for im in images) if a is not None] if keys else []
        if areas:
            look_area[name] = round(float(np.median(areas)), 3)
        refs = [m.reference_look(k) for k in keys[:LOOK_REFS]]
        if refs:
            looks[name] = max(float(v @ r) for v in vectors for r in refs)
        d, instances = dino_scan(m, images, keys)
        if d is not None:
            dinos[name], look_instances[name] = d, instances
    face = float(np.mean([identity(c) for c in best.values()])) if best else None
    composite = combine(clip, face, settings.score_face_weight)
    detail = {"scorer": SCORER_ID, "alignment": {k: round(p, 4) for k, p in zip(texts, probs)},
              "faces_seen": len(found), "faces_measured": len(measured),
              "identity": {k: round(identity(c), 4) for k, c in best.items()},
              "identity_cosine": {k: round(c, 4) for k, c in best.items()},
              **({"face_instances": face_instances} if face_instances else {}),
              **({"look_instances": look_instances} if look_instances else {}),
              **({"look": {k: round(v, 4) for k, v in looks.items()}} if target.looks else {}),
              **({"look_dino": {k: round(v, 4) for k, v in dinos.items()}} if dinos else {}),
              **({"look_area": look_area} if look_area else {}),
              **({"style_ref": style_ref} if style_ref else {})}
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
