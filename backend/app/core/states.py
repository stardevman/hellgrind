"""Karakter durumu (ağ yok): çekimin durumu ("eyes dark", "Bolt is switched off") hangi karakterlere uygulanır, hangi durum
referansı verilir ve kare hangi metin sondalarıyla doğrulanır.

Kıvılcım tam koşusu: "Exception for this shot…" istemin sonunda ve negatifte "glowing eyes" olmasına rağmen Qwen-Image-Edit
referans görselini (gözleri yanık Bolt stili) izledi. Çözüm üç katmanlı:
1. Durum referansı: varlığa durum başına görsel (state_off / state_on). Çekim o durumdaysa karakterin kimlik referansı
   olarak o görsel verilir.
2. Otomatik durum referansı: elle yüklenmemişse, sekansta aynı durumda olan ve durum sondasıyla doğrulanmış önceki karenin
   karakter kırpımı (ör. SH03'ün başlangıç karesi, gözler karanlık).
3. Durum kapısı: karakter kırpımında OpenCLIP sıfır-örnek sondaları ("… with dark, unlit eyes" / "… with glowing blue eyes");
   istenen durum kaybederse kare yeniden çizilir, son denemede "inceleme önerilir: durum (eyes dark) tutmadı".
"""
import re

STATE_KINDS = ("off", "on")                  # varlık referansı: references["state_off"] / references["state_on"]
UNLIT = {"dark", "unlit", "off", "switched off", "turned off", "powered off", "lifeless"}
LIT = {"lit", "glowing"}
BODY_DEFAULT = "eyes"                        # karakterin kendisi kapalıysa ("Bolt is switched off") sonda gözlere bakar


def state_key(states) -> str | None:
    """[(isim, durum)] -> "off" | "on" | None (yalnızca açık/kapalı durumlar referans ve sonda alır)."""
    found = {s for _, s in states or ()}
    if found & UNLIT:
        return "off"
    if found & LIT:
        return "on"
    return None


def targets(states, chars) -> list[str]:
    """Durumun uygulandığı karakterler: adı durumun öznesi olanlar ("Bolt is switched off"); yoksa (ör. "eyes dark")
    çekimdeki insan olmayan karakterler (robot, hayvan)."""
    from app.core.h3mode import is_human
    nouns = {n for n, _ in states or ()}
    named = [ch.name for ch in chars if ch.name.lower() in nouns]
    return named or [ch.name for ch in chars if not is_human(ch)]


def parts_of(states, names=()) -> list[str]:
    """Durumun parçaları (karakter adı olmayan isimler): [("bolt", "switched off"), ("eyes", "dark")] -> ["eyes"]."""
    lowered = {n.lower() for n in names}
    return list(dict.fromkeys(n for n, _ in states or () if n not in lowered)) or [BODY_DEFAULT]


def probe_label(states) -> str:
    """Panel ve not için: "eyes dark"."""
    return ", ".join(dict.fromkeys(f"{n} {s}" for n, s in states or () if s in UNLIT | LIT))


def probe_texts(states, key: str, subject: str, colors=(), names=()) -> tuple[str, str]:
    """(istenen, karşıt) sonda metinleri: ("a photo of small copper robot with dark, unlit eyes",
    "a photo of small copper robot with glowing blue eyes")."""
    parts = [n for n, s in states or () if (s in UNLIT if key == "off" else s in LIT) and n not in names]
    part = parts[0] if parts else BODY_DEFAULT
    glow = f"glowing {' '.join(colors[:1])} {part}".replace("  ", " ") if colors else f"glowing {part}"
    dark = f"dark, unlit {part}"
    wanted, opposite = (dark, glow) if key == "off" else (glow, dark)
    return f"a photo of {subject} with {wanted}", f"a photo of {subject} with {opposite}"


def subject_of(asset, ch) -> str:
    """Sondadaki özne: varlığın tarifinin başı ("small copper robot"); yoksa "a robot" ya da "a person"."""
    from app.core.h3mode import is_human
    text = " ".join(((getattr(asset, "description", None) or "").split(",")[0]).split()[:6])
    return text or ("a person" if is_human(ch) else "a robot")


# ---------- olaylar ve kare durumu ----------

