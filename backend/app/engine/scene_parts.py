"""Referans + senaryo ile video (VIDEO_INPUT_MODE=reference): sahne parçaları ve tek isteklik plan. Ağ yok, ücretli
modül yüklenmez; kuru çalıştırma, plan onayı, tahmin ve üretim aynı fonksiyonları kullanır.

Hiçbir kare ya da görsel üretilmez: video yalnızca varlıkların onaylı referans görselleri (karakter → nesne → mekân,
sabit sırayla) ve senaryodan üretilir. Süreklilik için yalnızca önceki klibin son karesi ffmpeg ile ÇIKARILIR.

- **Sahne:** aynı sahne başlığındaki çekimler (kod öneki: S001_SH01, S001_SH02 → S001), aynı sekansta, sırayla.
- **Parça (SCENE_RENDER_MODE):** whole_scene'de sahnenin ardışık, aynı motorlu çekimleri tek istekte üretilir; toplam
  süre motor sınırını (H3 Max 15 sn, Seedance 2.5 30 sn) aşarsa çekim sınırlarından parçalara bölünür. per_shot'ta her
  çekim tek başına bir parçadır. Üretilen parça klibi çekim sınırlarından kesilir: her çekim kendi klibini alır (ses,
  renk, montaj ve kurgu değişmeden çalışır).
- **İstem:** stil cümlesi, numaralı referanslar (Seedance "@Image1", H3 Max "Image 1"), gerçek boyutlar, çekim listesi
  ("Shot 1 (0-3s), medium shot, …: eylem. Nora says: "…". Mood: fear."), kadro kısıtı, negatiflerden pozitif kısıtlar.
- **Süreklilik:** parçanın ilk çekimi "continuous" ise önceki klibin son karesi verilir: H3 Max'te `image_url` (ilk kare),
  referanslar `reference_image_urls`'te kalır; Seedance 2.5 reference'ta ilk kare alanı yoktur: kare `image_urls`'ün
  BAŞINA konur, istem "the shot starts exactly from @Image1" der ve referans numaraları bir kayar. "cut"/"new_scene"
  geçişlerinde son kare verilmez.
- **Sınırlar:** H3 Max reference_image_urls en çok 9 (şema; görsel + video + ses toplam 12), Seedance image_urls en çok 30
  (toplam 50). Aşarsa en az önemli (çekim metinlerinde en az geçen) mekân ya da nesne düşer ve planda uyarı çıkar.
  Görseli olmayan varlık "eksik referans" olur ve render engellenir.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select

from app.config import project_settings, settings
from app.core.hashing import seed_for, stable_hash
from app.engine import fal_plan

LIMIT_S = {"h3max": 15.0, "seedance25": 30.0}
MAX_IMAGES = {"h3max": 9, "seedance25": 30}
MAX_FILES = {"h3max": 12, "seedance25": 50}
CONTINUOUS_SEEDANCE = "The shot starts exactly from @Image1 (the last frame of the previous shot); continue from it."
CONTINUOUS_H3 = "The video opens exactly on the provided first frame (the last frame of the previous shot); continue from it."


def reference_mode() -> bool:
    return settings.video_input_mode != "keyframe"


def active(engine: str | None) -> bool:
    """Bu çekim referans yoluyla mı üretilir: referans modu ve fal.ai motoru (H3 Max, Seedance 2.5)."""
    return reference_mode() and fal_plan.is_fal(engine)


def whole_scene() -> bool:
    return settings.scene_render_mode != "per_shot"


def plan_required(cfg: dict) -> bool:
    """Render'dan önce plan onayı: proje ayarı require_plan_approval; boşsa ücretli modda evet, ücretsiz modda hayır."""
    value = cfg.get("require_plan_approval")
    return (not settings.dry_run) if value is None else bool(value)


# ---------- sahne ve parçalar ----------

def heading_key(sc) -> str:
    return (sc.scene_code or "").split("_SH")[0]


def scene_shots(s, sc) -> list:
    """Çekimin sahnesi (aynı başlık önekli, aynı sekanstaki çekimler), sırayla."""
    from app.models import Scene
    q = select(Scene).where(Scene.project_id == sc.project_id)
    q = q.where(Scene.sequence_id == sc.sequence_id) if sc.sequence_id is not None else q.where(Scene.sequence_id.is_(None))
    key = heading_key(sc)
    return [x for x in s.scalars(q.order_by(Scene.seq_order, Scene.scene_code)) if heading_key(x) == key]


