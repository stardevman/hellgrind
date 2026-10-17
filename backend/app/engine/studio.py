"""Görsel stüdyosu: yönetmenin elle istediği görseller, varlık görseli için dışarıya çıkmadan.

İki kip: metinden (Qwen-Image-2512) ya da kaynaklardan (Qwen-Image-Edit-2511; varlık referansları ya da önceki stüdyo
görselleri, istemde "image 1..3"). İstem stil bestecisinden geçer: stil kılavuzu açıksa ışık dili, palet ve türün
render kuralı eklenir; bir varlık için üretiliyorsa varlığın tarifi, değişmezleri ve kadro kısıtı da girer. Üretilen
görsel Varlıklar'daki referans yüklemesiyle aynı yoldan bir varlığa atanır (onay akışı değişmez).

Ücretsiz modda yerel prosedürel taslak çizilir ve ücretli karşılığının fiyatı sanal olarak bütçeden düşer. Ücretli modda
fal.ai çağrısı `app/live/fal_qwen.studio_image` içindedir (guard + bütçe rezervasyonu). Hat kendiliğinden görsel üretmez
(referans modu); stüdyo yalnızca panelden elle çalışır.

Worker durumsuzdur: girdi satırın kendisidir, çıktı içerik-adresli `studio/{proje}/{girdi özeti}.png`; dosya zaten varsa
üretim atlanır ve bedel yazılmaz.
"""
import datetime as dt
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

from PIL import Image

from app import storage
from app.budget import killswitch, pricing
from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import StudioImage

log = logging.getLogger("hellgrind.studio")

ASPECTS = {          # kip -> (ad, genişlik, yükseklik): Qwen-Image'ın doğal boyları
    "square": ("Kare 1:1", 1328, 1328),
    "landscape": ("Yatay 16:9", 1664, 928),
    "portrait": ("Dikey 9:16", 928, 1664),
    "classic": ("Yatay 4:3", 1472, 1104),
    "classic_portrait": ("Dikey 3:4", 1104, 1472),
}
KINDS = ("free", "character", "location", "prop")       # varlık seçilmemişse türün render kuralı ve çerçevesi
MAX_COUNT = 4
MAX_SOURCES = 3
LOCAL_SIDE = 1024                                       # yerel taslağın uzun kenarı
THUMB_SIDE = 480
STALE = dt.timedelta(minutes=15)                        # bu kadar süredir sırada duran iş çöken bir worker'dan kalmıştır
LOCAL_CAMERA = {"character": {"shot": "medium", "movement": "static"}, "location": {"shot": "wide", "movement": "static"},
                "prop": {"shot": "close_up", "movement": "static"}, "free": {"shot": "medium", "movement": "static"}}


def model_of(mode: str) -> str:
    return "qwen_edit" if mode == "edit" else "qwen_image"


def estimate(mode: str, aspect: str) -> float:
    """Tek görselin bedeli (fal.ai fiyatı, megapiksel başına; ücretsiz modda sanal olarak düşer)."""
    _, w, h = ASPECTS[aspect]
    return pricing.estimate_fal_image(model_of(mode), w, h)


def ledger_label(mode: str) -> str:
    return "fal_qwen:studio:qwen_edit" if mode == "edit" else "fal_qwen:studio"


def engine_now() -> str:
    return "local" if settings.dry_run else "fal_qwen"


# ---------- istem ----------

@dataclass(frozen=True)
class Prompt:
    prompt: str
    negative: str


def compose(prompt: str, kind: str, asset, bible) -> Prompt:
    """Sağlayıcıya giden istem. asset: istemin kurulduğu varlık (yoksa None); bible: etkin stil kılavuzu ya da None
    (yönetmen stili kapattıysa). Varlık için boş istem, Varlıklar'daki referans görselinin istemidir."""
    from app.hermes import translate
    from app.style import composer
    text = " ".join((prompt or "").split())
    if asset is not None:
        from app.engine.references import REFERENCE_STILL
        base, camera = REFERENCE_STILL[asset.type]
        shot = {"prompt": text or base, "camera": None if text else camera,
                "characters": [asset.name] if asset.type == "character" else []}
        c = composer.compose_prompt(shot, [asset], bible)
        return Prompt(c.prompt, c.negative)
    parts = []
    if bible is not None:            # ışık dili ve palet; hareket tarzı durağan görselde yer almaz
        parts.append(composer.sentence(translate.to_english(bible.lighting_prefix or "")))
        if bible.palette:
            parts.append(composer.sentence("Palette: " + ", ".join(bible.palette)))
    parts.append(composer.sentence(translate.to_english(text)))
    if bible is not None and kind in ("character", "location", "prop"):
        from app.style.bible import default_rules
        rule = (bible.rules or {}).get(kind) or default_rules(bible).get(kind)
        if rule:
            parts.append(composer.sentence("Rendered in " + composer.style_text(rule)))
    negative = translate.to_english(bible.negative or "") if bible is not None else ""
    return Prompt(" ".join(p for p in parts if p), negative)


def input_hash(row_like: dict) -> str:
    """Aynı girdi aynı anahtarı üretir (değişmez kural 4): istem, boy, seed, kaynaklar ve motor."""
    return stable_hash({k: row_like[k] for k in ("composed_prompt", "negative", "mode", "width", "height", "seed",
                                                  "source_keys", "engine")})


