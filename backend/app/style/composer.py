"""Prompt bestecisi: sağlayıcılara giden prompt'un tek kaynağı.

compose_prompt(shot, assets, bible) deterministiktir ve her zaman aynı sırayı izler:
  1. ışık öneki (ve palet)          stil kılavuzundan, her çekimde aynı ifade
  2. hareket tarzı                   stil kılavuzundan
  3. çekim tarifi ve kamera          sahnenin kendi istemi
  4. varlıklar                       her biri kendi render stili kuralıyla (kılavuzun tür kuralı ya da varlığın istisnası)
  5. "yalnızca listelenen karakterler" kısıtı
  6. negatifler                      kılavuz + çekim + kadro negatifi + durum sözcüklerinin zıtları ("eyes dark" ->
                                     glowing eyes) + karakter değişmezlerinin zıtları ("exactly one antenna" -> second
                                     antenna, extra antenna)
Senaryodaki ve yönetmen notundaki durum sözcükleri (dark, switched off, broken, closed…) istemin EN SONUNA, referans ve
değişmezlerden sonra açık öncelikle yazılır: "Exception for this shot, overriding the reference images and the invariants:
the eyes are dark — unlit, no glow." Yönetmen notu ("Değişiklik iste") "Director note: …" olarak eklenir.
Stil çekim bazında elle yazılmaz: ışık ve hareket dili yalnızca kılavuzdan gelir.

Ücretli modda Türkçe metinler (çekim tarifi, mekân ve nesne adları ve tarifleri, Türkçe yazılmış kılavuz alanları)
yerel modelle İngilizceye çevrilir (hermes/translate.py); karakter adları korunur. Ücretsiz modda metin olduğu gibidir.
"""
import re
from dataclasses import dataclass

from app.hermes import lint, translate
from app.style.bible import effective_style

STYLE_TEXT = {
    "clean_3d": "clean 3D render, smooth stable surfaces, crisp edges, consistent geometry in motion",
    "painterly": "painterly watercolor look, visible brush texture, soft washes",
    "stylized_2d": "stylized 2D animation, flat shading, clean line art",
}
TYPE_LABEL = {"location": "Location", "character": "Character", "prop": "Prop"}
TYPE_ORDER = ("location", "character", "prop")
SHOT_TEXT = {"extreme_wide": "extreme wide shot", "wide": "wide shot", "pov": "point-of-view shot", "medium": "medium shot",
             "over_shoulder": "over-the-shoulder shot", "close_up": "close-up", "extreme_close_up": "extreme close-up"}
MOVE_TEXT = {"static": "static camera", "dolly_in": "slow dolly in", "dolly_out": "slow dolly out", "pan": "pan",
             "tilt": "tilt", "crane": "crane move", "orbit": "orbiting camera", "tracking": "tracking shot",
             "handheld": "handheld camera"}
ANGLE_TEXT = {"low": "low angle", "eye": "eye-level", "high": "high angle"}


@dataclass(frozen=True)
class Composed:
    prompt: str
    negative: str
    prefix: str          # 1 + 2: aynı stil sürümündeki her çekimde birebir aynı


def sentence(text: str | None) -> str:
    text = " ".join((text or "").split()).rstrip(" ,;")
    if not text:
        return ""
    return text if text[-1] in ".!?…" else text + "."


def style_text(style: str) -> str:
    return STYLE_TEXT.get(style, style)


def camera_text(camera: dict | None) -> str:
    camera = camera or {}
    parts = [SHOT_TEXT.get(camera.get("shot")), MOVE_TEXT.get(camera.get("movement")), ANGLE_TEXT.get(camera.get("angle"))]
    if camera.get("lens_mm"):
        parts.append(f"{camera['lens_mm']} mm lens")
    parts = [p for p in parts if p]
    return sentence("Camera: " + ", ".join(parts)) if parts else ""


def style_prefix(bible) -> str:
    if bible is None:
        return ""
    lighting = sentence(translate.to_english(bible.lighting_prefix or ""))
    if bible.palette:
        lighting = " ".join(x for x in (lighting, sentence("Palette: " + ", ".join(bible.palette))) if x)
    return " ".join(x for x in (lighting, sentence(translate.to_english(bible.motion_style or ""))) if x)


def asset_text(asset, bible, names=(), lang: str | None = None) -> str:
    name = asset.name if asset.type == "character" else translate.to_english_name(asset.name, lang)
    what = f"{TYPE_LABEL[asset.type]} {name}"
    description = translate.to_english(" ".join((asset.description or "").split()), names, lang).rstrip(".")
    body = ", ".join(x for x in (description, style_text(effective_style(asset, bible))) if x)
    return " ".join(x for x in (sentence(f"{what}: {body}"), invariant_text(asset, names, lang)) if x)


def invariant_text(asset, names=(), lang: str | None = None) -> str:
    """Tasarım değişmezleri (varlık sayfasında; ör. Bolt: "exactly one bent antenna with a small bulb"): her kare ve
    video istemine aynen eklenir; model ayrıntıyı (anten sayısı) unutmasın."""
    items = [translate.to_english(" ".join(str(x).split()), names, lang).rstrip(".")
             for x in (getattr(asset, "invariants", None) or []) if str(x).strip()]
    return sentence(f"{asset.name} must always have: {'; '.join(items)}") if items else ""


