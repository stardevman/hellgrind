"""Prompt bestecisi: sağlayıcılara giden prompt'un tek kaynağı.

compose_prompt(shot, assets, bible) deterministiktir ve her zaman aynı sırayı izler:
  1. ışık öneki (ve palet)          stil kılavuzundan, her çekimde aynı ifade
  2. hareket tarzı                   stil kılavuzundan
  3. çekim tarifi ve kamera          sahnenin kendi istemi
  4. varlıklar                       her biri kendi render stili kuralıyla (kılavuzun tür kuralı ya da varlığın istisnası)
  5. "yalnızca listelenen karakterler" kısıtı
  6. negatifler                      kılavuz + çekim + kadro negatifi
Stil çekim bazında elle yazılmaz: ışık ve hareket dili yalnızca kılavuzdan gelir.

Ücretli modda Türkçe metinler (çekim tarifi, mekân ve nesne adları ve tarifleri, Türkçe yazılmış kılavuz alanları)
yerel modelle İngilizceye çevrilir (hermes/translate.py); karakter adları korunur. Ücretsiz modda metin olduğu gibidir.
"""
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
    return sentence(f"{what}: {body}")


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
    body = [prefix, sentence(translate.to_english(shot.get("prompt") or "", names, lang)), camera_text(shot.get("camera")),
            *(asset_text(a, bible, names, lang) for a in ordered), lint.cast_constraint(cast)]
    negative = _join_negative(translate.to_english(bible.negative or "") if bible else "",
                              translate.to_english(shot.get("negative", "") or "", names, lang), lint.CAST_NEGATIVE)
    return Composed(prompt=" ".join(x for x in body if x), negative=negative, prefix=prefix)
