"""Ücretsiz, kural tabanlı senaryo ayrıştırıcı (Hermes'in çevrimdışı modu).

Standart senaryo biçimini okur:
  İÇ. / DIŞ. / INT. / EXT. ile başlayan satır  -> yeni sahne
  BÜYÜK HARFLİ kısa satır                      -> konuşan karakter
  (parantez içi)                               -> oyunculuk notu / duygu
  diğer paragraflar                            -> aksiyon (yeni çekim)
Çıktı, LLM modunun ürettiği şemayla aynıdır; böylece ikisi birbirinin yerine geçebilir.
Bu modül hiçbir dış servise ve yapılandırmaya bağlı değildir.
"""
import re

SLUG = re.compile(r"^(İÇ\.|DIŞ\.|İÇ/DIŞ\.|INT\.|EXT\.|INT/EXT\.)", re.IGNORECASE)
TRANSITION = re.compile(r"^(KESME|GEÇİŞ|CUT TO|FADE|KARARMA|AÇILIŞ)\b.*:?$")
ACTION_WORDS = ["koş", "kaç", "atla", "atlar", "patla", "düş", "vur", "çarp", "dövüş", "yumruk", "fırla",
                "savrul", "çök", "yıkıl", "ateş", "kovala", "tırman", "sıçra", "sarsıl", "kop", "parçalan"]
EMOTION_WORDS = {
    "fear": ["kork", "dehşet", "titre", "panik", "ürk"],
    "anger": ["öfke", "sinir", "bağır", "hiddet", "haykır", "kızgın"],
    "joy": ["gül", "sevin", "neşe", "mutlu", "coşku"],
    "sadness": ["üzgün", "hüzün", "ağla", "gözyaş", "keder", "yas tut", "yas içinde"],   # çıplak "yas", "yasak"ı da yakalardı
    "surprise": ["şaşkın", "şaşır", "hayret", "irkil"],
    "disgust": ["tiksin", "iğren", "midesi bulan"],
    "tension": ["gergin", "tedirgin", "sessizce", "fısılda", "temkin"],
}
WPS = 2.5   # konuşma hızı (kelime/sn)


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def tr_title(s: str) -> str:
    return " ".join(w[:1] + tr_lower(w[1:]) for w in s.split())


def is_cue(line: str) -> bool:
    core = re.sub(r"\(.*?\)", "", line).strip()
    if not core or SLUG.match(core) or TRANSITION.match(core):
        return False
    letters = [c for c in core if c.isalpha()]
    return bool(letters) and core == core.upper() and len(core.split()) <= 4 and len(core) <= 40


def _mask_names(low: str, names) -> str:
    """Kadro adlarını (ve eklerini) çıkarır; "Gül" ya da "Ateş" adlı karakter duygu/aksiyon kökü sayılmasın."""
    for n in names:
        low = re.sub(r"(?<!\w)" + re.escape(tr_lower(n)) + r"\w*", " ", low)
    return low


def emotion_of(*texts: str, default: str = "neutral", names: list[str] = ()) -> str:
    """Kökler yalnızca kelime başında aranır ("Türkiye" içindeki "ürk" korku sayılmaz)."""
    blob = _mask_names(tr_lower(" ".join(t for t in texts if t)), names)
    for emo, stems in EMOTION_WORDS.items():
        if any(re.search(r"(?<!\w)" + re.escape(st), blob) for st in stems):
            return emo
    return default


def _mentions(text: str, cast: list[str]) -> list[str]:
    low = tr_lower(text)
    return [c for c in cast if re.search(r"(?<!\w)" + re.escape(tr_lower(c)), low)]


def _is_action(text: str, names: list[str] = ()) -> bool:
    low = _mask_names(tr_lower(text), names)
    return any(re.search(r"(?<!\w)" + w, low) for w in ACTION_WORDS)


def _dialogue_seconds(lines: list[dict]) -> float:
    return sum(len(d["line"].split()) / WPS + 0.35 for d in lines)


def rules_cast(text: str) -> list[str]:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    cast_order: list[str] = []
    for i, ln in enumerate(lines):          # ön tarama: kadro
        s = ln.strip()
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
        if is_cue(s) and nxt and not SLUG.match(nxt):
            name = tr_title(re.sub(r"\(.*?\)", "", s).strip())
            if name not in cast_order:
                cast_order.append(name)
    return cast_order


