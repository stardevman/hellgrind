"""Okur: küçük, açık bir Türkçe metinden-sese modeli (geçici sesin varsayılan motoru). Torch'suz çalıştırıcı.

Model: ahakan/okur-tts v1.0 (9,8 M parametre, tek ses, 48 kHz; ağırlıklar CC BY 4.0, kod Apache-2.0; Copyright 2026
Ahmet Hakan Beşel; anonim bir Common Voice gönüllüsünün sesi). Üç ONNX grafiği (metin, örnekleyici, çözücü) CPU'da
onnxruntime ile çalışır; dosyalar scripts/fetch_models.py ile sürüme sabitlenip indirilir ({kök}/tts/okur).

Bu modül, Okur deposundaki (github.com/ahmethakanbesel/okur-tts, 0889eda) ön yüzün ve tarayıcı çalıştırıcısının
torch'suz bir uyarlamasıdır: src/okur/frontend (alfabe, sıra sayıları, şapka geri getirme; circumflex.py aynen alındı),
src/okur/model/timeline.py (numpy ile) ve src/okur/export_web.py (gaussian_noise, OnnxSynthesizer.say).

Sayı, tarih ve kısaltma okuma normalizer-tr (Apache-2.0) ile yapılır. Paketin Linux/arm64 tekerleği olmadığı için
(ör. Apple silikonunda Docker) kurulu değilse basit bir yedek okuyucu kullanılır: tam sayılar, ondalıklar, yüzde, saat
ve sıra sayıları ("3. kat" -> "üçüncü kat").
"""
import json
import math
import os
import re
import threading
import unicodedata
import wave

import numpy as np

from app.post.okur.circumflex import restore

MODEL_SUBDIR = "tts/okur"
SAMPLE_RATE = 48000
ATTRIBUTION = "Okur TTS v1.0 (ahakan/okur-tts), CC BY 4.0, Ahmet Hakan Beşel"

# ---------- alfabe (src/okur/frontend/alphabet.py) ----------

PAD, UNK = "<pad>", "<unk>"
PUNCTUATION = " !\"%&'(),-./:;?"
LETTERS = "abcdefghijklmnopqrstuvwxyzçğıöşüâîû"
SYMBOLS: tuple[str, ...] = (PAD, UNK, *PUNCTUATION, *LETTERS)
STOI: dict[str, int] = {s: i for i, s in enumerate(SYMBOLS)}
UNK_ID = STOI[UNK]

_KEEP_ACCENT = frozenset("çğıöşüâîûÇĞİÖŞÜÂÎÛ")
_TYPOGRAPHY = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "´": "'", "`": "'", "“": '"', "”": '"', "„": '"',
                             "«": '"', "»": '"', "–": "-", "—": "-", "−": "-", "…": "..."})
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")
_SPACES = re.compile(r"\s+")


def turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def clean(text: str) -> str:
    return _UNSAFE.sub(" ", unicodedata.normalize("NFC", text.encode("utf-8", "ignore").decode("utf-8")))


def to_alphabet(text: str) -> str:
    """Türkçe küçük harf; alfabede olmayan aksanlar atılır (é -> e), okunamayan her şey boşluk olur."""
    out = []
    for ch in turkish_lower(text.translate(_TYPOGRAPHY)):
        plain = ch if ch in _KEEP_ACCENT else "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(plain if plain and all(c in STOI for c in plain) else " ")
    return _SPACES.sub(" ", "".join(out)).strip()


def encode(letters: str) -> list[int]:
    return [STOI.get(ch, UNK_ID) for ch in letters]


# ---------- yedek sayı okuyucu (normalizer-tr yoksa) ----------

ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
SCALES = [(10 ** 9, "milyar"), (10 ** 6, "milyon"), (10 ** 3, "bin")]
_ORDINAL_SUFFIX = {"a": "ıncı", "ı": "ıncı", "e": "inci", "i": "inci", "o": "uncu", "u": "uncu", "ö": "üncü", "ü": "üncü"}