def chunk(shots: list, engine: str) -> list[list]:
    """Ardışık çekimleri motor sınırına göre (çekim sınırlarından) parçalara böler; per_shot'ta her çekim bir parça."""
    if not whole_scene():
        return [[x] for x in shots]
    limit = LIMIT_S.get(engine, 15.0)
    parts, current, total = [], [], 0.0
    for x in shots:
        d = float(x.duration_s or 5.0)
        if current and total + d > limit + 1e-6:
            parts.append(current)
            current, total = [], 0.0
        current.append(x)
        total += d
    if current:
        parts.append(current)
    return parts


def parts_of(s, sc) -> list[list]:
    """Sahnenin parçaları: aynı motorlu ve üretilen (generate) ardışık çekim dizileri, sınıra göre bölünmüş."""
    runs, current = [], []
    for x in scene_shots(s, sc):
        ok = (x.method or "generate") == "generate" and active(x.engine)
        if current and (not ok or x.engine != current[-1].engine):
            runs.append(current)
            current = []
        if ok:
            current.append(x)
    if current:
        runs.append(current)
    return [p for run in runs for p in chunk(run, run[0].engine)]


def part_of(s, sc) -> list:
    for p in parts_of(s, sc):
        if any(x.id == sc.id for x in p):
            return p
    return [sc]


def timeline(part: list) -> list[tuple]:
    """[(çekim, başlangıç sn, bitiş sn)] parçanın klibinde."""
    out, at = [], 0.0
    for x in part:
        d = float(x.duration_s or 5.0)
        out.append((x, round(at, 3), round(at + d, 3)))
        at += d
    return out


def seconds_label(v: float) -> str:
    return f"{v:g}"


# ---------- referanslar ----------

def union(s, part: list) -> tuple[list, list]:
    """Parçadaki karakterler ve varlıklar (tekrarsız)."""
    from app.core.state import load_assets, load_characters
    chars, assets, seen_c, seen_a = [], [], set(), set()
    for x in part:
        for ch in load_characters(s, x.id):
            if ch.id not in seen_c:
                seen_c.add(ch.id)
                chars.append(ch)
        for a in load_assets(s, x.id):
            if a.id not in seen_a:
                seen_a.add(a.id)
                assets.append(a)
    return chars, assets


def part_text(part: list) -> str:
    return " ".join(str((x.prompt_spec or {}).get("prompt") or x.description or "") for x in part)


def missing_references(chars, assets) -> list[dict]:
    """Görseli olmayan karakter, nesne ve mekânlar: render engellenir, yönetmen görsel yükler."""
    from app.core.h3mode import still_key
    by_id = {a.id: a for a in assets}
    out = []
    for ch in chars:
        asset = by_id.get(getattr(ch, "asset_id", None))
        if not still_key(ch) and not ((getattr(asset, "references", None) or {}).get("still") or {}).get("key"):
            out.append({"kind": "character", "name": ch.name, "asset_id": str(ch.asset_id) if ch.asset_id else None})
    for a in assets:
        if a.type in ("prop", "location") and not ((a.references or {}).get("still") or {}).get("key"):
            out.append({"kind": a.type, "name": a.name, "asset_id": str(a.id)})
    return out


def importance(name: str, part: list) -> int:
    """Kaç çekimin metninde adı geçiyor (düşürme sırası: en az geçen mekân ya da nesne önce düşer)."""
    low = name.lower()
    return sum(low in str((x.prompt_spec or {}).get("prompt") or "").lower() for x in part)


def fit(refs: list, part: list, limit: int) -> tuple[list, list]:
    """Sınırı aşan referans listesinden en az önemli mekân/nesneleri düşürür. (kalan, düşenler)."""
    refs, dropped = list(refs), []
    while len(refs) > limit:
        candidates = [r for r in refs if r.kind in ("prop", "location")]
        if not candidates:
            dropped += refs[limit:]
            refs = refs[:limit]
            break
        victim = min(reversed(candidates), key=lambda r: importance(r.name, part))
        refs.remove(victim)
        dropped.append(victim)
    for i, r in enumerate(refs, start=1):
        r.number = i
    return refs, dropped


