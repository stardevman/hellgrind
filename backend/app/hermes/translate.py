"""Prompt çevirisi: sağlayıcıya giden Türkçe tarifler yerel ve ücretsiz bir modelle İngilizceye çevrilir.

Görüntü modelleri (Flux, Wan, Seedance) İngilizce istemi izler; Türkçe tarif verilince sahneyle ilgisiz görüntü
üretirler. Çeviri yalnızca bestecinin çıktısına uygulanır (style/composer.py): senaryo, çekim tarifi ve görsel özet
Türkçe kalır, böylece çeviri hiçbir çekimin yeniden üretilmesine yol açmaz.

- Model: Helsinki-NLP opus-mt-tr-en (MarianMT, Apache 2.0), imajda sabit sürümle durur (scripts/fetch_models.py);
  çalışma anında ağ gerekmez. Işın araması deterministiktir: aynı metin her zaman aynı çeviriyi verir.
- Metin cümlelere bölünür; yalnızca Türkçe görünen cümleler çevrilir, İngilizce olanlar (kamera, stil ifadeleri,
  İngilizce yazılmış senaryo) olduğu gibi kalır.
- Karakter adları çeviriden korunur ("Gül", "Işıl" birer kelime olarak çevrilmesin): çeviri sırasında modelin
  dokunmadığı yedek adlarla değiştirilir, sonra geri konur.
- PROMPT_TRANSLATION: auto = yalnızca ücretli modda; on = her zaman; off = hiç.
"""
import importlib.util
import os
import re
import threading

from app.config import settings

MT_DIR = "mt/opus-mt-tr-en"                     # scripts/fetch_models.py ile aynı
MT_ID = "opus-mt-tr-en@19c65427"
PACKAGES = ("torch", "transformers", "sentencepiece")
BEAMS, MAX_LENGTH = 4, 512
PROTECT_ATTEMPTS = 2

TR_LETTERS = set("çğıöşüÇĞİÖŞÜ")
TR_WORDS = frozenset("""
ve ile bir bu şu o da de ki mi mı mu mü için gibi kadar daha çok en ama fakat ya veya hem ne her hiç sonra önce
üzerinde altında içinde arasında yanında karşı doğru gibi olan var yok değil diye ise bile şimdi yavaşça hızla
""".split())
EN_WORDS = frozenset("""
the a an and or of to in on at with from by for into onto over under through as is are was were be his her their
its he she they it this that these those while then who which looking stands walks runs
""".split())
SENTENCE = re.compile(r"(?<=[.!?…])\s+")
GLOSSARY = (("yakın planda", "yakın çekimde"),)   # model "yakın plan"ı yanlış anlıyor ("in close proximity")
# Yedek adlar: modelin çevirmeden bıraktığı adlar, Türkçe ek uyumuna göre (son ünlü kalın/ince, ünlüyle biter mi).
STAND_INS = {"back": ("Murat", "Hakan", "Yusuf", "Okan"), "front": ("Mert", "Selim", "Ferit", "Cenk"),
             "back_vowel": ("Ayla", "Hamza"), "front_vowel": ("Hande", "Emre")}
LEGACY = str.maketrans("þÞðÐýÝ", "şŞğĞıİ")       # eğitim verisinin eski kodlaması: "Ayşe" -> "Ayþe"


class TranslationUnavailable(RuntimeError):
    """Türkçe tarif var ama çeviri modeli kullanılamıyor: sağlayıcıya Türkçe gitmesin (para boşa harcanır)."""


def enabled() -> bool:
    mode = settings.prompt_translation
    return mode == "on" or (mode == "auto" and not settings.dry_run)


def _tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def looks_turkish(text: str) -> bool:
    """Kaba ama yeterli: Türkçeye özgü harfler ve sık kelimeler, İngilizce sık kelimelerden fazlaysa Türkçe."""
    words = re.findall(r"[^\W\d_]+", text)
    tr = sum(1 for w in words if TR_LETTERS & set(w) or _tr_lower(w) in TR_WORDS)
    en = sum(1 for w in words if w.lower() in EN_WORDS)
    return tr > en


def looks_english(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text)
    tr = sum(1 for w in words if TR_LETTERS & set(w) or _tr_lower(w) in TR_WORDS)
    en = sum(1 for w in words if w.lower() in EN_WORDS)
    return en > tr


def needs_translation(sentence: str, lang: str | None) -> bool:
    """Türkçe senaryoda İngilizce görünmeyen her cümle; dili bilinmeyen metinde yalnızca Türkçe görünenler ("Taksi gelir."
    gibi kısa cümlelerde Türkçe harf olmayabilir: senaryo dili bilinirse o esas alınır)."""
    if not re.search(r"[^\W\d_]", sentence):
        return False
    return not looks_english(sentence) if lang == "tr" else looks_turkish(sentence)


def model_path() -> str:
    return os.path.join(settings.quality_model_dir, MT_DIR)


def unavailable() -> str | None:
    """Kullanılamıyorsa nedeni (Türkçe), hazırsa None. Modeli yüklemez."""
    for package in PACKAGES:
        if importlib.util.find_spec(package) is None:
            return f"{package} paketi kurulu değil (requirements-quality.txt)"
    if not os.path.exists(os.path.join(model_path(), "pytorch_model.bin")):
        return f"çeviri modeli yok: {model_path()} (scripts/fetch_models.py)"
    return None


# ---------- model ----------

_model = None
_lock = threading.Lock()
_cache: dict[str, str] = {}


