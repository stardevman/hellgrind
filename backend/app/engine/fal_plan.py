"""fal.ai video motorları (MiniMax H3 Max taslak, Seedance 2.5 final): istek planı. Ağ yok, ücretli modül yüklenmez.

Kuru çalıştırma (`POST /scenes/{id}/render?dry_run=true`), maliyet tahmini ve ücretli sağlayıcı (app/live/fal_engines.py)
aynı fonksiyonları kullanır: panelde görülen gövde, gönderilecek gövdenin kendisidir (yalnızca dosya adresleri yerine
yerel yol ve SHA-256 yazar).

Mod (çekim başına; `scenes.h3_mode` alanı: fl2va = image-to-video, ref2va = reference-to-video):
- Yönetmen seçtiyse o. Seçmediyse: onaylı başlangıç karesi varsa (yönetmenin yüklediği kare ya da onaylanmış storyboard
  karesi) image-to-video, bitiş karesi varsa `end_image_url`; kare yoksa reference-to-video (karakter, nesne, mekân
  referans görselleri, bu sırayla).
- Referansların sırası istemdeki numaralandırmayla birebir aynıdır: Seedance "@Image1", H3 Max "Image 1". İstem her
  referansı tarifiyle ve numarasıyla yazar ("Nora, a red-haired girl …, is @Image1.").

Şema farkları (fal OpenAPI, 2026-10-12):
- H3 Max: duration SAYI (0,92-15; çıktı ~0,7 sn uzun olabilir), prompt_expansion_mode ZORUNLU, resolution 480P/768P/1080P.
  image-to-video: image_url, end_image_url (aspect_ratio yok, tuval kareyi izler). reference-to-video:
  reference_image_urls (en çok 9; görsel + video + ses en çok 12), image_url / end_image_url, aspect_ratio.
- Seedance 2.5: duration METİN ("auto" ya da "4".."30"), resolution 480p/720p/1080p, generate_audio. image-to-video:
  image_url (zorunlu), end_image_url, aspect_ratio her zaman "auto", tohum almaz. reference-to-video: image_urls,
  video_urls, audio_urls, task (reference/editing/extension), aspect_ratio, seed. draft: true -> 480p önizleme + draft_id.
"""
import hashlib
import math
import os
import re
from dataclasses import dataclass, field

from app.budget import pricing
from app.config import project_settings, settings

FAL_ENGINES = ("h3max", "seedance25")
H3_FAMILY = ("h3", "h3max", "seedance25")      # aynı kare zinciri ve koşul (görsel özet aynı aileyi görür)
ENGINE_TR = {"h3max": "MiniMax H3 Max (fal.ai)", "seedance25": "Seedance 2.5 (fal.ai)",
             "seedance": "Seedance 2.0 fast (fal.ai)", "h3": "MiniMax H3 (RunPod)", "fal_wan": "Wan 2.2 (fal.ai)",
             "higgsfield": "Higgsfield"}
MODE_TR = {"i2v": "image-to-video", "ref": "reference-to-video"}
STYLE_SENTENCE = "Render every environment in the same 3D animated style as the characters."
H3_SECONDS = (0.92, 15.0)
SEEDANCE_SECONDS = (4, 30)
H3_MAX_REFERENCES = 9                 # reference_image_urls (görsel + video + ses toplam en çok 12)
SEEDANCE_MAX_REFERENCES = 30          # şema: image_urls en çok 30 (görsel + video + ses toplam 50)
H3_RESOLUTIONS = ("480P", "768P", "1080P")
SEEDANCE_RESOLUTIONS = ("480p", "720p", "1080p")
TIMEOUT_S = {"h3max": 360, "seedance25": 600}     # kuyruk + üretim; aşılınca bir kez daha beklenir
TYPE_ORDER = {"character": 0, "prop": 1, "location": 2}


def tier_of(engine: str, cfg: dict | None = None) -> str:
    """fal motorunun klibi hangi turda kaydedilir: final motoru "full" (tam tur), taslak "probe" (test turu)."""
    return "full" if engine == final_engine(cfg) else "probe"


