"""Varlık referansları: yükleme doğrulaması, dönüş videosundan kare örnekleme, yerel vektörler ve yerel taslak referans.

Her şey yereldir ve ücretsizdir. Dosyalar içerik-adreslidir: aynı dosya aynı anahtarı üretir, zaten varsa yeniden yüklenmez.
  assets/{varlık}/{tür}/{sha}{uzantı}       yüklenen referans
  assets/{varlık}/frames/{sha}_{i}.jpg     dönüş videosundan eşit aralıkla örneklenen kareler (ilki yüz referansı)
  assets/{varlık}/thumbs/{sha}.jpg          panel için küçük resim

Vektör: karelerin HSV histogramlarının (16 kutu x 3 kanal) ortalaması. Gerçek yüz vektörleri (InsightFace) ve Pinecone
ücretli aşamadadır (Faz 18); buradaki vektör aynı şemayı doldurur ve referans değişimini görünür kılar.
"""
import hashlib
import os
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image

from app import storage
from app.config import settings
from app.core.hashing import seed_for
from app.engine.procedural import render_clip
from app.quality.anomaly import probe_duration

# tür -> (medya, Türkçe ad, yalnızca karakter mi)
REFERENCE_KINDS = {
    "still": ("image", "Durağan görsel", False),
    "turnaround": ("video", "Dönüş videosu (360°)", False),
    "expressions": ("image", "İfade sayfası", True),
    "voice_sample": ("audio", "Ses örneği", True),
}
VISUAL_KINDS = ("still", "turnaround", "expressions")
SAMPLE_FRAMES = 8
FRAME_WIDTH = 384
THUMB_WIDTH = 320
HIST_BINS = 16
VECTOR_MODEL = "hsv-hist-48@local"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
EXTENSIONS = {"image": (".jpg", ".jpeg", ".png", ".webp"), "video": (".mp4", ".mov", ".webm", ".mkv"),
              "audio": (".wav", ".mp3", ".ogg", ".m4a", ".flac")}


class InvalidReference(ValueError):
    """Yüklenen dosya bu referans türü için geçerli değil (mesaj Türkçe, kullanıcıya gösterilir)."""


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True)


def _streams(path: str) -> set[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.split()
    return set(out)


def sha_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:32]


# ---------- vektörler ----------

def image_vector(img: Image.Image) -> np.ndarray:
    """HSV histogramı: 3 kanal x 16 kutu, her kanal kendi içinde normalize."""
    hsv = np.asarray(img.convert("RGB").convert("HSV"))
    parts = [np.histogram(hsv[..., c], bins=HIST_BINS, range=(0, 256))[0].astype(float) for c in range(3)]
    return np.concatenate([p / max(p.sum(), 1.0) for p in parts])


def mean_vector(vectors: list[np.ndarray]) -> list[float]:
    v = np.mean(vectors, axis=0)
    v = v / max(float(np.linalg.norm(v)), 1e-9)
    return [round(float(x), 5) for x in v]


# ---------- doğrulama ve işleme ----------

def _validate(kind: str, path: str, filename: str) -> str:
    """Dosyanın türe uyduğunu denetler ve uzantıyı döndürür."""
    media = REFERENCE_KINDS[kind][0]
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in EXTENSIONS[media]:
        raise InvalidReference(f"{REFERENCE_KINDS[kind][1]} için desteklenen uzantılar: {', '.join(EXTENSIONS[media])}")
    if media == "image":
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception:
            raise InvalidReference("Görsel okunamadı: dosya bozuk ya da görsel değil.")
    else:
        need = "video" if media == "video" else "audio"
        if need not in _streams(path):
            raise InvalidReference(f"Dosyada {'görüntü' if need == 'video' else 'ses'} akışı yok.")
        try:
            if probe_duration(path) <= 0:
                raise InvalidReference("Dosyanın süresi 0.")
        except (subprocess.CalledProcessError, ValueError):
            raise InvalidReference("Dosyanın süresi okunamadı.")
    return ".jpg" if ext == ".jpeg" else ext


def _put_jpg(img: Image.Image, key: str, width: int) -> str:
    if not storage.exists(key):
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=settings.work_dir) as f:
            im = img.convert("RGB")
            im.thumbnail((width, width * 4))
            im.save(f.name, "JPEG", quality=85)
            storage.put_file(f.name, key, "image/jpeg")
    return key


