"""Sahne istemi yazıcı (referans modu): bir LLM (Hermes modeli) parçanın istemini yönetmen tarzında yazar. Referans
etiketleri eylemin içinde, somut sahneleme, oyunculuk ve zamanlama, sonda tek paragraf animasyon + ses + en çok iki kısa
kısıt; ~1.500–2.500 karakter (fal playground'daki elle yazılmış istemlerin dersi).

Ağ yok: LLM çağrısı `app.live.prompt_llm.ask` içindedir (ücretli kilit, bütçe rezervasyonu). Bu modül girdiyi, sistem
istemini, doğrulamayı, motor biçimine çeviriyi ve önbelleği (prompt_cache) yönetir. Yazım girdisinin özeti değişmedikçe
istem yeniden yazılmaz. Doğrulamadan iki kez geçemeyen yazım kaydedilir (plan kurallar istemine düşer, uyarı verir)."""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select

from app.config import settings
from app.core.hashing import stable_hash
from app.engine import fal_plan

log = logging.getLogger("hellgrind.prompt_writer")

WRITER_VERSION = 2                      # sistem istemi ya da girdi biçimi değişince artar (önbellek yenilenir)
TARGET_CHARS = (1500, 2500)             # hedef uzunluk (dışı yalnızca uyarı)
HARD_CHARS = (700, 4000)                # dışı doğrulama hatası
MAX_ATTEMPTS = 2                        # bir yazım + bir yeniden yazım; sonra kurallar istemi
OUTPUT_TOKENS = 1400                    # rezervasyon tahmini (yanıt üst sınırı)
CHARS_PER_TOKEN = 4
EXAMPLES = (("passport_rush.txt", "Passport Rush: airport check-in to police motorcycle, 3 shots"),
            ("taxi_director.txt", "Taxi: a country road and an open-top taxi, 3 shots"))   # yönetmenin iyi sonuç veren istemleri
EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "prompt_examples")
CALL_FAILED = "LLM çağrısı: "           # errors[0] bu önekle başlıyorsa çağrı hata verdi (doğrulama değil)
CALL_RETRY_S = 600                      # başarısız çağrı bu süreden sonra kendiliğinden yeniden denenebilir

SHOT_HEAD = re.compile(r"Shot\s+(\d+)\s*\(\s*(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*s\s*\)", re.I)
FORBIDDEN = (("Mood:", "\"Mood:\" etiketi kullanılmamalı"),
             ("Visual constraints", "uzun kısıt listesi (\"Visual constraints\") kullanılmamalı"))

SOURCE_TR = {"llm": "LLM istemi", "rules": "Kurallar istemi", "override": "Yönetmen istemi"}


# ---------- ayar ve kullanılabilirlik ----------

def mode() -> str:
    return "rules" if settings.prompt_writer == "rules" else "llm"


def unavailable() -> str | None:
    """LLM yazıcı çağrılamıyorsa Türkçe sebep (plan uyarısı); çağrılabiliyorsa None. Anahtarın değeri okunmaz."""
    from app.live import key_state
    if mode() != "llm":
        return None
    if settings.dry_run:
        return "Ücretsiz mod: LLM istem yazıcı çağrılmaz; kurallar istemi kullanılıyor."
    if not key_state("anthropic_api_key")["set"]:
        return "ANTHROPIC_API_KEY tanımlı değil: LLM istem yazıcı yerine kurallar istemi kullanılıyor."
    return None


def writer_model() -> str:
    """İstem yazıcının modeli: PROMPT_WRITER_MODEL, boşsa HERMES_MODEL."""
    return (settings.prompt_writer_model or "").strip() or settings.hermes_model


def input_hash(inp: dict) -> str:
    return stable_hash([WRITER_VERSION, writer_model(), inp])


# ---------- sistem istemi ----------