def scale_lines(chars, assets) -> list[str]:
    """Gerçek boyut: "Pip is tiny, palm-sized; much smaller than a hen." (varlığın scale alanı)."""
    by_id = {a.id: a for a in assets}
    out = []
    for ch in chars:
        scale = " ".join(str(getattr(by_id.get(ch.asset_id), "scale", None) or "").split()).rstrip(" .")
        if scale:
            out.append(f"{ch.name} is {scale}.")
    for a in assets:
        scale = " ".join(str(getattr(a, "scale", None) or "").split()).rstrip(" .")
        if a.type == "prop" and scale:
            out.append(f"The {a.name} {'are' if a.name.endswith('s') else 'is'} {scale}.")
    return out


# ---------- istem ----------

def shot_line(i: int, x, start: float, end: float, dialogue: list, names: list[str]) -> str:
    from app.core.chain import camera_of
    from app.hermes import translate
    from app.style.composer import camera_text, sentence
    spec = x.prompt_spec or {}
    action = translate.to_english(str(spec.get("prompt") or x.description or ""), names, spec.get("lang"))
    # yalnızca kamera cümlesi ("Camera: wide shot, slow dolly in, …"); hareket cümlesi çekimin kendi tarifinde
    camera = camera_text(camera_of(x)).removeprefix("Camera: ").split(". ")[0].rstrip(" .")
    parts = [f"Shot {i} ({seconds_label(start)}-{seconds_label(end)}s)" + (f", {camera}" if camera else "") + ":",
             sentence(action)]
    for who, line in dialogue:
        if line.strip():
            parts.append(f'{who or "A voice"} says: "{" ".join(line.split())}".')
    if x.emotion and x.emotion != "neutral":
        parts.append(f"Mood: {x.emotion}.")
    if getattr(x, "director_note", None):
        parts.append(sentence("Director note: " + translate.to_english(x.director_note, names, spec.get("lang"))))
    return " ".join(p for p in parts if p)


def build_prompt(s, engine: str, part: list, refs: list, chars, assets, style, frame: bool) -> tuple[str, list[str]]:
    from app.engine.storyboard import dialogue_of
    from app.hermes import lint, translate
    from app.style.constraints import constrained_prompt
    names = [ch.name for ch in chars]
    lines = [fal_plan.style_line(style)]
    if frame:
        lines.append(CONTINUOUS_SEEDANCE if engine == "seedance25" else CONTINUOUS_H3)
    lines += [fal_plan.reference_sentence(engine, r, names) for r in refs]
    lines += scale_lines(chars, assets)
    lines += [shot_line(i, x, a, b, dialogue_of(s, x.id), names) for i, (x, a, b) in enumerate(timeline(part), start=1)]
    lines.append(lint.cast_constraint(names))
    negatives = ", ".join(n for n in (translate.to_english(style.negative or "") if style else "",
                                      *[str((x.prompt_spec or {}).get("negative") or "") for x in part],
                                      lint.CAST_NEGATIVE) if n)
    prompt, c = constrained_prompt(" ".join(x for x in lines if x), negatives)
    return fal_plan.normalize_refs(engine, prompt), list(c.unmapped)


# ---------- plan ----------

