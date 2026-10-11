"""Varlık referansları: yükleme doğrulaması, dönüş videosundan kare örnekleme, yerel vektörler ve yerel taslak referans.

İşleme yereldir ve ücretsizdir. Referans üretimi ücretsiz modda prosedürel taslaktır; ücretli modda (Faz 18) karakter için
Flux karesinden yörünge hareketli dönüş videosu, mekân ve nesne için tek bir Flux karesidir. Dosyalar içerik-adreslidir: aynı dosya aynı anahtarı üretir, zaten varsa yeniden yüklenmez.
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
    # Durum referansı (core/states.py): çekim o durumdaysa kimlik referansı olarak bu görsel verilir (ör. Bolt kapalı,
    # gözler karanlık). Görüntü kimliğini değiştirmez: sürüm artmaz, varlık taslağa dönmez, kilitliyken de yüklenir.
    "state_off": ("image", "Durum referansı: kapalı (ör. gözler karanlık)", True),
    "state_on": ("image", "Durum referansı: açık (ör. gözler yanık)", True),
}
STATE_REF_KINDS = ("state_off", "state_on")
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


def streams(path: str) -> set[str]:
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
        if need not in streams(path):
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


# ---------- referans üretimi ----------

TURNAROUND_S, TURNAROUND_RESOLUTION = 4.0, "480p"
REFERENCE_SHOT = {   # tür -> (tarif, kamera): dönüş videosu (karakter) ve eski Flux yolu
    "character": ("Character reference turnaround: full body, neutral pose and expression, plain light grey studio "
                  "background, the camera orbits 360 degrees around the character", {"shot": "medium", "movement": "orbit"}),
    "location": ("Location reference: establishing view of the empty location, no people", {"shot": "wide"}),
    "prop": ("Prop reference: the object alone, centered, plain light grey background", {"shot": "close_up"}),
}
REFERENCE_STILL = {  # tür -> (tarif, kamera): yüksek çözünürlüklü durağan referans (Qwen-Image-2512)
    "character": ("Character reference sheet: full body, standing in a neutral pose facing the camera, the face clearly "
                  "visible, sharp and evenly lit, neutral expression, plain neutral light grey studio background, soft "
                  "even lighting, single character, no text", {"shot": "medium", "movement": "static"}),
    "location": ("Location reference: establishing view of the empty location, no people, no text",
                 {"shot": "wide", "movement": "static"}),
    "prop": ("Prop reference: the object alone, centered, plain neutral light grey background, no text",
             {"shot": "close_up", "movement": "static"}),
}
STILL_SIZES = {"character": (1328, 1328), "prop": (1328, 1328), "location": (1664, 928)}   # Qwen-Image'ın doğal boyları
KEYFRAME_PIXELS = 1280 * 704                     # pricing.yaml › keyframe_seconds bu boyda ölçüldü


def reference_kinds(asset_type: str) -> tuple[str, ...]:
    """Üretilebilen referanslar: karakterde önce durağan görsel, isteğe bağlı ikinci adım dönüş videosu."""
    return ("still", "turnaround") if asset_type == "character" else ("still",)


def reference_prompt(asset, style, kind: str = "still"):
    """Referansın prompt'u da besteciden geçer: varlık, filmdeki ışık diliyle ve kendi render kuralıyla tarif edilir."""
    from app.style.composer import compose_prompt
    text, camera = (REFERENCE_SHOT if kind == "turnaround" else REFERENCE_STILL)[asset.type]
    return compose_prompt({"prompt": text, "camera": camera,
                           "characters": [asset.name] if asset.type == "character" else []}, [asset], style)


def still_fallback() -> bool:
    """Qwen-Image iş akışı RunPod'da yoksa fal.ai Flux'a düşülsün mü (REFERENCE_FAL_FALLBACK; varsayılan kapalı:
    FLUX.1 [dev] ağırlıkları ticari olmayan lisanslıdır)."""
    return bool(settings.reference_fal_fallback)


def reference_estimate(asset_type: str, kind: str = "still") -> tuple[str, float]:
    """(defter etiketi, tahmin): dönüş videosu = 4 sn 480p MiniMax H3; durağan görsel = RunPod'da Qwen-Image-2512 karesi
    (sıcak worker süresi, boyla orantılı). Ücretsiz modda yerel taslak da bu fiyatla sanal olarak bütçeden düşer."""
    from app.budget import pricing
    from app.engine.providers import default_engine
    if kind == "turnaround":
        engine = default_engine()
        return f"{engine}:{TURNAROUND_RESOLUTION}:reference", \
            pricing.estimate_video(engine, TURNAROUND_RESOLUTION, TURNAROUND_S)
    w, h = STILL_SIZES[asset_type]
    if settings.keyframe_platform == "fal":         # fal.ai Qwen-Image-2512: megapiksel başına (yukarı yuvarlanır)
        return "h3:reference:qwen", pricing.estimate_fal_image("qwen_image", w, h)
    return "h3:reference:qwen", round(pricing.estimate_keyframe("h3", model="qwen") * (w * h) / KEYFRAME_PIXELS, 4)