def example(engine: str, audio: bool, name: str = "passport_rush.txt") -> str:
    """Bir few-shot örneği (yönetmenin playground'da iyi sonuç veren istemi) motorun referans biçiminde."""
    with open(os.path.join(EXAMPLE_DIR, name), encoding="utf-8") as f:
        text = f.read().strip()
    if not audio:                                       # sesi kapalı motor: ses cümlesi örnekte de yok
        text = re.sub(r" Sound: [^;]*(?:;[^.]*)?\.", "", text)
    return fal_plan.normalize_refs(engine, text)


def examples(engine: str, audio: bool) -> str:
    return "\n\n".join(f"Example {i} ({about}):\n\n{example(engine, audio, name)}"
                       for i, (name, about) in enumerate(EXAMPLES, start=1))


def system_prompt(engine: str, audio: bool) -> str:
    tok = fal_plan.ref_token(engine, 1)
    tok2 = fal_plan.ref_token(engine, 2)
    sound = ("then the sound: ambience, effects and how the dialogue sounds, per shot where it changes; "
             if audio else "(this engine renders no audio: write no sound description); ")
    return f"""You are a film director writing ONE text-to-video prompt for a reference-to-video model that renders a \
whole scene (several shots) in a single generation. Write it the way a director briefs an animation team: concrete, \
visual, timed.

Reference images are numbered. In this prompt they are written exactly as "{tok}", "{tok2}", ... (this exact format).

Structure, in this order:
1. One short style sentence (from the given lighting and motion style).
2. Reference definitions, one sentence each: who/what it is, its token, its look (full description) and its invariants \
in the same sentence, e.g. "The girl {tok} is a slim ten-year-old ...; she always wears ...". For every location \
reference write: "Use <token> only for the look of the place (architecture, materials, colors, light); do not copy its \
camera position or viewpoint."
3. One paragraph per shot, starting exactly with "Shot k (a-bs), <shot size>, <camera movement and height>:" using the \
given labels and times verbatim. Then concrete staging: who is where in the frame (left/right/foreground/background), \
what they do and in which order, face and body acting with concrete verbs, dialogue in double quotes ("..."). Every \
time a character, prop or location acts or is acted upon, tag it with its token: "the girl {tok} ...", "... onto the \
counter of the terminal {tok2}". Every reference must be tagged inside at least one shot paragraph.
4. One final paragraph: animation style and timing, {sound}then at most two short constraint sentences such as \
"Exactly one girl and one attendant; no subtitles or text on screen."

Rules:
- Do not repeat the location heading in every shot. No "Mood:" labels. No long constraint lists.
- Do not invent events that are not in the script. You may resolve ambiguous staging (where someone sits, which side \
they enter from, which side the camera is on) in a sensible way, consistent across shots.
- Shot count, order, labels and times must match the given shots exactly. Keep the camera height given for each shot \
(e.g. "camera at human eye level / street level").
- Only the listed cast appears; no extra people, animals or figures.
- Target length 1,500-2,500 characters. Write in English. Output only the prompt text, no headings, no markdown.

Examples of the expected style (other scenes; follow their form, not their content). Keep the rules above even where \
an example phrases something differently (e.g. always write the location rule and tag every reference in a shot):

{examples(engine, audio)}"""


def user_prompt(inp: dict, errors: list[str] | None = None) -> str:
    public = {k: v for k, v in inp.items() if not k.startswith("_")}
    text = "Write the prompt for this scene part. Input (JSON):\n\n" + json.dumps(public, ensure_ascii=False, indent=1)
    if errors:
        text += ("\n\nYour previous answer had these problems; rewrite the whole prompt and fix them:\n- " +
                 "\n- ".join(errors))
    return text


def estimate_usd(engine: str, inp: dict) -> tuple[int, int, float]:
    """(girdi belirteci, çıktı belirteci, $) — bir yazım için tahmin (rezervasyon da bununla yapılır)."""
    from app.budget import pricing
    tokens_in = (len(system_prompt(engine, bool(inp.get("audio")))) + len(user_prompt(inp))) // CHARS_PER_TOKEN
    return tokens_in, OUTPUT_TOKENS, round(pricing.estimate_llm(tokens_in, OUTPUT_TOKENS, model=writer_model()), 6)


