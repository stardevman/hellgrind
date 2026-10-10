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


def target_for(scene, chars, style) -> Target:
    """Çekimin ölçütü: kılavuzun ışık ve hareket dili, çekim tarifi ve referans görseli olan karakterler."""
    from app.core.h3mode import face_expected, is_human
    return Target(style=" ".join(x for x in ((style.lighting_prefix or "") if style else "",
                                             (style.motion_style or "") if style else "") if x),
                  content=(scene.prompt_spec or {}).get("prompt", ""),
                  faces=tuple((ch.name, ref_order(ch.reference_images)) for ch in chars
                              if ch.reference_images and is_human(ch)),
                  face_expected=face_expected(scene, chars),
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


def dino_likeness(m, images: list[Image.Image], keys) -> float | None:
    """İnsan olmayan karakter: karelerin kırpımlarıyla referanslar arasındaki en yüksek DINOv2 benzerliği."""
    refs = [r for r in (m.reference_dino(k) for k in keys[:LOOK_REFS * 3]) if r is not None]
    if not refs:
        return None
    vectors = m.encode_dino([c for im in images for c in crops(im)])
    if vectors is None:
        return None
    return float(max(float(v @ r) for v in vectors for r in refs))


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


def score_images(images: list[Image.Image], target: Target) -> Result:
    """identity: insan karakter başına 0-1 kimlik skoru (SFace kosinüsünün aynı kişi eşiği 0,363 çevresinde lojistik
    eğrisi; kapı bununla identity_threshold'u karşılaştırır), identity_cosine: ham kosinüs. Yalnızca kısa kenarı
    MEASURE_PX'ten büyük yüzler ölçülür (faces_measured); daha küçük yüz "ölçülemedi" sayılır, eleme sebebi değildir.
    look: insan olmayan karakter başına karelerle referans görseller arasındaki en yüksek OpenCLIP görüntü benzerliği."""
    m = models()
    texts = positives(target)
    vectors = m.encode_images(images)
    probs = alignment(vectors, m.encode_texts(list(texts.values()) + list(RIVALS)), len(texts), m.scale)
    clip = float(np.mean(probs))
    found = [f for im in images for f in m.faces(im)] if target.faces else []
    measured = [vector for _, vector, side in found if side >= MEASURE_PX]
    best: dict[str, float] = {}
    for name, keys in target.faces:
        refs = [r for r in (m.reference_face(k) for k in keys) if r is not None]
        if refs and measured:
            best[name] = max(float(v @ r) for v in measured for r in refs)
    looks: dict[str, float] = {}
    dinos: dict[str, float] = {}
    for name, keys in target.looks:
        refs = [m.reference_look(k) for k in keys[:LOOK_REFS]]
        if refs:
            looks[name] = max(float(v @ r) for v in vectors for r in refs)
        if (d := dino_likeness(m, images, keys)) is not None:
            dinos[name] = d
    face = float(np.mean([identity(c) for c in best.values()])) if best else None
    composite = combine(clip, face, settings.score_face_weight)
    detail = {"scorer": SCORER_ID, "alignment": {k: round(p, 4) for k, p in zip(texts, probs)},
              "faces_seen": len(found), "faces_measured": len(measured),
              "identity": {k: round(identity(c), 4) for k, c in best.items()},
              "identity_cosine": {k: round(c, 4) for k, c in best.items()},
              **({"look": {k: round(v, 4) for k, v in looks.items()}} if target.looks else {}),
              **({"look_dino": {k: round(v, 4) for k, v in dinos.items()}} if dinos else {})}
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