def is_fal(engine: str | None) -> bool:
    return engine in FAL_ENGINES


def draft_engine(cfg: dict | None = None) -> str:
    return (cfg or {}).get("draft_engine") or settings.video_draft_engine or "h3max"


def final_engine(cfg: dict | None = None) -> str:
    return (cfg or {}).get("final_engine") or settings.video_final_engine or "seedance25"


def draft(engine: str, scene=None, cfg: dict | None = None) -> bool:
    """Seedance 2.5 taslağı (draft: true, 480p + draft_id): çekimin ayarı, projenin ayarı, Ayarlar (varsayılan açık)."""
    if engine != "seedance25":
        return False
    value = (getattr(scene, "video_opts", None) or {}).get("draft")
    if value is None:
        value = (cfg or {}).get("seedance25_draft")
    return bool(settings.seedance25_draft) if value is None else bool(value)


def resolution(engine: str, scene=None, cfg: dict | None = None) -> str:
    """Çekimin çözünürlüğü: çekimin kendi ayarı (video_opts), projenin ayarı, Ayarlar'daki varsayılan (en ucuz: H3 Max
    480P); motorun yazımıyla. Seedance taslağı her zaman 480p (SEEDANCE25_RESOLUTION yalnızca taslak kapalıyken)."""
    raw = ((getattr(scene, "video_opts", None) or {}).get("resolution") or "").strip()
    cfg = cfg or {}
    if engine == "h3max":
        for value in (raw, cfg.get("h3max_resolution") or "", settings.h3max_resolution or ""):
            if value.upper() in H3_RESOLUTIONS:
                return value.upper()
        return "480P"
    if draft(engine, scene, cfg):
        return "480p"
    for value in (raw, cfg.get("seedance25_resolution") or "", settings.seedance25_resolution or ""):
        if value.lower() in SEEDANCE_RESOLUTIONS:
            return value.lower()
    return "720p"


def audio(engine: str, scene=None) -> bool:
    value = (getattr(scene, "video_opts", None) or {}).get("audio")
    return bool(settings.seedance25_audio) if value is None else bool(value)


def norm_resolution(engine: str, value: str | None) -> str:
    """Tur çözünürlüğünü (480p/720p/1080p) ya da motorun kendi yazımını motorun değerine çevirir: H3 Max 480P/768P/1080P,
    Seedance 480p/720p/1080p. Bilinmeyen: Ayarlar'daki varsayılan."""
    raw = (value or "").strip()
    if engine == "h3max":
        mapped = {"480P": "480P", "720P": "768P", "768P": "768P", "1080P": "1080P"}.get(raw.upper())
        return mapped or resolution(engine)
    mapped = {"480p": "480p", "720p": "720p", "768p": "720p", "1080p": "1080p"}.get(raw.lower())
    return mapped or resolution(engine)


def request_seconds(engine: str, duration_s: float) -> float | str:
    """Şemanın istediği tipte süre: H3 Max sayı (0,92-15), Seedance metin ("4".."30"; kısa çekim 4 sn üretilir)."""
    d = float(duration_s or 5.0)
    if engine == "h3max":
        return round(min(H3_SECONDS[1], max(H3_SECONDS[0], d)), 2)
    return str(int(min(SEEDANCE_SECONDS[1], max(SEEDANCE_SECONDS[0], math.ceil(d - 1e-6)))))


def billed_seconds(engine: str, duration_s: float) -> float:
    return float(request_seconds(engine, duration_s))


def ref_token(engine: str, i: int) -> str:
    """i. referans görseli (1'den): Seedance "@Image1" (şema), H3 Max "Image 1"."""
    return f"@Image{i}" if engine == "seedance25" else f"Image {i}"


IMAGE_REF = re.compile(r"\[\s*Image\s*(\d+)\s*\]|@Image\s*(\d+)|\bImage (\d+)\b")


