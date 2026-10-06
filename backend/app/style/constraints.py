"""Negatiflerden pozitif kısıtlar (MiniMax H3).

H3 CFG damıtılmıştır: negatif istem almaz. Stil kılavuzunun ve çekimin negatifleri kaybolmasın diye istemin sonuna
pozitif kısıtlar olarak eklenir: "bulanık" yerine "keskin odak, net konturlar". İstenmeyen şeyin adı isteme yazılmaz
(model adı geçen kavramı çizme eğilimindedir). Dönüşüm deterministiktir: aynı negatif her zaman aynı kısıtları verir.

Tanınmayan terimler isteme eklenmez, ayrıca döner: servis geçmişine yazılır ve stil kılavuzunda pozitif bir ifadeyle
yeniden yazılması için panelde gösterilir. Ücretli modda negatifler besteciden İngilizce gelir; Türkçe kökler de tanınır.
"""
import re
from dataclasses import dataclass

# (anahtar kelimeler, pozitif kısıt). Sıra önemlidir: ilk eşleşen kural kazanır. Anahtarlar küçük harfle, kelime başından
# aranır (kök + ek eşleşir: "deform" -> "deformed"); "\b" ile biten anahtar tam kelimedir ("text" -> "texture" değil).
RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("extra people", "crowd", "bystander", "passenger", "unlisted character", "background people", "kalabalık",
      "fazla kişi", "figüran"), "only the listed characters are present and the surroundings are otherwise unpopulated"),
    (("duplicate", "clone", "twin", "kopya", "ikiz"), "each character appears exactly once"),
    (("blur", "out of focus", "soft focus", "bulanık", "odak dış", r"flu\b"), "sharp focus with crisp, clean contours"),
    (("low quality", "worst quality", "low-res", "lowres", "low resolution", "jpeg", "compression", "artifact",
      "artefakt", "düşük kalite", "düşük çözünürlük", "sıkıştırma"), "pristine, high-detail rendering"),
    (("noise", "noisy", "grain", "gren", "kumlu", "parazit"), "clean, smooth surfaces with even tones"),
    (("watermark", "logo", "signature", "caption", "subtitle", r"text\b", r"texts\b", "lettering", "filigran", "imza", "altyazı", "yazı"),
     "a clean, unmarked image area"),
    (("deform", "distort", "disfigur", "bad anatomy", "extra limb", "extra finger", "missing finger", "mutat",
      "malform", "bozuk anatomi", "fazla parmak", "eksik parmak", "çarpık"),
     "anatomically correct bodies with natural proportions and well-formed hands"),
    (("morph", "warp", "melt", "shape shift", "erime", "biçim değiş"), "objects and characters keep consistent shapes throughout"),
    (("flicker", "jitter", "strob", "titre", "kırpış"), "temporally stable lighting and smooth, steady motion"),
    (("shaky", "camera shake", "sarsıntı"), "steady, controlled camera movement"),
    (("static", "frozen", "still image", "slideshow", "donuk", "hareketsiz"), "continuous, natural motion throughout the shot"),
    (("oversaturat", "aşırı doygun", "garish", "cırtlak"), "balanced, natural color saturation"),
    (("overexpos", "blown highlight", "aşırı pozlan", "patlamış"), "controlled highlights that keep full detail"),
    (("underexpos", "too dark", "az pozlan", "çok karanlık"), "well-exposed shadows with readable detail"),
    (("photoreal", "realistic photo", "photograph", "fotogerçekçi", "fotoğraf"), "a stylized, hand-crafted animated look"),
    (("cartoon", "anime", "çizgi film"), "grounded, cinematic realism"),
    (("cropped", "cut off", "out of frame", "kırpılmış", "kadraj dış"), "subjects fully framed with comfortable headroom"),
    (("ugly", "bad composition", "çirkin", "kötü kompozisyon"), "a balanced, aesthetically refined composition"),
)
SPLIT = re.compile(r"[,;\n]+")
PATTERNS = tuple(re.compile(r"\b(?:" + "|".join(keys) + ")") for keys, _ in RULES)


@dataclass(frozen=True)
class Constraints:
    phrases: tuple[str, ...]          # isteme eklenen pozitif kısıtlar (tekrarsız, kural sırasıyla)
    unmapped: tuple[str, ...]         # tanınmayan negatif terimler (isteme eklenmez)


def terms(negative: str) -> list[str]:
    return [t.strip() for t in SPLIT.split(negative or "") if t.strip()]


def positive_constraints(negative: str) -> Constraints:
    hits: dict[int, str] = {}
    unmapped: list[str] = []
    for term in terms(negative):
        low = term.lower()
        for i, pattern in enumerate(PATTERNS):
            if pattern.search(low):
                hits.setdefault(i, RULES[i][1])
                break
        else:
            if low not in (u.lower() for u in unmapped):
                unmapped.append(term)
    return Constraints(phrases=tuple(hits[i] for i in sorted(hits)), unmapped=tuple(unmapped))


def constrained_prompt(prompt: str, negative: str) -> tuple[str, Constraints]:
    """İstem + "Visual constraints: ...". Negatif yoksa istem değişmez."""
    c = positive_constraints(negative)
    if not c.phrases:
        return prompt, c
    return f"{prompt.rstrip()} Visual constraints: {'; '.join(c.phrases)}.", c
