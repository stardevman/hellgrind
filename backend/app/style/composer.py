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
Senaryodaki durum sözcükleri (dark, off, broken, closed…) olumlu isteme "State: eyes dark." olarak açıkça yazılır.
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
    "dim": ("bright", "glowing"), "lifeless": ("glowing", "animated"), "broken": ("intact", "repaired"),
    "closed": ("open",), "shut": ("open",), "open": ("closed", "shut"), "empty": ("full",), "lit": ("dark", "unlit"),
    "glowing": ("dark", "unlit"), "cracked": ("intact",), "wet": ("dry",),
}
STATE_WORDS = "|".join(sorted(STATES, key=len, reverse=True))
# "with its eyes dark", "the lamp is off": belirteçli isim + (is/are/...) + durum; "a broken lamp": durum + isim
STATE_AFTER = re.compile(rf"\b(?:its|his|her|their|the|a|an)\s+([a-z-]{{3,}})\s+(?:(?:is|are|stay|stays|remain|"
                         rf"remains|still|now|all)\s+)*({STATE_WORDS})\b", re.IGNORECASE)
STATE_BEFORE = re.compile(rf"\b({STATE_WORDS})\s+([a-z-]{{3,}})\b", re.IGNORECASE)
STOP = {"with", "and", "the", "its", "his", "her", "while", "into", "from", "that", "this", "light", "shot"}


def state_terms(text: str | None) -> tuple[list[str], list[str]]:
    """(olumlu, negatif), İngilizce metinden (ücretli modda istem İngilizceye çevrilmiştir):
    "with its eyes dark" -> (["eyes dark"], ["glowing eyes", "lit eyes", "bright eyes"])."""
    pos, neg = [], []
    text = text or ""
    found = [(m.group(1), m.group(2)) for m in STATE_AFTER.finditer(text)]
    found += [(m.group(2), m.group(1)) for m in STATE_BEFORE.finditer(text)]
    for noun, state in found:
        noun_l, state_l = noun.lower(), state.lower()
        if noun_l in STOP or noun_l in STATES or state_l not in STATES:
            continue
        phrase = f"{noun_l} {state_l}"
        if phrase in pos:
            continue
        pos.append(phrase)
        neg += [f"{opposite} {noun_l}" for opposite in STATES[state_l]]
    return pos, list(dict.fromkeys(neg))


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
    english = lang == "en" or described != (shot.get("prompt") or "") or not translate.looks_turkish(described)
    states, opposite = state_terms(described) if english else ([], [])
    body = [prefix, sentence(described), sentence("State: " + ", ".join(states)) if states else "",
            camera_text(shot.get("camera")), *(asset_text(a, bible, names, lang) for a in ordered),
            lint.cast_constraint(cast)]
    designs = [x for a in ordered if a.type == "character"
               for x in invariant_negatives(translate.to_english(" ".join(str(i).split()), names, lang)
                                            for i in (getattr(a, "invariants", None) or []))]
    negative = _join_negative(translate.to_english(bible.negative or "") if bible else "",
                              translate.to_english(shot.get("negative", "") or "", names, lang), lint.CAST_NEGATIVE,
                              ", ".join(opposite), ", ".join(designs))
    return Composed(prompt=" ".join(x for x in body if x), negative=negative, prefix=prefix)