def normalize_refs(engine: str, text: str) -> str:
    """İstemdeki referans yazımları ("[Image1]", "@Image1", "Image 1") motorun biçimine çevrilir: Seedance "@Image1"
    (şema), H3 Max "Image 1". Yönetmenin tarifte [ImageN] yazması da kabul edilir."""
    return IMAGE_REF.sub(lambda m: ref_token(engine, int(next(g for g in m.groups() if g))), text or "")


# ---------- referanslar ----------

@dataclass
class Ref:
    kind: str                 # character | prop | location
    name: str
    key: str                  # nesne deposu anahtarı (durağan görsel)
    description: str = ""
    number: int = 0

    def as_dict(self, engine: str) -> dict:
        return {"image": self.number, "token": ref_token(engine, self.number), "kind": self.kind, "name": self.name,
                "key": self.key, "description": self.description}


def _asset_still(asset) -> str | None:
    return ((getattr(asset, "references", None) or {}).get("still") or {}).get("key")


def references(scene, chars=(), assets=(), strict: bool = True, limit: int = H3_MAX_REFERENCES) -> list[Ref]:
    """Referans görselleri, sırayla: karakterler (çekim tarifinde ilk adı geçen önde), nesneler, mekânlar. Onaylı
    referansı (proje ayarına göre: lint_strictness block ise varlık en az still_approved) olmayanlar atlanır."""
    from app.core.h3mode import APPROVED, _approved, _mentioned_first, still_key
    by_id = {a.id: a for a in assets or ()}
    out: list[Ref] = []
    for ch in _mentioned_first(scene, list(chars or ())):
        key = still_key(ch)
        if key and _approved(ch, by_id, strict):
            asset = by_id.get(getattr(ch, "asset_id", None))
            out.append(Ref("character", ch.name, key, _describe(getattr(ch, "description", None)
                                                                or getattr(asset, "description", None))))
    for a in sorted(assets or (), key=lambda a: (TYPE_ORDER.get(a.type, 9), a.name)):
        if a.type not in ("prop", "location"):
            continue
        key = _asset_still(a)
        if key and (not strict or a.status in APPROVED):
            out.append(Ref(a.type, a.name, key, _describe(a.description)))
    out = out[:limit]
    for i, r in enumerate(out, start=1):
        r.number = i
    return out


def _describe(text: str | None) -> str:
    return " ".join((text or "").split()).rstrip(" .")


SHORT_CHARS = 140


def short_description(name: str, text: str) -> str:
    """Referans cümlesi için kısa tanım: tarifin ilk cümlesi, baştaki ad atılır, en çok ~140 karakter (virgülde kesilir).
    Tam tarif istemin varlık bölümünde zaten durur."""
    first = re.split(r"(?<=[.!?])\s", " ".join((text or "").split()), maxsplit=1)[0].rstrip(" .")
    low = first.lower()
    for lead in (f"{name.lower()}, ", f"{name.lower()} is ", f"{name.lower()}: ", f"{name.lower()} - "):
        if low.startswith(lead):
            first = first[len(lead):]
            break
    if len(first) > SHORT_CHARS:
        cut = first[:SHORT_CHARS].rsplit(",", 1)[0]
        first = cut if len(cut) > 40 else first[:SHORT_CHARS].rsplit(" ", 1)[0]
    return first.strip(" ,;")


def reference_sentence(engine: str, r: Ref, names: list[str]) -> str:
    """"Nora (a slim teenage girl with long copper-red hair) is Image 1." — numara istemdeki sırayla birebir aynı."""
    from app.hermes import translate
    desc = short_description(r.name, translate.to_english(r.description, names)) if r.description else ""
    label = {"character": r.name, "prop": f"The prop {r.name}", "location": f"The location {r.name}"}[r.kind]
    return f"{label} ({desc}) is {ref_token(engine, r.number)}." if desc else f"{label} is {ref_token(engine, r.number)}."


# ---------- mod ----------