# ---------- doğrulama ----------

def _num(x: str) -> float:
    return float(x.replace(",", "."))


def ref_numbers(text: str) -> set[int]:
    return {int(next(g for g in m.groups() if g)) for m in fal_plan.IMAGE_REF.finditer(text or "")}


def validate(text: str, inp: dict) -> tuple[list[str], list[str]]:
    """(hatalar, uyarılar). Hata: çekim sayısı/sırası/zamanı plandan farklı, bir referans hiçbir çekim paragrafında
    etiketlenmemiş, olmayan referans, mekân bakış kuralı eksik, yasak etiket, sınır dışı uzunluk."""
    errors, notes = [], []
    text = (text or "").strip()
    if not text:
        return ["boş yanıt"], notes
    heads = list(SHOT_HEAD.finditer(text))
    want = inp["shots"]
    got = [(int(m.group(1)), _num(m.group(2)), _num(m.group(3))) for m in heads]
    if len(got) != len(want):
        errors.append(f"{len(want)} çekim bekleniyordu, {len(got)} çekim başlığı var (\"Shot k (a-bs)\")")
    else:
        for i, ((n, a, b), w) in enumerate(zip(got, want), start=1):
            if n != i or abs(a - w["start"]) > 0.05 or abs(b - w["end"]) > 0.05:
                errors.append(f"Shot {i} zamanı planla eşleşmiyor: yazılan {n} ({a:g}-{b:g}s), plan "
                              f"{w['label']}")
    known = {r["number"] for r in inp["references"]}
    if inp.get("frame") and inp["engine"] == "seedance25":
        known.add(1)                                    # @Image1 = önceki klibin son karesi
    body = text[heads[0].start():] if heads else ""
    used = ref_numbers(body)
    for r in inp["references"]:
        if r["number"] not in used:
            errors.append(f"{r['name']} ({r['token']}) hiçbir çekimde eylem içinde etiketlenmemiş")
        if r["kind"] == "location":
            view = re.search(rf"only for the look[^.]*", text, re.I)
            if not view or r["number"] not in ref_numbers(text[max(0, view.start() - 80):view.end()]):
                errors.append(f"{r['name']} için mekân bakış kuralı (\"Use {r['token']} only for the look of the "
                              "place…\") yok")
    extra = sorted(ref_numbers(text) - known)
    if extra:
        errors.append("olmayan referans: " + ", ".join(fal_plan.ref_token(inp["engine"], n) for n in extra))
    for needle, why in FORBIDDEN:
        if needle.lower() in text.lower():
            errors.append(why)
    n = len(text)
    if not HARD_CHARS[0] <= n <= HARD_CHARS[1]:
        errors.append(f"uzunluk {n} karakter (sınır {HARD_CHARS[0]}–{HARD_CHARS[1]})")
    elif not TARGET_CHARS[0] <= n <= TARGET_CHARS[1]:
        notes.append(f"LLM istemi {n} karakter (hedef {TARGET_CHARS[0]}–{TARGET_CHARS[1]}).")
    return errors, notes


def finalize(text: str, inp: dict) -> str:
    """Motorun referans biçimi ve (sürekli geçişte) son kare cümlesi; yazılan metne başka dokunulmaz."""
    from app.engine.scene_parts import CONTINUOUS_H3, CONTINUOUS_SEEDANCE
    text = fal_plan.normalize_refs(inp["engine"], (text or "").strip())
    if inp.get("frame"):
        sentence = CONTINUOUS_SEEDANCE if inp["engine"] == "seedance25" else CONTINUOUS_H3
        if sentence[:30].lower() not in text.lower():
            text = f"{sentence} {text}"
    return text


# ---------- önbellek ----------