QUANTITY = re.compile(r"^(?:exactly\s+)?(?:one|a\s+single|single|a|an)\s+", re.IGNORECASE)
SPLIT = re.compile(r"\s+(?:with|in|over|under|on|and)\s+|,\s*", re.IGNORECASE)
ARTICLE = re.compile(r"^(?:a|an|the|one|two|its|his|her|their)\s+", re.IGNORECASE)


def _head(phrase: str) -> str:
    """İsim öbeğinin baş ismi (son kelime): "small bulb" -> "bulb", "white t-shirt" -> "t-shirt"."""
    words = [w for w in re.split(r"\s+", ARTICLE.sub("", phrase.strip(" .;:"))) if w]
    return words[-1].lower() if words else ""


def invariant_negatives(invariants) -> list[str]:
    """Değişmezlerden otomatik zıtlar (negatif istem): tasarım zincir boyunca kaymasın.
    "exactly one bent antenna with a small bulb" -> second antenna, extra antenna, missing antenna, extra bulb, missing bulb;
    "round yellow glasses (always worn)" -> missing glasses; tekil isim için "extra X, missing X", çoğul için "missing X"."""
    out: list[str] = []
    for item in invariants or ():
        text = re.sub(r"\([^)]*\)", " ", str(item)).strip()
        if not text:
            continue
        single = bool(QUANTITY.match(text))
        for i, part in enumerate(p for p in SPLIT.split(QUANTITY.sub("", text)) if p.strip()):
            noun = _head(part)
            if len(noun) < 3:
                continue
            plural = noun.endswith("s") and not noun.endswith("ss")
            if single and i == 0:
                out += [f"second {noun}", f"extra {noun}", f"two {noun}s", f"missing {noun}"]
            elif plural:
                out += [f"missing {noun}"]
            else:
                out += [f"extra {noun}", f"missing {noun}"]
    return list(dict.fromkeys(out))


# Senaryodaki durum sözcükleri (ör. SH03 "eyes dark"): olumlu isteme açıkça, zıtları negatife
STATES = {
    "dark": ("glowing", "lit", "bright"), "unlit": ("glowing", "lit"), "off": ("glowing", "lit", "switched on"),
    "switched off": ("glowing", "lit", "switched on"), "turned off": ("glowing", "lit", "switched on"),
    "powered off": ("glowing", "lit", "switched on"),
    "dim": ("bright", "glowing"), "lifeless": ("glowing", "animated"), "broken": ("intact", "repaired"),
    "closed": ("open",), "shut": ("open",), "open": ("closed", "shut"), "empty": ("full",), "lit": ("dark", "unlit"),
    "glowing": ("dark", "unlit"), "cracked": ("intact",), "wet": ("dry",),
}
UNLIT = {"dark", "unlit", "off", "switched off", "turned off", "powered off", "lifeless"}
# Durumun açık tarifi ("Exception for this shot: the eyes are dark — unlit, no glow")
STATE_TEXT = {"dark": "dark — unlit, no glow", "unlit": "unlit — no glow", "off": "switched off — dark, no glow",
              "switched off": "switched off — dark, no glow", "turned off": "switched off — dark, no glow",
              "powered off": "switched off — dark, no glow", "lifeless": "lifeless and inactive — no glow"}
STATE_WORDS = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(STATES, key=len, reverse=True))
# "with its eyes dark", "the lamp is off", "Bolt is still switched off", "eyes dark and unlit": isim + (is/are/...) +
# durum (+ "and" + durum); "a broken lamp": durum + isim
STATE_AFTER = re.compile(rf"\b(?:(?:its|his|her|their|the|a|an)\s+)?([a-z-]{{3,}})\s+(?:(?:is|are|stay|stays|remain|"
                         rf"remains|still|now|all|completely)\s+)*({STATE_WORDS})(?:\s+and\s+({STATE_WORDS}))?\b",
                         re.IGNORECASE)
STATE_BEFORE = re.compile(rf"\b({STATE_WORDS})\s+([a-z-]{{3,}})\b", re.IGNORECASE)
STOP = {"with", "and", "the", "its", "his", "her", "while", "into", "from", "that", "this", "light", "shot", "but",
        # fiiller ("falls off", "sits lifeless", "jumps off"): isim değil
        "falls", "turns", "switches", "runs", "jumps", "gets", "comes", "goes", "takes", "puts", "steps", "walks", "sits",
        "stands", "looks", "flips", "pulls", "pushes", "drops", "knocks", "slides", "tips", "kicks", "rolls", "breaks",
        "cuts", "sets", "lays", "lies", "keeps", "stays", "remains", "was", "were", "has", "have", "had", "been", "being",
        "seems", "appears", "becomes", "fades", "dies", "shuts", "lights", "glows", "blinks", "flickers"}


def _noun_ok(noun: str) -> bool:
    n = noun.lower()
    return not (n in STOP or n in STATES or n.endswith(("ed", "ing")) or n in ("is", "are"))