def sample_frames(video: str, n: int = SAMPLE_FRAMES) -> list[Image.Image]:
    """Videodan eşit aralıkla n kare: 0, d/n, 2d/n ... İlk kare (dönüşün başı, önden görünüş) yüz referansıdır."""
    duration = probe_duration(video)
    frames = []
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        for i in range(n):
            out = os.path.join(tmp, f"{i:02d}.png")
            _ffmpeg("-ss", f"{duration * i / n:.3f}", "-i", video, "-frames:v", "1", out)
            if os.path.exists(out):
                with Image.open(out) as im:
                    frames.append(im.copy())
    if not frames:
        raise InvalidReference("Dönüş videosundan kare okunamadı.")
    return frames


def process(asset_id: str, kind: str, path: str, filename: str, content_type: str | None = None) -> dict:
    """Referansı doğrular, depoya yükler ve türüne göre işler. Varlığın `references[kind]` girdisini döndürür."""
    if kind not in REFERENCE_KINDS:
        raise InvalidReference(f"Bilinmeyen referans türü: {kind}")
    os.makedirs(settings.work_dir, exist_ok=True)
    ext = _validate(kind, path, filename)
    sha = sha_of(path)
    key = f"assets/{asset_id}/{kind}/{sha}{ext}"
    if not storage.exists(key):
        storage.put_file(path, key, content_type or "application/octet-stream")
    entry = {"key": key, "sha": sha, "filename": os.path.basename(filename or key), "uploaded_at": time.time()}

    media = REFERENCE_KINDS[kind][0]
    if media == "image":
        with Image.open(path) as img:
            img.load()
            entry["thumb"] = _put_jpg(img, f"assets/{asset_id}/thumbs/{sha}.jpg", THUMB_WIDTH)
            entry["vector"], entry["vector_model"] = mean_vector([image_vector(img)]), VECTOR_MODEL
    elif media == "video":
        frames = sample_frames(path)
        entry["duration_s"] = round(probe_duration(path), 2)
        entry["frames"] = [_put_jpg(f, f"assets/{asset_id}/frames/{sha}_{i:02d}.jpg", FRAME_WIDTH)
                           for i, f in enumerate(frames)]
        entry["face_ref"] = entry["frames"][0]
        entry["thumb"] = entry["frames"][0]
        entry["vector"], entry["vector_model"] = mean_vector([image_vector(f) for f in frames]), VECTOR_MODEL
    else:
        entry["duration_s"] = round(probe_duration(path), 2)
    return entry


def visual_keys(references: dict) -> list[str]:
    """Karakterin `reference_images` alanı: dönüş videosu kareleri, sonra durağan görsel ve ifade sayfası."""
    keys = list((references.get("turnaround") or {}).get("frames", []))
    keys += [references[k]["key"] for k in ("still", "expressions") if references.get(k)]
    return keys


# ---------- yerel taslak referans ----------

def placeholder(asset, out_dir: str) -> tuple[str, str, str]:
    """Ücretsiz modda yönetmenin elinde dosya yokken: prosedürel motordan taslak referans.
    Karakter: 4 sn'lik yörünge (dönüş) videosu. Mekân ve nesne: durağan görsel. (tür, yol, dosya adı) döndürür."""
    seed = seed_for(str(asset.id), asset.version, "placeholder")
    label = f"{asset.name} · yerel taslak"
    if asset.type == "character":
        path = os.path.join(out_dir, "turnaround.mp4")
        render_clip(path, seed, 640, 360, 4.0, settings.local_render_fps, emotion="neutral",
                    camera={"shot": "medium", "movement": "orbit"}, characters=[asset.name], label=label)
        return "turnaround", path, "turnaround.mp4"
    clip = os.path.join(out_dir, "still.mp4")
    camera = {"shot": "wide", "movement": "pan"} if asset.type == "location" else {"shot": "close_up", "movement": "static"}
    render_clip(clip, seed, 960, 540, 1.0, settings.local_render_fps, emotion="neutral", camera=camera, label=label)
    path = os.path.join(out_dir, "still.png")
    _ffmpeg("-ss", "0.5", "-i", clip, "-frames:v", "1", path)
    return "still", path, "still.png"