@dataclass
class Written:
    input_hash: str
    prompt: str | None
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    attempts: int = 0
    at: float = 0.0

    @property
    def call_failed(self) -> bool:
        return self.prompt is None and bool(self.errors) and str(self.errors[0]).startswith(CALL_FAILED)

    def view(self) -> dict:
        return {"input_hash": self.input_hash, "ok": self.prompt is not None, "errors": self.errors, "model": self.model,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "usd": round(self.usd, 6),
                "attempts": self.attempts, "failed": "call" if self.call_failed else "validation" if self.prompt is None
                else None, "at": self.at}


def cached(s, ih: str) -> Written | None:
    from app.models import PromptCache
    row = s.get(PromptCache, ih)
    if row is None:
        return None
    return Written(ih, row.prompt, list(row.errors or []), [], row.model, row.input_tokens, row.output_tokens,
                   float(row.usd or 0), row.attempts, row.created_at.timestamp() if row.created_at else 0.0)


def _store(project_id, inp: dict, w: Written) -> None:
    from app.db import session_scope
    from app.models import PromptCache
    with session_scope() as s:
        row = s.get(PromptCache, w.input_hash) or PromptCache(input_hash=w.input_hash)
        row.project_id, row.engine, row.model = project_id, inp["engine"], w.model or writer_model()
        row.prompt, row.errors, row.attempts = w.prompt, w.errors, w.attempts
        row.input_tokens, row.output_tokens, row.usd = w.input_tokens, w.output_tokens, Decimal(str(round(w.usd, 6)))
        row.created_at = func.now()
        s.merge(row)


# ---------- yazım ----------

def _default_ask(project_id: str, system: str, user: str) -> dict:
    from app.live.prompt_llm import ask                # ücretli: kilit + bütçe rezervasyonu orada
    return ask(project_id, system, user)


ASK = _default_ask                                     # testler sahte istemciyle değiştirir