def start_frame(scene, fp: str | None) -> tuple[str | None, str | None]:
    """Onaylı başlangıç karesi: (anahtar, kaynak). Yönetmenin yüklediği kare ya da bu görsel özetin onaylanmış (ve
    değişiklik istenmemiş) storyboard karesinin yazısız hâli."""
    from app import storage
    from app.engine.storyboard import clean_key
    if getattr(scene, "keyframe_key", None):
        return scene.keyframe_key, "upload"
    if scene.board_key and fp and scene.board_hash == fp and scene.board_status == "approved":
        key = clean_key(scene.board_key)
        if storage.exists(key):
            return key, "board"
    return None, None


def choose_mode(scene, frame_key: str | None, frame_source: str | None, refs: list[Ref]) -> tuple[str, str]:
    """(i2v | ref, Türkçe gerekçe)."""
    explicit = getattr(scene, "h3_mode", None)
    if explicit == "ref2va":
        return "ref", "Yönetmen reference-to-video seçti: karakter, nesne ve mekân referansları gider."
    if explicit == "fl2va":
        if frame_key:
            return "i2v", "Yönetmen image-to-video seçti: başlangıç karesi videonun ilk karesidir."
        return "i2v", ("Yönetmen image-to-video seçti ama onaylı başlangıç karesi yok: kare önce çizilip kalite "
                       "kapısından geçer.")
    if frame_key:
        what = "yönetmenin yüklediği kare" if frame_source == "upload" else "onaylı storyboard karesi"
        return "i2v", f"Onaylı başlangıç karesi var ({what}): image-to-video."
    if refs:
        return "ref", (f"Onaylı başlangıç karesi yok: reference-to-video ({len(refs)} referans görseli: "
                       f"{', '.join(r.name for r in refs)}).")
    return "ref", "Onaylı başlangıç karesi ve referans görseli yok: yalnızca metinden (reference-to-video, görselsiz)."


# ---------- istem ve gövde ----------

def style_line(style) -> str:
    """Proje stilinin tarifi (kılavuzun ışık dili ve hareket tarzı) + ortamların karakterlerle aynı 3D stilde olması."""
    parts = [getattr(style, "lighting_prefix", "") or "", getattr(style, "motion_style", "") or ""] if style else []
    head = " ".join(" ".join(p.split()).rstrip(" .") + "." for p in parts if p.strip())
    return f"{head} {STYLE_SENTENCE}".strip()


def build_prompt(engine: str, mode: str, composed, refs: list[Ref], style, names: list[str]) -> tuple[str, list[str]]:
    """Gönderilen istem: stil cümlesi, (ref modunda) numaralı referans tarifleri, çekim tarifi (bestecinin çıktısı, stil
    öneki hariç), negatiflerden pozitif kısıtlar (iki model de negatif istem almaz). (istem, tanınmayan negatifler)."""
    from app.style.constraints import constrained_prompt
    body = composed.prompt
    if composed.prefix and body.startswith(composed.prefix):
        body = body[len(composed.prefix):].strip()
    lines = [style_line(style)]
    if mode == "ref":
        lines += [reference_sentence(engine, r, names) for r in refs]
    lines.append(body)
    prompt, c = constrained_prompt(" ".join(x for x in lines if x), composed.negative)
    return normalize_refs(engine, prompt), list(c.unmapped)


