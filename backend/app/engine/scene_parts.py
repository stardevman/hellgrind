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
import re
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


def lint_cfg(cfg: dict, engine: str | None) -> dict:
    """Referans modunda varlığın onaylı durağan görseli yeterlidir (hareket testi ve kilit gerekmez): asset_min_status
    en çok still_approved. Kuru çalıştırma ve dağıtım aynı kuralı kullanır."""
    if active(engine) and cfg.get("asset_min_status", "locked") != "still_approved":
        return {**cfg, "asset_min_status": "still_approved"}
    return cfg


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

PROMPT_LIMIT = {"h3max": 50000, "seedance25": None}     # şemadaki prompt maxLength (Seedance'ta sınır yok)
CAMERA_WORDS = ("extreme wide", "wide shot", "medium shot", "close-up", "close up", "closeup", "full shot", "long shot",
                "over-the-shoulder", "over the shoulder", "point-of-view", "ground-level", "ground level", "two-shot")
SHOT_PLAIN = {"extreme_wide": "extreme wide shot", "wide": "wide shot", "pov": "point-of-view shot",
              "medium": "medium shot", "over_shoulder": "over-the-shoulder shot", "close_up": "close-up",
              "extreme_close_up": "extreme close-up"}
MOVE_PLAIN = {"static": "static camera", "dolly_in": "slow push in", "dolly_out": "slow pull back", "pan": "slow pan",
              "tilt": "slow tilt", "crane": "crane move", "orbit": "slow orbit", "tracking": "tracking camera",
              "handheld": "handheld camera"}
UNPOPULATED = "only the listed characters are present and the surroundings are otherwise unpopulated"


def plain_camera(camera: dict | None) -> str:
    """Kamera, modellerin anladığı sade dille: derece yerine "from the left side", "low at ground level"."""
    camera = camera or {}
    shot = camera.get("shot")
    parts = [SHOT_PLAIN.get(shot), MOVE_PLAIN.get(camera.get("movement"))]
    angle = camera.get("angle")
    if angle == "low":
        parts.append("low at ground level" if shot in ("close_up", "extreme_close_up") else "low angle")
    elif angle == "high":
        parts.append("high angle")
    side = int(camera.get("side") or 0)
    if abs(side) >= 135:
        parts.append("from behind")
    elif abs(side) >= 60:
        parts.append(f"from the {'left' if side < 0 else 'right'} side")
    elif abs(side) >= 10:
        parts.append(f"from slightly {'left' if side < 0 else 'right'}")
    if camera.get("lens_mm"):
        parts.append(f"{camera['lens_mm']} mm lens")
    return ", ".join(p for p in parts if p)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-zçğıöşü]+", (text or "").lower()))


def head_noun(description: str | None) -> str | None:
    """Tarifin baş adı ("a tiny round fluffy baby chick with …" -> chick; "a small flock of … hens with …" -> hens)."""
    first = re.split(r",|;|:| with | who | that | in | on ", " ".join((description or "").split()).lower(), maxsplit=1)[0]
    words = re.findall(r"[a-zçğıöşü]+", first)
    return words[-1] if words else None


def _forms(word: str) -> set[str]:
    word = word.lower()
    return {word, word.rstrip("s"), word + "s"} if word else set()


def roster(chars, assets) -> list[tuple[str, str, str | None]]:
    """Sahnenin oyuncuları: karakterler ve nesneler (nesne olarak tanımlı canlılar dahil, ör. Hens). [(ad, tür, baş ad)]."""
    by_id = {a.id: a for a in assets}

    def head(name, *texts):
        for text in texts:                          # "Pip, a tiny round fluffy baby chick …": baştaki ad atılır
            if text:
                return head_noun(fal_plan.short_description(name, text))
        return None
    out = [(ch.name, "character", head(ch.name, getattr(ch, "description", None),
                                       getattr(by_id.get(getattr(ch, "asset_id", None)), "description", None)))
           for ch in chars]
    out += [(a.name, "prop", head(a.name, a.description)) for a in assets if a.type == "prop"]
    return out


