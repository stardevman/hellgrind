"""Ses denetimi: replik sesini yazıya döker ve senaryodaki metinle karşılaştırır (Ses sayfası).

Model: Duyu-small (KaanAydinli/Duyu-small, Apache-2.0), Türkçe + İngilizce konuşma tanıma; 19 M etkin parametre, int8
ONNX (~56 MB), CPU'da gerçek zamanın ~14 katı hızlı. Çalıştırıcı app/quality/duyu altındadır (aynen alındı); model
dosyaları scripts/fetch_models.py ile sürüme sabitlenip indirilir ({kök}/asr/duyu-small). Ağ ya da ücretli servis yok.

Karşılaştırma iki metni aynı biçime getirir: sayılar kelimeyle okunur (Duyu rakam yazar: "3. kat"; senaryo "üçüncü
kat" diyebilir), Türkçe küçük harf, noktalama ve şapka atılır. Harf hata oranı (CER, boşluklar hariç) MAX_CER'i
aşarsa kayıt replikle uyuşmuyor sayılır: yanlış repliğe yüklenmiş ya da metinden sapan kayıt animatiğe girmeden görünür.
"""
import os
import re
import threading

MODEL_SUBDIR = "asr/duyu-small"
MODEL_ID = "duyu-small@1f1c327"     # sonuç önbelleğinin anahtarında: model değişince kayıtlar yeniden dökülür
MAX_CER = 0.25
REQUIRED = ("engine.json", "fbank.npz", "passes.npz", "spm.model", "int8/encoder_pre.onnx", "int8/encoder_pass.onnx",
            "int8/encoder_post.onnx", "int8/decoder.onnx")
TURKISH = re.compile("[çğıöşüÇĞİÖŞÜ]")
# Sık İngilizce kelimeler; Türkçede de kelime olanlar (a, an, at, can, it, on, be, he, do…) bilerek yok
ENGLISH = frozenset("the and of to is are was were you your i i'm this that what where why how not don't will won't "
                    "we they she my me with for have has look go going".split())
_MODELS: dict = {}
_LOAD = threading.Lock()
_RUN = threading.Lock()


def model_path(root: str) -> str:
    return os.path.join(root, MODEL_SUBDIR)


def available(root: str) -> bool:
    path = model_path(root)
    return all(os.path.exists(os.path.join(path, f)) for f in REQUIRED)


def model(root: str):
    """Süreç başına bir kez yüklenir."""
    from app.quality.duyu import DuyuSmall
    path = model_path(root)
    with _LOAD:
        if path not in _MODELS:
            _MODELS[path] = DuyuSmall(path, quant="int8", threads=2)
        return _MODELS[path]


def lang_for(expected: str) -> str:
    """Dil Türkçeye zorlanır; metin İngilizce görünüyorsa (Türkçe harf yok, kelimelerin en az dörtte biri sık İngilizce
    kelime) model ne konuşulduysa onu yazar ("mix": Türkçe, İngilizce ya da karışık). Senaryo İngilizce de olabilir."""
    words = re.findall(r"[a-z']+", (expected or "").lower())
    english = sum(w in ENGLISH for w in words)
    return "mix" if not TURKISH.search(expected or "") and words and english * 4 >= len(words) else "tr"


def transcribe(path: str, root: str, lang: str = "mix") -> str:
    """Ses dosyasını (ffmpeg'in okuduğu her biçim) yazıya döker."""
    from app.quality.duyu.audio import load_audio
    wav = load_audio(path)
    with _RUN:
        return model(root).transcribe(wav, lang=lang)


_FRONTEND = None


def plain(text: str) -> str:
    """Karşılaştırma biçimi: sayılar kelimeyle, Türkçe küçük harf, şapkasız, noktalamasız, tek boşluklu."""
    global _FRONTEND
    from app.post import okur
    if _FRONTEND is None:
        _FRONTEND = okur.Frontend()
    letters = _FRONTEND(text or "").replace("'", "").translate(str.maketrans("âîû", "aiu"))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zçğıöşü ]", " ", letters)).strip()


def _distance(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def compare(expected: str, heard: str) -> dict:
    """{"cer", "wer", "ok"}: harf ve kelime hata oranı (beklenen metne göre) ve uyuşma kararı."""
    want, got = plain(expected), plain(heard)
    if not want:
        return {"cer": 0.0, "wer": 0.0, "ok": True}
    cer = _distance(want.replace(" ", ""), got.replace(" ", "")) / len(want.replace(" ", ""))
    wer = _distance(want.split(), got.split()) / len(want.split())
    return {"cer": round(min(cer, 1.0), 3), "wer": round(min(wer, 1.0), 3), "ok": cer <= MAX_CER}