@dataclass
class PartPlan:
    engine: str
    shots: list                       # Scene satırları
    timeline: list                    # [(kod, başlangıç, bitiş)]
    refs: list
    dropped: list
    missing: list
    continuity: dict                  # {"transition", "previous", "frame_key", "expected", "note"}
    prompt: str
    unmapped: list
    endpoint: str
    body: dict
    resolution: str
    audio: bool
    duration_s: float
    cost: dict
    plan_hash: str
    part_hash: str
    seed: int
    warnings: list = field(default_factory=list)
    uploads: list = field(default_factory=list)

    @property
    def leader(self):
        return self.shots[0]

    def approved(self) -> bool:
        return all((x.plan_approval or {}).get("hash") == self.plan_hash for x in self.shots)

    def as_dict(self) -> dict:
        refs = [r.as_dict(self.engine) for r in self.refs]   # numaralar zaten kaydırılmış (Seedance + son kare)
        if self.engine == "seedance25" and self.continuity.get("expected"):
            refs.insert(0, {"image": 1, "token": fal_plan.ref_token(self.engine, 1), "kind": "frame",
                            "name": f"{self.continuity.get('previous')} son karesi", "key": self.continuity.get("frame_key"),
                            "description": "önceki klibin son karesi (ffmpeg ile çıkarılır, üretilmez)"})
        return {"input_mode": "reference", "render_mode": settings.scene_render_mode, "engine": self.engine,
                "engine_label": fal_plan.ENGINE_TR[self.engine], "mode": "ref", "mode_label": fal_plan.MODE_TR["ref"],
                "reason": "Referans modu: video yalnızca onaylı referans görselleri ve senaryodan üretilir (kare yok).",
                "part": [x.scene_code for x in self.shots], "part_ids": [str(x.id) for x in self.shots],
                "timeline": [{"code": c, "start": a, "end": b} for c, a, b in self.timeline],
                "references": refs, "dropped": [{"kind": r.kind, "name": r.name} for r in self.dropped],
                "missing": self.missing, "continuity": self.continuity, "endpoint": self.endpoint, "body": self.body,
                "uploads": self.uploads, "resolution": self.resolution, "audio": self.audio,
                "duration": self.body.get("duration"), "duration_s": self.duration_s, "cost": self.cost,
                "unmapped_negatives": self.unmapped, "warnings": self.warnings, "plan_hash": self.plan_hash,
                "part_hash": self.part_hash, "approved": self.approved(), "seed": self.seed,
                "blocked": bool(self.missing),
                "notes": [w for w in self.warnings]}


def previous_frame(s, first, engine: str) -> dict:
    """Parçanın ilk çekiminin geçişine göre önceki klibin son karesi: {"transition", "previous", "expected", "frame_key",
    "winner_id", "note"}. Kare yalnızca "continuous" geçişte; çıkarma üretimde yapılır (seam/{varyasyon}.jpg)."""
    from app.core.chain import neighbours, transition_of
    from app.models import Variation
    transition = transition_of(first)
    out = {"transition": transition, "previous": None, "expected": False, "frame_key": None, "winner_id": None,
           "note": None}
    if transition != "continuous":
        out["note"] = "Kesme: önceki klibin son karesi verilmez, yalnızca referanslar."
        return out
    previous, _ = neighbours(s, first)
    if previous is None:
        out["note"] = "Sekansın ilk çekimi: önceki klip yok."
        return out
    out["previous"] = previous.scene_code
    winner = s.scalars(select(Variation).where(Variation.scene_id == previous.id, Variation.refined_rank == 1,
                                               Variation.video_key.is_not(None))).first()
    if winner is not None:
        out.update(expected=True, frame_key=f"seam/{winner.id}.jpg", winner_id=str(winner.id),
                   note=f"{previous.scene_code} kazanan klibinin son karesi (ffmpeg ile çıkarılır).")
    elif active(previous.engine) and (previous.method or "generate") == "generate":
        out.update(expected=True, note=f"{previous.scene_code} henüz üretilmedi: önce o parça üretilir, son karesi "
                                       "çıkarılıp bu parçanın ilk karesi olur.")
    else:
        out["note"] = f"{previous.scene_code} klibi yok ve referans yoluyla üretilmiyor: son kare verilmeyecek."
    return out