def present(shot, member, linked: set[str]) -> bool:
    """Oyuncu bu çekimde görünüyor mu: çekime bağlı, adı çekim tarifinde geçiyor ya da tarifin baş adı geçiyor (Nora
    SH01: "a tiny fluffy yellow chick" -> Pip). Yönetmen notu sayılmaz ("Do not show Nora")."""
    name, _, head = member
    if name in linked:
        return True
    text = _words(str((shot.prompt_spec or {}).get("prompt") or shot.description or ""))
    name_words = set().union(*(_forms(w) for w in re.findall(r"[a-zçğıöşü]+", name.lower()))) if name else set()
    return bool(text & name_words) or bool(head and text & _forms(head))


def label(member) -> str:
    name, kind, _ = member
    return f"the {name}" if kind == "prop" else name


def join_names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def frame_line(shot, members: list, linked: set[str]) -> str:
    """Çekime özel kadro: "only Nora and Pip in frame; the Hens are out of frame." (yalnızca bu çekimin satırında)."""
    inside = [m for m in members if present(shot, m, linked)]
    outside = [m for m in members if m not in inside]
    if not outside or not inside:
        return ""
    verb = "are" if len(outside) > 1 or outside[0][0].lower().endswith("s") else "is"
    return f"Only {join_names([label(m) for m in inside])} in frame; {join_names([label(m) for m in outside])} {verb} " \
           "out of frame."


HEIGHT_WORDS = ("eye level", "eye-level", "street level", "ground level", "ground-level", "low angle", "high angle",
                "aerial", "overhead", "from above", "bird's-eye", "birds-eye", "top-down", "drone", "worm's-eye")
HEIGHT_DEFAULT = "camera at human eye level / street level"     # çekimde kamera yüksekliği yazılmamışsa
ABOVE_WARNING = "Bu mekân referansı yukarıdan çekilmiş; sokak seviyesinde bir görsel yüklemeyi düşünün."
ABOVE = re.compile(r"\b(aerial|overhead|from above|overpass|bird'?s[- ]eye|top[- ]down|drone)\b", re.I)


def height_text(x) -> str:
    """Kamera yüksekliği yazılmamışsa "camera at human eye level / street level": model mekân referansının (ör. üst
    geçitten çekilmiş otoyol) bakış açısını kopyalamasın."""
    from app.core.chain import camera_of
    if camera_of(x).get("angle") in ("low", "high"):
        return ""
    text = " ".join(str(v) for v in ((x.prompt_spec or {}).get("prompt"), x.description, getattr(x, "director_note", None))
                    if v).lower()
    if any(w in text for w in HEIGHT_WORDS):
        return ""
    return HEIGHT_DEFAULT


def shot_line(i: int, x, start: float, end: float, dialogue: list, names: list[str], members: list | None = None,
              linked: set[str] | None = None) -> str:
    from app.core.chain import camera_of
    from app.hermes import translate
    from app.style.composer import sentence
    spec = x.prompt_spec or {}
    action = translate.to_english(str(spec.get("prompt") or x.description or ""), names, spec.get("lang"))
    first = re.split(r"(?<=[.!?])\s", action.strip(), maxsplit=1)[0].lower()
    # çekim tarifi kendi kamera cümlesiyle başlıyorsa (Nora: "Wide shot, 28 mm, slow gentle dolly-in.") başlık tekrar etmez
    camera = "" if any(w in first for w in CAMERA_WORDS) else plain_camera(camera_of(x))
    camera = ", ".join(c for c in (camera, height_text(x)) if c)
    parts = [f"Shot {i} ({seconds_label(start)}-{seconds_label(end)}s)" + (f", {camera}" if camera else "") + ":",
             sentence(action)]
    if members:
        parts.append(frame_line(x, members, linked or set()))
    for who, line in dialogue:
        if line.strip():
            parts.append(f'{who or "A voice"} says: "{" ".join(line.split())}".')
    if x.emotion and x.emotion != "neutral":
        parts.append(f"Mood: {x.emotion}.")
    if getattr(x, "director_note", None):                 # yalnızca kendi çekiminde
        parts.append(sentence("Director note: " + translate.to_english(x.director_note, names, spec.get("lang"))))
    return " ".join(p for p in parts if p)