# Durumu değiştiren olaylar (çekimin açıklamasında): "Bolt's eyes flicker and light up blue" -> açılır
TURN_ON = re.compile(r"\b(?:light(?:s|ed)?\s+up|flicker(?:s|ed)?(?:\s+(?:on|to\s+life))?|switch(?:es|ed)?\s+on|"
                     r"turn(?:s|ed)?\s+on|power(?:s|ed)?\s+(?:on|up)|boot(?:s|ed)?\s+up|wake(?:s)?\s+up|"
                     r"come(?:s)?\s+(?:alive|to\s+life)|blink(?:s|ed)?\s+on)\b", re.IGNORECASE)
TURN_OFF = re.compile(r"\b(?:switch(?:es|ed)?\s+off|turn(?:s|ed)?\s+off|power(?:s|ed)?\s+(?:off|down)|shut(?:s)?\s+down|"
                      r"go(?:es)?\s+dark|fade(?:s|d)?\s+out)\b", re.IGNORECASE)
PARTS = ("eyes", "eye", "lights", "light", "screen", "lamp", "bulb")


def events(text: str | None) -> list[tuple[str, str]]:
    """[(isim, "on" | "off")]: çekimde durum değişiyor mu. İsim olaya en yakın parça ("eyes"), yoksa gözler."""
    out = []
    for pattern, kind in ((TURN_ON, "on"), (TURN_OFF, "off")):
        for m in pattern.finditer(text or ""):
            before = (text or "")[max(0, m.start() - 40):m.start()].lower()
            if re.search(r"\b(?:is|are|was|were|still|remains?|stays?|kept)\s*$", before):
                continue                       # "Bolt is still switched off": durum, olay değil
            noun = next((p for p in PARTS if re.search(rf"\b{p}\b", before)), BODY_DEFAULT)
            out.append(("eyes" if noun == "eye" else noun, kind))
    return list(dict.fromkeys(out))


def frame_states(states, evs, frame: str | None) -> list[tuple[str, str]]:
    """Karenin durumu. Başlangıç karesi (frame="start"): yazılı durum, yoksa olaydan önceki durum (SH05 "eyes flicker and
    light up": gözler henüz karanlık). Son kare (frame="end"): olay varsa sonrası, yoksa yazılı durum. Video istemi
    (frame=None): yazılı durum."""
    states = list(states or ())
    if frame == "start" and not states and evs:
        return [(noun, "dark" if kind == "on" else "lit") for noun, kind in evs]
    if frame == "end" and evs:
        return [(noun, "lit" if kind == "on" else "dark") for noun, kind in evs]
    return states


def opposite(key: str | None) -> str | None:
    return {"off": "on", "on": "off"}.get(key)


# ---------- durumla birlikte yazılan tarif ve değişmezler ----------

GLOW_WORDS = r"(?:glow\w*|lit|light(?:s|ed|ing)?|shin\w*|luminous)"
_PAREN = re.compile(rf"\s*\([^)]*\b{GLOW_WORDS}\b[^)]*\)", re.IGNORECASE)
_CLAUSE = re.compile(rf"\s*\b(?:that|which)\s+{GLOW_WORDS}\b[^,.;]*", re.IGNORECASE)
_ADJ = re.compile(r"\b(?:glowing|luminous|lit)\s+(?:(?:soft|bright|pale|light)\s+)?(?:blue|red|green|yellow|orange|"
                  r"purple|pink|white|amber|cyan|golden)?\s*", re.IGNORECASE)


def state_text(text: str, key: str | None, nouns=()) -> str:
    """"off" durumunda ışıktan söz eden ifadeler çıkarılır ve durum parçası durumla yazılır: "round glass eyes (glow blue
    only when switched on)" -> "round glass eyes (dark and unlit)"; "big round glass eyes that glow soft blue when on" ->
    "big round glass eyes (dark and unlit)". Diğer durumlarda metin değişmez."""
    if key != "off" or not text:
        return text
    out = _ADJ.sub("", _CLAUSE.sub("", _PAREN.sub("", text)))
    for noun in dict.fromkeys(nouns or (BODY_DEFAULT,)):
        out = re.sub(rf"\b({re.escape(noun)})\b(?!\s*\(dark)", r"\1 (dark and unlit)", out, count=1)
    return " ".join(out.split())