def paid_reference(project_id: str, asset, style, lora: dict, out_dir: str, kind: str = "still") -> tuple[str, str, str]:
    """Ücretli referans (Faz 18). Durağan görsel: RunPod worker'ında Qwen-Image-2512 (keyframe.json; karakter 1328x1328,
    düz arka plan, tam boy, yüz net). Dönüş videosu (karakter, isteğe bağlı ikinci adım): MiniMax H3, durağan görsel varsa
    ondan başlar. fal.ai Flux yalnızca REFERENCE_FAL_FALLBACK açıksa. Kilit rezervasyondan önce çalışır; geçici hatalar
    yeniden denenir (gönderilmiş iş yeniden ödenmez). (tür, yol, dosya adı)."""
    from dataclasses import replace
    from app.engine import paid
    from app.engine.providers import GenRequest, default_engine, get_provider
    seed = seed_for(str(asset.id), asset.version, "reference", *([kind] if kind != "turnaround" else []))
    composed = reference_prompt(asset, style, kind)
    label, estimate = reference_estimate(asset.type, kind)
    if kind == "turnaround":
        provider = get_provider(default_engine())
        req = GenRequest(prompt=composed.prompt, negative=composed.negative, seed=seed, resolution=TURNAROUND_RESOLUTION,
                         duration_s=TURNAROUND_S, extra={**lora, "project_id": project_id})
        still = (asset.references or {}).get("still")
        if still:                                    # onaylı durağan görsel: dönüş o yüzden başlar
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                local = storage.download(still["key"], os.path.join(tmp, "still" + os.path.splitext(still["key"])[1]))
                req = replace(req, image_url=provider.upload(local))
        made = paid.call(project_id, estimate, label, provider.key_name, lambda: provider.generate(req), asset_id=asset.id)
        path = os.path.join(out_dir, "turnaround.mp4")
        os.replace(made.local_path, path)
        return "turnaround", path, "turnaround.mp4"
    from app.engine import frames                   # fal.ai (varsayılan) ya da RunPod worker'ı
    if (frames.platform() == "fal" or settings.h3_platform == "runpod") and frames.ready("keyframe"):
        w, h = STILL_SIZES[asset.type]
        made = paid.call(project_id, estimate, label, frames.key_name(),
                         lambda: frames.keyframe_image(composed.prompt, seed, project_id, width=w, height=h),
                         asset_id=asset.id)
        ext = os.path.splitext(made.local_path)[1] or ".png"
        path = os.path.join(out_dir, "still" + ext)
        os.replace(made.local_path, path)
        return "still", path, "still" + ext
    if not (still_fallback() and paid.available("fal_key")):
        from app.live import PaidCallBlocked
        raise PaidCallBlocked("Referans görseli Qwen-Image-2512 ile üretilir: KEYFRAME_PLATFORM=fal için fal.ai anahtarı, "
                              "runpod için RunPod anahtarı ve keyframe.json iş akışı gerekli (fal.ai Flux'a düşme "
                              "REFERENCE_FAL_FALLBACK ile açılır; FLUX.1 [dev] ticari olmayan lisanslıdır).")
    from app.live import fal_video                  # ücretli modül: yalnızca fonksiyon içinde (isteğe bağlı yedek)
    frame = paid.call(project_id, pricing_image(), "flux_lora:reference", "fal_key",
                      lambda: fal_video.image(composed.prompt, seed), asset_id=asset.id)
    path = os.path.join(out_dir, "still.jpg")
    os.replace(frame.local_path, path)
    return "still", path, "still.jpg"


def pricing_image() -> float:
    from app.budget import pricing
    return pricing.estimate_image(1.0)


def motion_source(references: dict) -> str | None:
    """Hareket testinin başlangıç karesi (ücretli mod): onaylı durağan görsel, yoksa dönüş videosunun ilk karesi,
    yoksa ifade sayfası. Böylece sınanan, onaylanan referansın kendisidir."""
    if references.get("still"):
        return references["still"]["key"]
    frames = (references.get("turnaround") or {}).get("frames") or []
    if frames:
        return frames[0]
    return (references.get("expressions") or {}).get("key")


# ---------- yerel taslak referans ----------

def placeholder(asset, out_dir: str, kind: str | None = None) -> tuple[str, str, str]:
    """Ücretsiz modda yönetmenin elinde dosya yokken: prosedürel motordan taslak referans.
    Karakter: 4 sn'lik yörünge (dönüş) videosu (kind="still": tek kare). Mekân ve nesne: durağan görsel.
    (tür, yol, dosya adı) döndürür."""
    seed = seed_for(str(asset.id), asset.version, "placeholder", *([kind] if kind == "still" else []))
    label = f"{asset.name} · yerel taslak"
    if asset.type == "character" and kind == "still":
        clip = os.path.join(out_dir, "still.mp4")
        render_clip(clip, seed, 1024, 1024, 1.0, settings.local_render_fps, emotion="neutral",
                    camera={"shot": "medium", "movement": "static"}, characters=[asset.name], label=label)
        path = os.path.join(out_dir, "still.png")
        _ffmpeg("-ss", "0.5", "-i", clip, "-frames:v", "1", path)
        return "still", path, "still.png"
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