def fit_length(engine: str, head: list[str], shots: list[str], tail: list[str], constraints: list[str]) -> tuple[str, list]:
    """Uç noktanın prompt sınırı (şema maxLength) aşılırsa önce tekrar eden cümleler, sonra "Visual constraints" listesi
    kısaltılır; çekim tariflerine dokunulmaz. (istem, uyarılar)."""
    limit = PROMPT_LIMIT.get(engine)

    def build(h, c):
        text = " ".join(x for x in (*h, *shots, *tail) if x)
        return f"{text} Visual constraints: {'; '.join(c)}." if c else text
    prompt = build(head, constraints)
    if not limit or len(prompt) <= limit:
        return prompt, []
    notes = []
    seen, dedup = set(), []
    for line in head:
        key = " ".join(line.lower().split())
        if key not in seen:
            seen.add(key)
            dedup.append(line)
    head = dedup
    prompt = build(head, constraints)
    while len(prompt) > limit and constraints:
        constraints = constraints[:-1]
        prompt = build(head, constraints)
    notes.append(f"İstem {limit} karakter sınırına göre kısaltıldı (tekrarlar ve görsel kısıtlar; çekim tarifleri aynen).")
    if len(prompt) > limit:
        notes.append(f"İstem hâlâ sınırın üstünde ({len(prompt)} > {limit}): çekim tariflerini kısaltın.")
    return prompt, notes


def build_prompt(s, engine: str, part: list, refs: list, chars, assets, style, frame: bool) -> tuple[str, list[str]]:
    """İstem: stil, (süreklilik), numaralı referanslar, gerçek boyutlar, sahne kadrosu (karakterler ve nesne olarak
    tanımlı canlılar), çekim listesi (çekime özel kadro yalnızca kendi satırında), görsel kısıtlar."""
    from app.core.state import load_assets, load_characters
    from app.engine.storyboard import dialogue_of
    from app.hermes import lint, translate
    from app.style.constraints import positive_constraints
    names = [ch.name for ch in chars]
    members = roster(chars, assets)
    cast = join_names([label(m) for m in members])
    head = [fal_plan.style_line(style)]
    if frame:
        head.append(CONTINUOUS_SEEDANCE if engine == "seedance25" else CONTINUOUS_H3)
    head += [fal_plan.reference_sentence(engine, r, names) for r in refs]
    head += scale_lines(chars, assets)
    if members:
        head.append(f"The cast of this scene: {cast}. No other people, animals or figures appear.")
    else:
        head.append(lint.cast_constraint([]))
    shots = []
    for i, (x, a, b) in enumerate(timeline(part), start=1):
        linked = {ch.name for ch in load_characters(s, x.id)} | {a_.name for a_ in load_assets(s, x.id) if a_.type == "prop"}
        shots.append(shot_line(i, x, a, b, dialogue_of(s, x.id), names, members, linked))
    negatives = ", ".join(n for n in (translate.to_english(style.negative or "") if style else "",
                                      *[str((x.prompt_spec or {}).get("negative") or "") for x in part],
                                      lint.CAST_NEGATIVE) if n)
    c = positive_constraints(negatives)
    # sahne düzeyi kısıt bütün kadroyu kapsar (tavuk sürüsü "unpopulated" diye bastırılmasın)
    phrases = [f"only this scene's cast ({cast}) is present; no other people, animals or figures" if p == UNPOPULATED
               and members else p for p in c.phrases]
    prompt, notes = fit_length(engine, head, shots, [], phrases)
    return fal_plan.normalize_refs(engine, prompt), list(c.unmapped) + [f"! {n}" for n in notes]


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
    draft: bool = False
    lint: list = field(default_factory=list)          # onay durumu eşiğin altında kalan varlıklar (lint)
    lint_blocks: bool = False

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
                "blocked": bool(self.missing) or self.lint_blocks, "lint": self.lint, "draft": self.draft,
                "prompt_chars": len(self.prompt),
                "prompt_limit": PROMPT_LIMIT.get(self.engine),
                "notes": [w for w in self.warnings]}