def body(engine: str, mode: str, *, prompt: str, duration_s: float, resolution_: str, audio_: bool, seed: int | None,
         image_url: str | None = None, end_image_url: str | None = None, reference_urls=(),
         draft_: bool = False) -> tuple[str, dict]:
    """(uç nokta, istek gövdesi). Tipler şemaya uyar (testler OpenAPI tanımıyla doğrular)."""
    seconds = request_seconds(engine, duration_s)
    refs = list(reference_urls)
    if engine == "h3max":
        args = {"prompt": prompt, "prompt_expansion_mode": settings.h3max_prompt_expansion or "disabled",
                "duration": seconds, "resolution": resolution_}
        if seed is not None:
            args["seed"] = int(seed)
        if mode == "i2v":
            endpoint = settings.fal_h3max_i2v_endpoint
            if image_url:
                args["image_url"] = image_url
            if end_image_url:
                args["end_image_url"] = end_image_url
        else:
            endpoint = settings.fal_h3max_ref_endpoint
            args["aspect_ratio"] = "16:9"
            if refs:
                args["reference_image_urls"] = refs[:H3_MAX_REFERENCES]
            if image_url:                                   # ilk kare ref modunda da verilebilir (referanslar korunur)
                args["image_url"] = image_url
        return endpoint, args
    if engine != "seedance25":
        raise ValueError(f"fal planı yalnızca {', '.join(FAL_ENGINES)} içindir: {engine}")
    args = {"prompt": prompt, "duration": seconds, "resolution": resolution_, "generate_audio": bool(audio_)}
    if draft_:                                          # 480p önizleme + draft_id (7 gün içinde 1080p'ye tamamlanır)
        args.update(draft=True, resolution="480p")
    if mode == "i2v":
        endpoint = settings.fal_seedance25_i2v_endpoint
        args.update(image_url=image_url, aspect_ratio="auto")          # image-to-video'da her zaman "auto"
        if end_image_url:
            args["end_image_url"] = end_image_url
        return endpoint, args                                          # image-to-video tohum almaz
    endpoint = settings.fal_seedance25_ref_endpoint
    args.update(task="reference", aspect_ratio="16:9")
    if refs:
        args["image_urls"] = refs[:SEEDANCE_MAX_REFERENCES]
    if seed is not None:
        args["seed"] = int(seed)
    return endpoint, args


def images_in(args: dict) -> int:
    """İstekteki girdi görselleri (H3 Max referans token'ı: her biri 1.024)."""
    return len(args.get("reference_image_urls") or args.get("image_urls") or []) + \
        sum(1 for k in ("image_url", "end_image_url", "middle_image_url") if args.get(k))


def cost(engine: str, resolution_: str, duration_s: float, images: int = 0, mode: str = "ref",
         draft_: bool = False) -> dict:
    """Uç noktaya göre tahmin: H3 Max image-to-video ve reference-to-video ayrı fiyatlı; Seedance taslağı 480p."""
    return pricing.fal_video_cost(engine, norm_resolution(engine, resolution_), billed_seconds(engine, duration_s),
                                  images=images, mode=mode, draft=draft_)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- çekimin planı ----------

@dataclass
class ScenePlan:
    engine: str
    mode: str
    reason: str
    endpoint: str
    body: dict                          # dosya adresleri yerine "local:<anahtar>" yer tutucuları
    uploads: list[dict] = field(default_factory=list)     # [{"field", "key", "sha256"?, "path"?}]
    refs: list[Ref] = field(default_factory=list)
    frame_key: str | None = None
    end_key: str | None = None
    resolution: str = ""
    audio: bool = False
    cost: dict = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"engine": self.engine, "engine_label": ENGINE_TR.get(self.engine, self.engine), "mode": self.mode,
                "mode_label": MODE_TR[self.mode], "reason": self.reason, "endpoint": self.endpoint, "body": self.body,
                "uploads": self.uploads, "references": [r.as_dict(self.engine) for r in self.refs],
                "resolution": self.resolution, "audio": self.audio, "duration": self.body.get("duration"),
                "cost": self.cost, "unmapped_negatives": self.unmapped, "notes": self.notes}


def local_url(key: str) -> str:
    return f"local:{key}"