def _load():
    global _model
    if _model is None:
        import warnings
        from transformers import MarianMTModel, MarianTokenizer
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*sacremoses.*")     # yalnızca bazı dillerde gerekir; tr-en'de değil
            _model = (MarianTokenizer.from_pretrained(model_path()), MarianMTModel.from_pretrained(model_path()).eval())
    return _model


def _translate(segments: list[str]) -> list[str]:
    """Modelle çeviri; sonuçlar süreç içinde önbelleklenir. Çıkarım bir kilitle sıraya girer (dalgalar iş parçacıklıdır)."""
    todo = [s for s in dict.fromkeys(segments) if s not in _cache]
    if todo:
        with _lock:
            import torch
            tok, model = _load()
            with torch.no_grad():
                out = model.generate(**tok(todo, return_tensors="pt", padding=True), num_beams=BEAMS,
                                     max_length=MAX_LENGTH)
            for src, dst in zip(todo, tok.batch_decode(out, skip_special_tokens=True)):
                _cache[src] = dst.translate(LEGACY).strip()
    return [_cache[s] for s in segments]


# ---------- metin ----------

def _spans(segment: str, name: str) -> list[tuple[int, int]]:
    pattern = re.compile(r"(?<!\w)" + re.escape(_tr_lower(name)) + r"(?!\w)")
    return [m.span() for m in pattern.finditer(_tr_lower(segment))]     # _tr_lower uzunluğu korur: konumlar eşleşir


def _kept(out: str, name: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(_tr_lower(name)), _tr_lower(out)) is not None


def _harmony(name: str) -> str:
    """Yedek adın Türkçe ekleri doğru taşıması için: son ünlünün kalınlığı ve adın ünlüyle bitip bitmediği."""
    low = _tr_lower(name)
    vowels = [c for c in low if c in "aıoueiöü"]
    back = not vowels or vowels[-1] in "aıou"
    return ("back" if back else "front") + ("_vowel" if low[-1:] in "aıoueiöü" else "")


def _protect(segment: str, names, attempt: int) -> tuple[str, dict[str, str]]:
    """Adları ek uyumu tutan yedek adlarla değiştirir ("Kerem'in" -> "Mert'in"); {yedek: ad} döner. Her denemede havuzdaki
    sıradaki ad kullanılır."""
    swaps: dict[str, str] = {}
    used = set(names) | {w for w in re.findall(r"\w+", segment)}
    for name in sorted({n for n in names if n and _spans(segment, n)}, key=len, reverse=True):
        pool = [x for x in STAND_INS[_harmony(name)] if x not in used]
        if not pool:
            continue
        stand_in = pool[attempt % len(pool)]
        used.add(stand_in)
        for start, end in reversed(_spans(segment, name)):
            segment = segment[:start] + stand_in + segment[end:]
        swaps[stand_in] = name
    return segment, swaps


def _normalize(segment: str) -> str:
    letters = [c for c in segment if c.isalpha()]
    if letters and not any(c.islower() for c in letters):     # "ÇATI - GÜN BATIMI." büyük harfle kötü çevrilir
        low = _tr_lower(segment)
        segment = low[:1].upper() + low[1:]
    for old, new in GLOSSARY:
        segment = re.sub(re.escape(old), new, segment, flags=re.IGNORECASE)
    return segment


def _translate_one(segment: str, names) -> str:
    """Önce olduğu gibi çevirir. Bir ad çeviride kaybolursa ("Gül güler" -> "The rose laughs", "Ayşe atlar" -> "He
    jumps") adlar yedek adlarla değiştirilip yeniden denenir; hiçbir deneme adları korumazsa ilk çeviri kalır."""
    present = [n for n in names if n and _spans(segment, n)]
    base = _normalize(segment)
    first = _translate([base])[0]
    if all(_kept(first, n) for n in present):
        return first
    for attempt in range(PROTECT_ATTEMPTS):
        protected, swaps = _protect(segment, present, attempt)
        out = _translate([_normalize(protected)])[0]
        if swaps and all(_kept(out, x) for x in swaps):
            for stand_in, name in swaps.items():
                out = re.sub(r"(?<!\w)" + stand_in + r"(?!\w)", name, out)
            return out
    return first


def to_english(text: str, names=(), lang: str | None = None) -> str:
    """Türkçe cümleleri İngilizceye çevirir, diğerlerini olduğu gibi bırakır. Çeviri kapalıysa metin aynen döner.
    names: korunacak karakter adları; lang: metnin geldiği senaryonun dili ("tr" / "en"), biliniyorsa."""
    if not text or not enabled():
        return text
    parts = SENTENCE.split(text.strip())
    turkish = [i for i, p in enumerate(parts) if needs_translation(p, lang)]
    if not turkish:
        return text
    reason = unavailable()
    if reason:
        raise TranslationUnavailable(f"Prompt Türkçe ama çeviri modeli kullanılamıyor ({reason}). Görüntü modelleri "
                                     "Türkçe tarifi anlamaz: imajı yeniden derleyin ya da aksiyon paragraflarını "
                                     "İngilizce yazın.")
    _translate([_normalize(parts[i]) for i in turkish])          # tek toplu çağrı: çoğu cümle burada biter
    for i in turkish:
        parts[i] = _translate_one(parts[i], names)
    return " ".join(parts)


def to_english_name(name: str, lang: str | None = None) -> str:
    """Mekân ve nesne adları ("Terk Edilmiş Dökümhane"): cümle biçimine çevrilip çevrilir, nokta atılır."""
    if not name or not enabled() or not needs_translation(name, lang):
        return name
    low = _tr_lower(name)
    out = to_english(low[:1].upper() + low[1:] + ".", lang=lang)
    return out.rstrip(".").strip() or name