def previous_frame(s, first, engine: str) -> dict:
    """Parçanın ilk çekiminden önceki klibin son karesi: {"transition", "previous", "previous_id", "previous_active",
    "model_switch", "expected", "frame_key", "winner_id", "note"}. Kare "continuous" geçişte ve sahne içi model
    değişiminde (önceki çekim aynı sahnede, başka motorla) verilir; çıkarma üretimde yapılır (seam/{varyasyon}.jpg).
    Önceki çekim referans yoluyla üretiliyorsa üretimde önce onun parçası güncel planıyla hazırlanır."""
    from app.core.chain import neighbours, transition_of
    from app.models import Variation
    transition = transition_of(first)
    out = {"transition": transition, "previous": None, "previous_id": None, "previous_active": False,
           "model_switch": False, "expected": False, "frame_key": None, "winner_id": None, "note": None}
    previous, _ = neighbours(s, first)
    if previous is None:
        out["note"] = "Sekansın ilk çekimi: önceki klip yok."
        return out
    switch = heading_key(previous) == heading_key(first) and previous.engine != first.engine
    out.update(previous=previous.scene_code, previous_id=str(previous.id), model_switch=switch,
               previous_active=active(previous.engine) and (previous.method or "generate") == "generate")
    if transition != "continuous" and not switch:
        out["note"] = "Kesme: önceki klibin son karesi verilmez, yalnızca referanslar."
        return out
    why = (f"sahne içi model değişimi ({fal_plan.ENGINE_TR.get(previous.engine, previous.engine)} → "
           f"{fal_plan.ENGINE_TR.get(first.engine, first.engine)})") if switch else "sürekli geçiş"
    winner = s.scalars(select(Variation).where(Variation.scene_id == previous.id, Variation.refined_rank == 1,
                                               Variation.video_key.is_not(None))).first()
    if winner is not None:
        out.update(expected=True, frame_key=f"seam/{winner.id}.jpg", winner_id=str(winner.id),
                   note=f"{why}: {previous.scene_code} kazanan klibinin gerçek son karesi (ffmpeg ile çıkarılır).")
    elif out["previous_active"]:
        out.update(expected=True, note=f"{why}: {previous.scene_code} henüz üretilmedi; önce o üretilir, son karesi "
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
    for a in assets:                                  # sarı uyarı: yukarıdan çekilmiş mekân referansı
        if a.type == "location" and ABOVE.search(f"{a.name} {a.description or ''}"):
            warnings.append(f"{a.name}: {ABOVE_WARNING}")
    if dropped:
        warnings.append(f"Referans sınırı ({limit} görsel): düşenler {', '.join(r.name for r in dropped)} "
                        "(çekim metinlerinde en az geçen mekân/nesne).")
    missing = missing_references(chars, assets)
    from app.hermes import lint as lint_
    checks = [lint_.check_scene(s, x.id, lint_cfg(cfg, engine)) for x in part]   # dağıtımın referans denetimi, aynen
    lint_missing = {(m.type, m.name): {"kind": m.type, "name": m.name, "status": m.status, "asset_id": m.asset_id}
                    for r in checks for m in r.missing}
    lint_blocks = any(r.blocks for r in checks)
    if lint_missing:
        warnings.append(("Onaylı referansı olmayan varlıklar (gereken en az durum: durağan görsel onaylı): " if lint_blocks
                         else "Onay bekleyen varlıklar (uyarı): ") + ", ".join(v["name"] for v in lint_missing.values()))
    if missing:
        warnings.append("Eksik referans: " + ", ".join(m["name"] for m in missing) +
                        " (görsel yükleyin; render engelli).")
    if cont.get("model_switch"):
        warnings.append(f"Sahne içi model değişimi ({cont.get('previous')} → {part[0].scene_code}): bu çekim ayrı "
                        "üretilir ve önceki çekimin gerçek son karesiyle başlar. Model değişimi sahne sınırında "
                        "önerilir.")
    if cont.get("transition") == "continuous" and cont.get("note") and not cont.get("frame_key"):
        warnings.append(cont["note"])                 # önceki klip henüz yok ya da kare verilmeyecek
    prompt, unmapped = build_prompt(s, engine, part, refs, chars, assets, style, bool(cont.get("expected")))
    warnings += [u[2:] for u in unmapped if u.startswith("! ")]           # uzunluk kısaltması
    unmapped = [u for u in unmapped if not u.startswith("! ")]
    total = round(sum(float(x.duration_s or 5.0) for x in part), 3)
    resolution, audio = fal_plan.resolution(engine, part[0], cfg), fal_plan.audio(engine, part[0])
    draft = fal_plan.draft(engine, part[0], cfg)
    frame_local = fal_plan.local_url(cont["frame_key"]) if cont.get("frame_key") else \
        ("local:<önceki klibin son karesi>" if cont.get("expected") else None)
    ref_urls = [fal_plan.local_url(r.key) for r in refs]
    if seed_frame:
        ref_urls = [frame_local, *ref_urls]
    plan_hash = stable_hash([engine, resolution, audio, prompt, total, [r.key for r in refs], bool(cont.get("expected")),
                             [x.scene_code for x in part], *(["draft"] if draft else [])])
    part_hash = stable_hash([plan_hash, cont.get("frame_key"), [x.prompt_revision for x in part]])
    seed = seed_for(part_hash, "part", 0)
    endpoint, body = fal_plan.body(engine, "ref", prompt=prompt, duration_s=total, resolution_=resolution, audio_=audio,
                                   seed=seed, image_url=frame_local if engine == "h3max" else None,
                                   reference_urls=ref_urls, draft_=draft)
    files = len(ref_urls) + (1 if engine == "h3max" and frame_local else 0)
    if files > MAX_FILES[engine]:
        warnings.append(f"Dosya sınırı aşıldı ({files} > {MAX_FILES[engine]}).")
    images = len(ref_urls) + (1 if engine == "h3max" and frame_local else 0)
    plan = PartPlan(engine=engine, shots=part, timeline=[(x.scene_code, a, b) for x, a, b in timeline(part)], refs=refs,
                    dropped=dropped, missing=missing, continuity=cont, prompt=prompt, unmapped=unmapped,
                    endpoint=endpoint, body=body, resolution=resolution, audio=audio, duration_s=total,
                    cost=fal_plan.cost(engine, resolution, total, images, "ref", draft), plan_hash=plan_hash,
                    part_hash=part_hash,
                    seed=seed, warnings=warnings, draft=draft, lint=list(lint_missing.values()),
                    lint_blocks=lint_blocks)
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



# ---------- üretim tutarlılığı (karma kurgu) ----------

MIXED_WARNING = ("Bu çekim başka bir üretimden; geçiş tutarsız olabilir. Bütün sahneyi bu motorla değiştir ya da bu "
                 "çekimi ayrı üret.")


def generations(winners: dict) -> dict:
    """winners: {kod: (parça, parça kodları, motor, varyasyon)} -> {kod: tutarlılık}. Bir whole_scene parçasının çekimleri
    yalnızca AYNI üretimden gelebilir: kazananın üretimi kardeşlerinkiyle aynı değilse uyarı."""
    out = {}
    for code, (part, codes, engine, vid) in winners.items():
        if not part or not codes or len(codes) < 2:
            continue
        mismatched = [c for c in codes if c != code and c in winners and winners[c][0] != part]
        if mismatched:
            out[code] = {"consistent": False, "part": part, "engine": engine, "variation_id": vid,
                         "mismatched": mismatched, "message": MIXED_WARNING}
    return out


def scene_generations(s, sc) -> dict:
    from app.models import Variation
    winners = {}
    for x in scene_shots(s, sc):
        v = s.scalars(select(Variation).where(Variation.scene_id == x.id, Variation.refined_rank == 1)).first()
        if v is not None:
            winners[x.scene_code] = ((v.params or {}).get("part"), (v.params or {}).get("part_codes"), v.engine, str(v.id))
    return generations(winners)