def scene_plan(s, scene, engine: str | None = None, seed: int | None = None, with_sha: bool = True) -> ScenePlan:
    """Çekimin fal isteği (kuru çalıştırma). engine: verilirse o motorla (ör. "yükselt" önizlemesi). Yükleme yapılmaz:
    dosyalar yerel anahtar (ve with_sha ise depodaki içeriğin SHA-256'sı) olarak yazılır."""
    from app.core.chain import plan_of
    from app.core.h3mode import strict_refs
    from app.core.hashing import seed_for, visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene, last_frame_of
    from app.models import Project
    from app.style import bible
    engine = engine or scene.engine
    if not is_fal(engine):
        raise ValueError(f"{scene.scene_code}: {ENGINE_TR.get(engine, engine)} bir fal planı değil")
    project = s.get(Project, scene.project_id)
    cfg = project_settings(project.settings)
    chars, assets = load_characters(s, scene.id), load_assets(s, scene.id)
    style = bible.active(s, scene.project_id)
    version = style.version if style else None
    fp = visual_fingerprint(scene, chars, assets, version)
    refs = references(scene, chars, assets, strict_refs(cfg),
                      H3_MAX_REFERENCES if engine == "h3max" else SEEDANCE_MAX_REFERENCES)
    frame_key, frame_source = start_frame(scene, fp)
    mode, reason = choose_mode(scene, frame_key, frame_source, refs)
    notes = []
    if mode == "i2v" and not frame_key:
        from app.engine.iteration import keyframe_of
        frame_key = keyframe_of(scene, fp)                     # açık i2v: kalite kapısından geçmiş güncel kare
        if not frame_key:
            notes.append("Başlangıç karesi henüz yok: üretimden önce fal.ai Qwen ile çizilecek (ayrı ücretli).")
    end_key = None
    if mode == "i2v" and frame_key:
        end_key, _ = last_frame_of(s, scene, version, cfg)
    walk_in = plan_of(s, scene)["entering"] if scene.entry_mode == "walk_in" else None
    composed = compose_for_scene(scene, chars, assets, style, entering=walk_in)
    names = [ch.name for ch in chars] + [a.name for a in assets if a.type == "character"]
    prompt, unmapped = build_prompt(engine, mode, composed, refs, style, names)
    res, snd, drf = resolution(engine, scene, cfg), audio(engine, scene), draft(engine, scene, cfg)
    if seed is None:                                           # üretimin ilk klibinin tohumu (iteration.run_one)
        seed = seed_for(str(scene.id), fp, tier_of(engine, cfg), 0, 0,
                        *([scene.prompt_revision] if scene.prompt_revision else []))
    endpoint, args = body(engine, mode, prompt=prompt, duration_s=scene.duration_s, resolution_=res, audio_=snd,
                          seed=seed, image_url=local_url(frame_key) if frame_key else None,
                          end_image_url=local_url(end_key) if end_key else None,
                          reference_urls=[local_url(r.key) for r in refs] if mode == "ref" else (), draft_=drf)
    uploads = []
    for name, key in (("image_url", frame_key), ("end_image_url", end_key)):
        if key and args.get(name):
            uploads.append(_upload_row(name, key, with_sha))
    for i, r in enumerate(refs if mode == "ref" else [], start=1):
        uploads.append(_upload_row(f"{'reference_image_urls' if engine == 'h3max' else 'image_urls'}[{i - 1}]",
                                   r.key, with_sha))
    if engine == "seedance25" and mode == "i2v" and not args.get("image_url"):
        notes.append("Seedance image-to-video başlangıç karesi ister (image_url zorunlu).")
    return ScenePlan(engine=engine, mode=mode, reason=reason, endpoint=endpoint, body=args, uploads=uploads, refs=refs,
                     frame_key=frame_key, end_key=end_key, resolution=res, audio=snd,
                     cost=cost(engine, res, scene.duration_s, images_in(args), mode, drf), unmapped=unmapped, notes=notes)


def _upload_row(field_name: str, key: str, with_sha: bool) -> dict:
    from app import storage
    row = {"field": field_name, "key": key, "path": key, "upload": "yüklenmedi (kuru çalıştırma)"}
    if with_sha and storage.exists(key):
        os.makedirs(settings.work_dir, exist_ok=True)
        import tempfile
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            row["sha256"] = sha256_of(storage.download(key, os.path.join(tmp, "f")))
    elif with_sha:
        row["missing"] = True
    return row
