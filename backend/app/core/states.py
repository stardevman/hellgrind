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