def plan_part(s, sc, engine: str | None = None, frame_key: str | None = None, with_sha: bool = False,
              continuity: dict | None = None) -> PartPlan:
    """Çekimin parçasının planı. engine: verilirse o motorla (yükseltme önizlemesi). frame_key: üretimde çıkarılmış son
    kare (verilmezse plan önceki kazananın anahtarını tahmin eder). Yükleme yapılmaz."""
    from app.core.h3mode import strict_refs
    from app.models import Project
    from app.style import bible
    part = part_of(s, sc)
    engine = engine or part[0].engine
    if not fal_plan.is_fal(engine):
        raise ValueError(f"{sc.scene_code}: {engine} referans yolunda kullanılamaz")
    cfg = project_settings(s.get(Project, sc.project_id).settings)
    style = bible.active(s, sc.project_id)
    chars, assets = union(s, part)
    limit = MAX_IMAGES[engine]
    cont = dict(continuity or previous_frame(s, part[0], engine))
    if frame_key:
        cont.update(frame_key=frame_key, expected=True)
    seed_frame = engine == "seedance25" and cont.get("expected")
    focus = SimpleNamespace(prompt_spec={"prompt": part_text(part)}, description="")
    refs = fal_plan.references(focus, chars, assets, strict_refs(cfg), limit=10 ** 6)
    refs, dropped = fit(refs, part, limit - (1 if seed_frame else 0))
    if seed_frame:                                    # Seedance: son kare @Image1, referanslar bir kayar
        for r in refs:
            r.number += 1
    warnings = []
    if dropped:
        warnings.append(f"Referans sınırı ({limit} görsel): düşenler {', '.join(r.name for r in dropped)} "
                        "(çekim metinlerinde en az geçen mekân/nesne).")
    missing = missing_references(chars, assets)
    if missing:
        warnings.append("Eksik referans: " + ", ".join(m["name"] for m in missing) +
                        " (görsel yükleyin; render engelli).")
    if cont.get("transition") == "continuous" and cont.get("note") and not cont.get("frame_key"):
        warnings.append(cont["note"])                 # önceki klip henüz yok ya da kare verilmeyecek
    prompt, unmapped = build_prompt(s, engine, part, refs, chars, assets, style, bool(cont.get("expected")))
    total = round(sum(float(x.duration_s or 5.0) for x in part), 3)
    resolution, audio = fal_plan.resolution(engine, part[0]), fal_plan.audio(engine, part[0])
    frame_local = fal_plan.local_url(cont["frame_key"]) if cont.get("frame_key") else \
        ("local:<önceki klibin son karesi>" if cont.get("expected") else None)
    ref_urls = [fal_plan.local_url(r.key) for r in refs]
    if seed_frame:
        ref_urls = [frame_local, *ref_urls]
    plan_hash = stable_hash([engine, resolution, audio, prompt, total, [r.key for r in refs], bool(cont.get("expected")),
                             [x.scene_code for x in part]])
    part_hash = stable_hash([plan_hash, cont.get("frame_key"), [x.prompt_revision for x in part]])
    seed = seed_for(part_hash, "part", 0)
    endpoint, body = fal_plan.body(engine, "ref", prompt=prompt, duration_s=total, resolution_=resolution, audio_=audio,
                                   seed=seed, image_url=frame_local if engine == "h3max" else None,
                                   reference_urls=ref_urls)
    files = len(ref_urls) + (1 if engine == "h3max" and frame_local else 0)
    if files > MAX_FILES[engine]:
        warnings.append(f"Dosya sınırı aşıldı ({files} > {MAX_FILES[engine]}).")
    images = len(ref_urls) + (1 if engine == "h3max" and frame_local else 0)
    plan = PartPlan(engine=engine, shots=part, timeline=[(x.scene_code, a, b) for x, a, b in timeline(part)], refs=refs,
                    dropped=dropped, missing=missing, continuity=cont, prompt=prompt, unmapped=unmapped,
                    endpoint=endpoint, body=body, resolution=resolution, audio=audio, duration_s=total,
                    cost=fal_plan.cost(engine, resolution, total, images), plan_hash=plan_hash, part_hash=part_hash,
                    seed=seed, warnings=warnings)
    plan.uploads = uploads(plan, with_sha)
    return plan


def uploads(plan: PartPlan, with_sha: bool) -> list[dict]:
    from app.engine.fal_plan import _upload_row
    rows = []
    if plan.continuity.get("expected"):
        key = plan.continuity.get("frame_key")
        field_ = "image_url" if plan.engine == "h3max" else "image_urls[0]"
        rows.append({"field": field_, "key": key, "path": key, "upload": "yüklenmedi (kuru çalıştırma)",
                     "note": "önceki klibin son karesi: üretimde ffmpeg ile çıkarılır"} if key else
                    {"field": field_, "key": None, "upload": "önceki klip üretilince çıkarılacak"})
    shift = 1 if plan.engine == "seedance25" and plan.continuity.get("expected") else 0
    name = "reference_image_urls" if plan.engine == "h3max" else "image_urls"
    for i, r in enumerate(plan.refs):
        rows.append(_upload_row(f"{name}[{i + shift}]", r.key, with_sha))
    return rows


def approve(s, sc, plan: PartPlan) -> None:
    """Planı parçanın bütün çekimleri için onaylar (planın özeti saklanır)."""
    import time
    for x in plan.shots:
        x.plan_approval = {"hash": plan.plan_hash, "usd": plan.cost["reserve_usd"], "at": time.time(),
                           "part": [y.scene_code for y in plan.shots], "engine": plan.engine}