def _below_thousand(n: int) -> list[str]:
    h, rest = divmod(n, 100)
    words = ([] if h == 0 else ["yüz"] if h == 1 else [ONES[h], "yüz"])
    return words + [w for w in (TENS[rest // 10], ONES[rest % 10]) if w]


def number_words(n: int) -> str:
    """Tam sayıyı Türkçe okur: 1250 -> "bin iki yüz elli", 0 -> "sıfır"."""
    if n == 0:
        return "sıfır"
    if n < 0:
        return "eksi " + number_words(-n)
    words = []
    for value, name in SCALES:
        q, n = divmod(n, value)
        if q:
            words += ([name] if q == 1 and value == 1000 else [*number_words(q).split(), name])
    return " ".join(words + _below_thousand(n))


def ordinal_words(n: int) -> str:
    """Sıra sayısı, ünlü uyumuyla: 3 -> "üçüncü", 4 -> "dördüncü", 6 -> "altıncı"."""
    words = number_words(n).split()
    last = "dörd" if words[-1] == "dört" else words[-1]
    vowel = next(c for c in reversed(last) if c in _ORDINAL_SUFFIX)
    suffix = _ORDINAL_SUFFIX[vowel]
    return " ".join(words[:-1] + [last + (suffix[1:] if last[-1] in _ORDINAL_SUFFIX else suffix)])


def _digits(s: str) -> int:
    return int(s.replace(".", ""))


_FALLBACK_RULES = [
    (re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b(?![.,]\d)"),                # 09:30, 09.30 -> dokuz otuz
     lambda m: " ".join(w for w in (number_words(int(m[1])), number_words(int(m[2])) if int(m[2]) else "") if w)),
    (re.compile(r"%\s?(\d+(?:,\d+)?)|(\d+(?:,\d+)?)\s?%"),                      # %5, 5% -> yüzde beş
     lambda m: "yüzde " + (m[1] or m[2])),
    (re.compile(r"\b(\d{1,3}(?:\.\d{3})+|\d+),(\d+)\b"),                       # 3,5 -> üç virgül beş
     lambda m: f"{number_words(_digits(m[1]))} virgül {number_words(int(m[2]))}"),
    (re.compile(r"\b(\d{1,3}(?:\.\d{3})+|\d+)\b"), lambda m: number_words(_digits(m[1]))),
]
# "3. kat", "1. Dünya Savaşı": sayı ve nokta, ardından aynı satırda bir kelime ya da başka bir sıra sayısı.
_ORDINAL = re.compile(r"(?<![\d.,:])\d+\.(?=[ \t]+(?:[^\W\d_]|\d+\.))")


def read_numbers(text: str) -> str:
    """Yedek okuyucu: sıra sayıları, saat, yüzde, ondalık ve tam sayılar kelimeyle yazılır."""
    text = _ORDINAL.sub(lambda m: ordinal_words(int(m.group()[:-1])), text)
    for rule, sub in _FALLBACK_RULES:
        text = rule.sub(sub, text)
    return text


class Frontend:
    """Yazılı Türkçe -> modelin harfleri: temizle, sıra sayıları, sayı/tarih okuma, küçük harf, şapka, alfabe."""

    def __init__(self) -> None:
        try:
            from normalizer_tr import Hint, Normalizer
            self._normalizer, self._hint = Normalizer(), Hint
        except ImportError:
            self._normalizer = self._hint = None

    @property
    def full(self) -> bool:
        """normalizer-tr kurulu mu (değilse yedek okuyucu)."""
        return self._normalizer is not None

    def __call__(self, text: str) -> str:
        text = clean(text or "")
        if not text.strip():
            return ""
        if self._normalizer is None:
            return to_alphabet(restore(turkish_lower(read_numbers(text))))
        text = _ORDINAL.sub(self._ordinal, text)
        return to_alphabet(restore(turkish_lower(self._normalize(text))))

    def _ordinal(self, m: re.Match) -> str:
        number = m.group()
        try:
            return self._normalizer.normalize(number, ambiguity_policy="fallback",
                                              hints=[self._hint(0, len(number), "ordinal")]).normalized_text
        except Exception:
            return number

    def _normalize(self, text: str) -> str:
        try:
            return self._normalizer.normalize(text, ambiguity_policy="fallback").normalized_text
        except Exception:               # geçersiz girdi ya da kaynak sınırı: cümleyi kaybetmektense kelimeleri koru
            return read_numbers(text)


# ---------- kelime-harf zaman çizelgesi (src/okur/model/timeline.py, numpy) ----------

def words(letters: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Her harfin kelimesi ve o kelimenin ilk harfi. Kelime, ardından gelen boşluk ve noktalamayı da taşır."""
    starts = [i for i, ch in enumerate(letters) if ch != " " and (i == 0 or letters[i - 1] == " ")] or [0]
    bounds = [0, *starts[1:], len(letters)]
    cw, wstart = [], []
    for w, (a, b) in enumerate(zip(bounds, bounds[1:])):
        cw += [w] * (b - a)
        wstart += [a] * (b - a)
    return np.array(cw, np.int64), np.array(wstart, np.int64), len(bounds) - 1


def word_frames(dur: np.ndarray, cw: np.ndarray, n_words: int) -> np.ndarray:
    """Kelime başına tam kare; toplam üzerinden yuvarlanır, böylece süreler toplamından kaymaz."""
    per_word = np.bincount(cw, weights=dur.astype(np.float64), minlength=n_words)
    ends = np.round(np.cumsum(per_word)).astype(np.int64)
    return np.clip(np.diff(ends, prepend=0), 0, None)


def frame_timeline(counts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Her karenin kelimesi ve kelime içindeki yeri [0, 1)."""
    fw = np.repeat(np.arange(counts.size, dtype=np.int64), counts)
    start = (np.cumsum(counts) - counts)[fw]
    fp = ((np.arange(fw.size) - start) / counts[fw]).astype(np.float32)
    return fw, fp


def gaussian_noise(seed: int, count: int) -> np.ndarray:
    """Tohumlu standart normal değerler (mulberry32 + Box-Muller; Okur'un tarayıcı çalıştırıcısıyla bire bir aynı)."""
    state = seed & 0xFFFFFFFF
    out = np.empty(count, dtype=np.float32)

    def uniform() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        z = state
        z = ((z ^ (z >> 15)) * (z | 1)) & 0xFFFFFFFF
        z ^= (z + (((z ^ (z >> 7)) * (z | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((z ^ (z >> 14)) & 0xFFFFFFFF) / 4294967296.0

    for i in range(0, count, 2):
        u1, u2 = 1.0 - uniform(), uniform()
        r = math.sqrt(-2.0 * math.log(u1))
        out[i] = r * math.cos(2 * math.pi * u2)
        if i + 1 < count:
            out[i + 1] = r * math.sin(2 * math.pi * u2)
    return out


# ---------- çalıştırıcı (src/okur/export_web.py › OnnxSynthesizer) ----------

def model_path(root: str) -> str:
    return os.path.join(root, MODEL_SUBDIR)


def available(root: str) -> bool:
    """Model dosyaları {kök}/tts/okur altında eksiksiz mi."""
    path = model_path(root)
    try:
        files = json.load(open(os.path.join(path, "config.json")))["files"].values()
    except (OSError, ValueError, KeyError):
        return False
    return all(os.path.exists(os.path.join(path, f)) for f in files)


class Voice:
    def __init__(self, path: str, threads: int = 0) -> None:
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads           # 0: onnxruntime'ın varsayılanı
        options.log_severity_level = 3
        with open(os.path.join(path, "config.json")) as f:
            self.config = json.load(f)
        self.sessions = {k: ort.InferenceSession(os.path.join(path, v), options, providers=["CPUExecutionProvider"])
                         for k, v in self.config["files"].items() if v.endswith(".onnx")}
        self.frontend = Frontend()
        self.lock = threading.Lock()

    def say(self, text: str, *, speed: float = 1.0, seed: int = 0) -> np.ndarray:
        """48 kHz mono float32 ses; okunacak harf yoksa boş dizi."""
        letters = self.frontend(text)
        if not letters.strip(" !\"%&'(),-./:;?"):
            return np.zeros(0, np.float32)
        ids = np.array([encode(letters)], dtype=np.int64)
        with self.lock:                                  # onnxruntime oturumu iş parçacıkları arasında paylaşılır
            h, dur = (np.asarray(x) for x in self.sessions["text"].run(None, {"ids": ids}))
            dur = dur / speed
            cw, wstart, n_words = words(letters)
            fw, fp = frame_timeline(word_frames(dur[0], cw, n_words))
            if not fw.size:
                return np.zeros(0, np.float32)
            steps, c = len(self.config["times"]), self.config["latent_dim"]
            noise = gaussian_noise(seed, steps * fw.size * c).reshape(steps, 1, fw.size, c)
            (z,) = self.sessions["sample"].run(None, {
                "h": h, "dur": dur.astype(np.float32), "cw": cw[None], "wstart": wstart[None], "fw": fw[None],
                "fp": fp[None], "noise": noise})
            (audio,) = self.sessions["decoder"].run(None, {"z": z})
        return np.asarray(audio, dtype=np.float32).reshape(-1)


_VOICES: dict[str, Voice] = {}
_LOAD = threading.Lock()


def voice(root: str) -> Voice:
    """Süreç başına bir kez yüklenir (~23 MB)."""
    path = model_path(root)
    with _LOAD:
        if path not in _VOICES:
            _VOICES[path] = Voice(path)
        return _VOICES[path]


def write_wav(path: str, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