def write(project_id: str, inp: dict, force: bool = False) -> Written:
    """İstemi yazar (ya da önbellekten verir). Doğrulamadan geçmezse bir kez hatalarla yeniden yazdırır; yine geçmezse
    başarısız yazım (prompt=None, hatalar) kaydedilir. Ücretli çağrı yalnızca ASK içinde yapılır."""
    from app.db import session_scope
    ih = input_hash(inp)
    if not force:
        with session_scope() as s:
            hit = cached(s, ih)
        if hit is not None and not hit.call_failed:   # başarısız çağrı önbellek sayılmaz (yeniden denenir)
            return hit
    system = system_prompt(inp["engine"], bool(inp.get("audio")))
    w = Written(ih, None, model=writer_model())
    errors: list[str] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer = ASK(project_id, system, user_prompt(inp, errors))
        except Exception as e:
            from app.budget import killswitch
            if killswitch.ProjectFrozen in type(e).__mro__:
                raise
            from app.live import redact
            w.prompt, w.errors = None, [CALL_FAILED + redact(f"{type(e).__name__}: {e}")[:1000]]
            _store(project_id, inp, w)                 # plan "LLM yazımı başarısız: …" gösterir; çağıran olaya yazar
            raise
        w.attempts = attempt
        w.model = answer.get("model") or w.model
        w.input_tokens += int(answer.get("input_tokens") or 0)
        w.output_tokens += int(answer.get("output_tokens") or 0)
        w.usd += float(answer.get("usd") or 0)
        text = finalize(answer.get("text") or "", inp)
        errors, notes = validate(text, inp)
        if not errors:
            w.prompt, w.errors, w.notes = text, [], notes
            break
        log.warning("LLM istemi doğrulamadan geçmedi (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, errors)
        w.errors = errors
    _store(project_id, inp, w)
    return w


def choose(s, inp: dict, rules_prompt: str, override: dict | None, part_codes: list[str]) -> dict:
    """Planın istemi: yönetmen istemi > LLM istemi (önbellekte) > kurallar istemi. Ağ yok. {"prompt", "source",
    "warnings", "llm", "override", "estimate"}."""
    engine = inp["engine"]
    ih = input_hash(inp)
    out = {"prompt": rules_prompt, "source": "rules", "warnings": [], "llm": None, "override": None,
           "input_hash": ih, "writer": mode()}
    if override and override.get("text"):
        stale = []
        if override.get("part") and list(override["part"]) != list(part_codes):
            stale.append("parçanın çekimleri değişti")
        if override.get("input_hash") and override["input_hash"] != ih:
            stale.append("senaryo, stil, referans ya da kamera değişti")
        out.update(prompt=fal_plan.normalize_refs(engine, override["text"]), source="override",
                   override={"saved_at": override.get("saved_at"), "stale": stale, "part": override.get("part")})
        if stale:
            out["warnings"].append("Yönetmen istemi eski olabilir (" + "; ".join(stale) + "): gözden geçirin.")
        return out
    if mode() != "llm":
        return out
    hit = cached(s, ih)
    if hit is not None:
        out["llm"] = hit.view()
        if hit.prompt:
            out.update(prompt=fal_plan.normalize_refs(engine, hit.prompt), source="llm")
            n = len(hit.prompt)
            if not TARGET_CHARS[0] <= n <= TARGET_CHARS[1]:
                out["warnings"].append(f"LLM istemi {n} karakter (hedef {TARGET_CHARS[0]}–{TARGET_CHARS[1]}).")
        elif hit.call_failed:
            out["warnings"].append(f"LLM yazımı başarısız: {hit.errors[0][len(CALL_FAILED):]} — kurallar istemi "
                                   "kullanıldı.")
        else:
            out["warnings"].append("LLM istemi iki denemede doğrulamadan geçmedi; kurallar istemi kullanılıyor (" +
                                   "; ".join(hit.errors[:4]) + ").")
        return out
    tokens_in, tokens_out, usd = estimate_usd(engine, inp)
    out["estimate"] = {"input_tokens": tokens_in, "output_tokens": tokens_out, "usd": usd}
    out["writing"] = writing(ih)
    why = unavailable()
    out["warnings"].append(why or ("LLM istemi yazılıyor: bitince plan güncellenir; onaylamadan önce bekleyin."
                                   if out["writing"] else
                                   "LLM istemi henüz yazılmadı: kurallar istemi gösteriliyor (\"İstemi LLM ile yaz\")."))
    return out


# ---------- kuyruk ----------

WRITING = "hg:prompt_writing:{}"
WRITING_TTL_S = 600


def writing(ih: str) -> bool:
    from app.budget import killswitch
    return bool(killswitch.r().exists(WRITING.format(ih)))


def needed(plan) -> bool:
    """Plan kurallar istemiyle ve LLM istemi hiç yazılmamış (önbellekte yok), yazıcı çağrılabilir."""
    import time
    meta = plan.prompt_meta or {}
    llm = meta.get("llm")
    retry = bool(llm and llm.get("failed") == "call" and time.time() - float(llm.get("at") or 0) > CALL_RETRY_S)
    return plan.prompt_source == "rules" and mode() == "llm" and unavailable() is None and (not llm or retry)


def queue(project_id: str, scene_id: str, plan, force: bool = False) -> bool:
    """Arka planda yazdırır (aynı girdi için tek görev). True: kuyruğa verildi."""
    from app.budget import killswitch
    ih = (plan.prompt_meta or {}).get("input_hash") or input_hash(plan.writer_input)
    if not killswitch.r().set(WRITING.format(ih), "1", nx=True, ex=WRITING_TTL_S):
        return False
    from app.worker.tasks import write_prompt
    write_prompt.delay(project_id, scene_id, force)
    return True


def full_error(e: BaseException) -> str:
    """Olay için hatanın tam metni (anahtarlar maskeli)."""
    from app.live import redact
    return redact(f"{type(e).__name__}: {e}")[:2000]