def file_key(project_id, digest: str) -> str:
    return f"studio/{project_id}/{digest}.png"


def thumb_key(digest: str) -> str:
    return f"thumbs/studio/{digest}.jpg"


# ---------- çizim ----------

def local_size(width: int, height: int) -> tuple[int, int]:
    scale = LOCAL_SIDE / max(width, height)
    return int(width * scale) // 2 * 2, int(height * scale) // 2 * 2


def render_local(out_dir: str, seed: int, width: int, height: int, prompt: str, composed: str, kind: str,
                 character: str | None) -> str:
    """Ücretsiz modda yerel taslak: prosedürel dünyadan tek kare. Duygu istemden, ışık görünümü bestecinin isteminden
    (ör. "golden-hour"), karakter siluetinin adı varlıktan gelir."""
    from app.engine.procedural import render_clip
    from app.hermes.rules import emotion_of
    w, h = local_size(width, height)
    clip = os.path.join(out_dir, "local.mp4")
    label = "Yerel taslak · " + (" ".join(prompt.split())[:60] or (character or "görsel"))
    render_clip(clip, seed, w, h, 1.0, 4, emotion=emotion_of(prompt), camera=LOCAL_CAMERA.get(kind, LOCAL_CAMERA["free"]),
                characters=[character] if character else [], label=label, lighting=composed)
    path = os.path.join(out_dir, "local.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", clip, "-frames:v", "1", path],
                   check=True, capture_output=True)
    return path


def put_thumb(path: str, key: str) -> str:
    if not storage.exists(key):
        with tempfile.NamedTemporaryFile(suffix=".jpg", dir=settings.work_dir) as f:
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_SIDE, THUMB_SIDE))
                im.save(f.name, "JPEG", quality=85)
            storage.put_file(f.name, key, "image/jpeg")
    return key


def _finish(image_id: str, **fields) -> None:
    with session_scope() as s:
        row = s.get(StudioImage, image_id)
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
            row.finished_at = dt.datetime.now(dt.timezone.utc)


def run(image_id: str) -> str:
    """Tek görseli üretir (gen kuyruğu). Proje donmuşsa başlamaz; hata satıra Türkçe yazılır."""
    with session_scope() as s:
        row = s.get(StudioImage, image_id)
        if row is None or row.status == "done":
            return "skip"
        row.status = "running"
        job = {"project_id": str(row.project_id), "prompt": row.prompt, "composed": row.composed_prompt,
               "negative": row.negative, "mode": row.mode, "aspect": row.aspect, "width": row.width,
               "height": row.height, "seed": row.seed, "sources": [x["key"] for x in row.sources or []],
               "digest": row.input_hash, "kind": row.kind, "character": None}
        if row.target_asset_id is not None:
            from app.models import Asset
            a = s.get(Asset, row.target_asset_id)
            if a is not None:
                job["kind"], job["character"] = a.type, (a.name if a.type == "character" else None)
    pid = job["project_id"]
    if killswitch.is_frozen(pid):
        _finish(image_id, status="failed", error="Proje donduruldu ya da bütçe limiti doldu; görsel üretilmedi.")
        return "frozen"
    key = file_key(pid, job["digest"])
    try:
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            if storage.exists(key):                 # aynı girdi daha önce üretildi: iş atlanır, bedel yazılmaz
                path = storage.download(key, os.path.join(tmp, "image.png"))
                put_thumb(path, thumb_key(job["digest"]))
                _finish(image_id, status="done", file_key=key, thumb_key=thumb_key(job["digest"]), cost_usd=0,
                        engine=engine_now(), error=None)
                return "cached"
            cost = estimate(job["mode"], job["aspect"])
            if settings.dry_run:
                with killswitch.spend(pid, cost, ledger_label(job["mode"]), details={"studio_image": image_id}) as charge:
                    path = render_local(tmp, job["seed"], job["width"], job["height"], job["prompt"], job["composed"],
                                        job["kind"], job["character"])
                    charge.actual = cost
                engine = "local"
            else:
                from app.engine import paid
                from app.live import fal_qwen            # ücretli modül: yalnızca fonksiyon içinde
                made = paid.call(pid, cost, ledger_label(job["mode"]), "fal_key",
                                 lambda: fal_qwen.studio_image(job["composed"], job["seed"], job["width"], job["height"],
                                                               job["negative"], job["sources"], pid),
                                 details={"studio_image": image_id})
                path, cost, engine = made.local_path, float(made.cost_usd), "fal_qwen"
            storage.put_file(path, key, "image/png")
            put_thumb(path, thumb_key(job["digest"]))
        _finish(image_id, status="done", file_key=key, thumb_key=thumb_key(job["digest"]), cost_usd=cost, engine=engine,
                error=None)
        return "done"
    except killswitch.ProjectFrozen:
        _finish(image_id, status="failed", error="Bütçe limiti doldu ya da proje donduruldu; görsel üretilmedi.")
        return "frozen"
    except Exception as e:                              # noqa: BLE001 — hata satıra; panel gösterir
        log.exception("görsel stüdyosu hatası image=%s", image_id)
        from app.live import redact
        _finish(image_id, status="failed", error=redact(f"{type(e).__name__}: {e}")[:600])
        return "failed"