def parse(text: str) -> dict:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    cast_order = rules_cast(text)

    scenes: list[dict] = []

    def cur_scene():
        if not scenes:
            scenes.append({"heading": "SAHNE", "shots": []})
        return scenes[-1]

    def new_shot(sc, description, chars, emotion, kind):
        shot = {"description": description, "characters": list(dict.fromkeys(chars)), "dialogue": [],
                "emotion": emotion, "kind": kind}
        sc["shots"].append(shot)
        return shot

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or TRANSITION.match(s):
            i += 1
            continue
        if SLUG.match(s):
            scenes.append({"heading": s, "shots": []})
            i += 1
            continue
        sc = cur_scene()
        if is_cue(s):
            name = tr_title(re.sub(r"\(.*?\)", "", s).strip())
            i += 1
            paren, said = None, []
            while i < len(lines) and lines[i].strip():
                l = lines[i].strip()
                if l.startswith("(") and l.endswith(")"):
                    paren = l[1:-1]
                else:
                    said.append(l)
                i += 1
            if not said:
                continue
            last_emo = sc["shots"][-1]["emotion"] if sc["shots"] else "neutral"
            line = {"character": name, "line": " ".join(said),
                    "emotion": emotion_of(paren or "", " ".join(said), default=last_emo, names=cast_order)}
            last = sc["shots"][-1] if sc["shots"] else None
            fits = (last is not None and name in last["characters"] and len(last["dialogue"]) < 2
                    and _dialogue_seconds(last["dialogue"] + [line]) <= 8.5)
            if fits:
                last["dialogue"].append(line)
                if paren:
                    last["emotion"] = line["emotion"]
            else:
                shot = new_shot(sc, f"{name} konuşuyor: {line['line'][:80]}", [name], line["emotion"], "dialogue")
                shot["dialogue"].append(line)
            continue
        para = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not is_cue(lines[i].strip()) and not SLUG.match(lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        desc = " ".join(para)
        prev = sc["shots"][-1]["emotion"] if sc["shots"] else "neutral"
        new_shot(sc, desc, _mentions(desc, cast_order), emotion_of(desc, default=prev, names=cast_order),
                 "action" if _is_action(desc, cast_order) else "establish" if not sc["shots"] else "beat")

    shots = []
    for si, sc in enumerate([x for x in scenes if x["shots"]], start=1):
        for hi, sh in enumerate(sc["shots"], start=1):
            shots.append(_finalize(si, hi, sc["heading"], sh))
    return {"cast": cast_order, "shots": shots}


def _finalize(scene_no: int, shot_no: int, heading: str, sh: dict) -> dict:
    kind, emo, chars = sh["kind"], sh["emotion"], sh["characters"]
    intense = emo in ("fear", "anger", "tension")
    if kind == "action":
        camera = {"shot": "wide", "movement": "handheld" if emo in ("anger", "fear") else "tracking", "angle": "low", "lens_mm": 24}
        complexity = 0.8
    elif kind == "establish":
        camera = {"shot": "extreme_wide" if not chars else "wide", "movement": "pan", "angle": "eye", "lens_mm": 24}
        complexity = 0.2
    elif kind == "dialogue":
        camera = {"shot": "close_up", "movement": "dolly_in" if intense else "static", "angle": "eye", "lens_mm": 50}
        complexity = 0.1
    else:
        camera = {"shot": "medium", "movement": "dolly_in" if intense else "static", "angle": "eye", "lens_mm": 35}
        complexity = 0.3
    action_s = min(8.0, max(3.0, len(sh["description"].split()) / 4))
    dlg_s = _dialogue_seconds(sh["dialogue"]) + 1.0 if sh["dialogue"] else 0
    duration = round(min(10.0, max(3.0, action_s if kind != "dialogue" else 0, dlg_s)), 1)
    return {
        "scene_id": f"S{scene_no:03d}_SH{shot_no:02d}",
        "heading": heading,
        "description": sh["description"],
        "visual_prompt": sh["description"],
        "camera": camera,
        "emotion": emo,
        "characters": chars,
        "dialogue": sh["dialogue"],
        "duration_s": duration,
        "action_complexity": complexity,
    }


SAMPLE_SCRIPT = """İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE

Kızıl bir ışık paslı kazanların arasından süzülüyor. Kerem yavaşça içeri girer.

KEREM
(tedirgin)
Burada kimse yok mu?

Ayşe gölgelerden çıkar, elinde titreyen bir fener var.

AYŞE
Sesini alçalt. Makineler hâlâ dinliyor.

DIŞ. ÇÖKEN KÖPRÜ - ŞAFAK

Köprü sarsılır. Kerem ve Ayşe demir kirişlerin üzerinde koşar, arkalarında çelik halatlar kopar.

KEREM
(korkuyla)
Atla! Şimdi!

Ayşe karşı kenara atlar ve Kerem'in elini yakalar.

AYŞE
(öfkeyle)
Bırakmayacağım seni!
"""