def state_list(text: str | None) -> list[tuple[str, str]]:
    """[(isim, durum)] İngilizce metinden: "Bolt is still switched off, eyes dark and unlit" ->
    [("bolt", "switched off"), ("eyes", "dark"), ("eyes", "unlit")]."""
    found = []
    for m in STATE_AFTER.finditer(text or ""):
        found += [(m.group(1), s) for s in (m.group(2), m.group(3)) if s]
    found += [(m.group(2), m.group(1)) for m in STATE_BEFORE.finditer(text or "")]
    out = []
    for noun, state in found:
        state_l = " ".join(state.lower().split())
        if _noun_ok(noun) and state_l in STATES and (noun.lower(), state_l) not in out:
            out.append((noun.lower(), state_l))
    return out


def state_terms(text: str | None, glow_colors=()) -> tuple[list[str], list[str]]:
    """(olumlu, negatif): "with its eyes dark" -> (["eyes dark"], ["glowing eyes", "lit eyes", "bright eyes",
    "eyes glow"]). glow_colors: değişmezlerde geçen renkler ("blue" -> "blue glow")."""
    pos, neg = [], []
    for noun, state in state_list(text):
        pos.append(f"{noun} {state}")
        neg += [f"{opposite} {noun}" for opposite in STATES[state]]
        if state in UNLIT:
            neg += [f"{noun} glow", *(f"{c} glow" for c in glow_colors)]
    return pos, list(dict.fromkeys(neg))


def state_exception(text: str | None) -> str:
    """Durum, referans görseli ve değişmezlerden SONRA ve açık öncelikle (Kıvılcım SH03/SH04: referansta yanık gözler ve
    "blue glass eyes" değişmezi "eyes dark" durumunu eziyordu)."""
    parts = []
    for noun, state in state_list(text):
        verb = "are" if noun.endswith("s") and not noun.endswith("ss") else "is"
        subject = noun.capitalize() if noun[:1].isalpha() and noun not in ("eyes",) and verb == "is" and \
            (text or "").find(noun.capitalize()) >= 0 else f"the {noun}"
        parts.append(f"{subject} {verb} {STATE_TEXT.get(state, state)}")
    parts = list(dict.fromkeys(parts))
    return sentence("Exception for this shot, overriding the reference images and the invariants: " + "; ".join(parts)) \
        if parts else ""


COLORS = ("blue", "red", "green", "yellow", "orange", "purple", "pink", "white", "amber", "cyan", "golden")


def glow_colors(invariants) -> list[str]:
    """Değişmezlerde geçen renkler: "round glass eyes (glow blue only when switched on)" -> ["blue"]."""
    text = " ".join(str(x).lower() for x in invariants or ())
    return [c for c in COLORS if re.search(rf"\b{c}\b", text)]


def _join_negative(*parts: str) -> str:
    seen, out = set(), []
    for part in parts:
        for item in (part or "").split(","):
            item = item.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
    return ", ".join(out)


def compose_prompt(shot: dict, assets, bible) -> Composed:
    """shot: {"prompt", "negative"?, "camera"?, "characters": [ad], "lang"?}; assets: çekimdeki varlıklar; bible: etkin
    kılavuz ya da None. lang: çekim tarifinin dili ("tr" / "en"); çeviri bunu esas alır."""
    prefix = style_prefix(bible)
    lang = shot.get("lang")
    cast = list(shot.get("characters") or [])
    names = cast + [a.name for a in assets if a.type == "character"]
    ordered = sorted(assets, key=lambda a: (TYPE_ORDER.index(a.type), a.name))
    described = translate.to_english(shot.get("prompt") or "", names, lang)
    note = translate.to_english(" ".join((shot.get("note") or "").split()), names, lang)
    english = lang == "en" or described != (shot.get("prompt") or "") or not translate.looks_turkish(described)
    invariants = [i for a in ordered if a.type == "character" for i in (getattr(a, "invariants", None) or [])]
    state_source = " ".join(x for x in (described, note) if x) if english else ""
    states, opposite = state_terms(state_source, glow_colors(invariants)) if english else ([], [])
    # Sıra: tarif, kamera, varlıklar (referans ve değişmezler), kadro kısıtı, yönetmen notu, durum istisnası (en son,
    # açık öncelikle: değişmezi ve referansı ezer)
    body = [prefix, sentence(described), camera_text(shot.get("camera")),
            *(asset_text(a, bible, names, lang) for a in ordered), lint.cast_constraint(cast),
            sentence("Director note: " + note) if note else "", state_exception(state_source) if states else ""]
    designs = [x for a in ordered if a.type == "character"
               for x in invariant_negatives(translate.to_english(" ".join(str(i).split()), names, lang)
                                            for i in (getattr(a, "invariants", None) or []))]
    negative = _join_negative(translate.to_english(bible.negative or "") if bible else "",
                              translate.to_english(shot.get("negative", "") or "", names, lang), lint.CAST_NEGATIVE,
                              ", ".join(opposite), ", ".join(designs))
    return Composed(prompt=" ".join(x for x in body if x), negative=negative, prefix=prefix)
