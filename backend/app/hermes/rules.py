"""Hermes çevrimdışı mod: ücretsiz, kural tabanlı senaryo ayrıştırıcı.

Standart senaryo biçimini okur:
  İÇ. / DIŞ. / İÇ/DIŞ. / INT. / EXT. ile başlayan satır -> yeni sahne
  BÜYÜK HARFLİ kısa satır (hemen ardından replik)      -> konuşan karakter, uzantılar (D.S.) atılır
  (parantez içi satır)                                  -> duygu ipucu
  diğer paragraflar                                     -> aksiyon, her biri yeni bir çekim
  [NESNE: Taksi] işareti                                -> nesne varlığı; paragraf içindeyse o çekime, tek başına
                                                           bir satırdaysa sahnenin tüm çekimlerine bağlanır
  [KARAKTER: Kerem] işareti                             -> konuşmayan karakter: kadroya girer ve aynı kuralla çekime
                                                           bağlanır; adı geçen sonraki çekimlere de bağlanır
                                                           (İngilizce: [PROP: Taxi], [CHARACTER: Kerem])
  # SEKANS: Havalimanı                                  -> yeni sekans; sonraki sahne başlıkları ona aittir (yoksa
                                                           bütün sahneler tek sekanstadır)
Mekân, sahne başlığından çıkar ("İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE" -> "Terk Edilmiş Dökümhane").
Konuşan kişiler ve işaretlenen karakterler kadroyu oluşturur; adı geçen her çekime bağlanırlar. Senaryo İngilizce de
yazılabilir (INT./EXT., İngilizce duygu ve aksiyon kelimeleri); Türkçe harf içermeyen senaryoda adlar İngilizce
kurallarla büyük harfe çevrilir ("IRIS" -> "Iris", Türkçede "Irıs" olurdu).

Çıktı, LLM modunun üreteceği şemayla aynıdır. Bu modül hiçbir dış servise ve yapılandırmaya bağlı değildir.
"""
import math
import re
from contextvars import ContextVar

WPS = 2.5                 # konuşma hızı (kelime/sn)
LINE_GAP_S = 0.35         # replikler arası boşluk
LEAD_IN_S = 1.0           # diyaloglu çekimde konuşma öncesi ve sonrası toplam pay
MAX_DIALOGUE_S = 8.5      # bir çekime bağlanan diyalogun üst sınırı
MAX_LINES_PER_SHOT = 2
MIN_DURATION_S, MAX_DURATION_S = 3.0, 10.0
ACTION_WPS = 4.0          # aksiyon paragrafı için okuma hızı (kelime/sn), süre tahmini
MAX_CUE_WORDS, MAX_CUE_CHARS = 4, 40

INTENSE = ("fear", "anger", "tension")

# Kök eşleştirme yalnızca kelime başında yapılır ("Türkiye" içindeki "ürk" korku sayılmaz).
# Çıplak "yas" gibi kısa kökler "yasak"ı da yakalayacağından eklenmedi.
EMOTION_STEMS = {
    "fear": ["kork", "dehşet", "panik", "ürk"],
    "anger": ["öfke", "sinir", "bağır", "hiddet", "haykır", "kızgın"],
    "joy": ["gül", "sevin", "neşe", "mutlu", "coşku", "kahkaha"],
    "sadness": ["üzgün", "üzül", "hüzün", "ağla", "gözyaş", "keder", "yas tut", "yas içinde"],
    "surprise": ["şaşk", "şaşır", "hayret", "irkil", "afalla"],
    "disgust": ["tiksin", "iğren", "midesi bulan"],
    "tension": ["gergin", "tedirgin", "sessizce", "fısılda", "temkin", "titre", "huzursuz", "endişe"],
}
# İngilizce senaryolar için düzenli ifadeler (kelime başında eşleşir; "sad" Türkçe "sadece"yi yakalamasın diye tam kelime).
EMOTION_PATTERNS_EN = {
    "fear": [r"fear", r"afraid", r"terrif", r"scared", r"scream", r"horrif", r"frighten", r"panic"],
    "anger": [r"angr", r"anger", r"furious", r"fury\b", r"rag(?:e|es|ing)\b", r"shout", r"yell", r"snarl"],
    "joy": [r"joy", r"happ(?:y|ily|iness)\b", r"laugh", r"smil", r"cheer", r"delight", r"grin(?:s|ning)?\b"],
    "sadness": [r"sad(?:ly|ness)?\b", r"cr(?:y|ies|ied|ying)\b", r"tears\b", r"weep", r"griev", r"grief", r"mourn", r"sob(?:s|bing)?\b"],
    "surprise": [r"surpris", r"shock", r"astonish", r"gasp", r"stunned", r"startl"],
    "disgust": [r"disgust", r"revolt", r"grimac"],
    "tension": [r"tense\b", r"nervous", r"uneas", r"anxious", r"whisper", r"cautious", r"war(?:y|ily)\b", r"trembl", r"shiver"],
}

# Aksiyon kökleri; negatif bakışlar sık yanlış eşleşmeleri eler (düşünmek, düşman, koşul, atlas, kopya...).
ACTION_STEMS = [
    r"koş(?!ul|um)", r"kaç", r"atla(?!s|t)", r"patla", r"düş(?!ün|man|ük)", r"vur", r"çarp", r"dövüş", r"yumruk",
    r"fırla", r"savrul", r"çök", r"yıkıl", r"ateş", r"kovala", r"tırman", r"sıçra", r"sarsıl", r"kop(?!ya)",
    r"parçalan",
    # İngilizce
    r"run(?:s|ning)?\b", r"ran\b", r"jump", r"leap", r"explo(?:de|des|ded|ding|sion)", r"fall(?:s|ing)?\b", r"fell\b",
    r"crash", r"punch", r"fight", r"fought\b", r"chas(?:e|es|ed|ing)\b", r"climb", r"collaps", r"shatter", r"flee",
    r"fled\b", r"sprint", r"dodg", r"tumbl", r"smash", r"shak(?:e|es|ing)\b", r"shook\b",
]

TRANSITION_WORDS = ("kesme", "geçiş", "kararma", "açılış", "cut to", "fade", "dissolve")
SLUG_TR = ("iç/dış.", "iç.", "dış.")
SLUG_EN = ("int/ext.", "int.", "ext.")
PAREN = re.compile(r"\([^)]*\)")
PROP_MARKER = re.compile(r"\[\s*(?:NESNE|PROP)\s*:\s*([^\]]*)\]", re.IGNORECASE)
CHARACTER_MARKER = re.compile(r"\[\s*(?:KARAKTER|CHARACTER)\s*:\s*([^\]]*)\]", re.IGNORECASE)
TR_LETTERS = set("çğıöşüÇĞİÖŞÜ")
_turkish: ContextVar[bool | None] = ContextVar("turkish_script", default=None)
TIME_OF_DAY_SEP = re.compile(r"\s+[-–—]\s+")
SEQUENCE_MARKER = re.compile(r"^#\s*(?:SEKANS|SEQUENCE)\s*:\s*(.*?)\s*$", re.IGNORECASE)


# ---------- Türkçe büyük/küçük harf ----------

def tr_lower(s: str) -> str:
    """Python'un lower()'ı I -> i ve İ -> i̇ (birleşik nokta) yapar; Türkçe I -> ı ve İ -> i olmalı."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def tr_upper(s: str) -> str:
    return s.replace("i", "İ").replace("ı", "I").upper()


def tr_title(s: str) -> str:
    """"AYŞE" -> "Ayşe", "IŞIL" -> "Işıl", "İBRAHİM" -> "İbrahim"."""
    return re.sub(r"[^\s'’\-]+", lambda m: tr_upper(m.group()[:1]) + tr_lower(m.group()[1:]), s)


def en_title(s: str) -> str:
    """"IRIS" -> "Iris" (tr_title "Irıs" yapardı)."""
    return re.sub(r"[^\s'’\-]+", lambda m: m.group()[:1].upper() + m.group()[1:].lower(), s)


def is_turkish_script(text: str) -> bool:
    """Türkçe harf ya da Türkçe sahne başlığı varsa Türkçe; yoksa İngilizce kabul edilir."""
    return bool(TR_LETTERS & set(text)) or any(_slug_prefix(line) in SLUG_TR for line in text.splitlines())


def name_title(s: str) -> str:
    """Ad biçimi: ayrıştırılan senaryonun diline göre; bağlam yoksa Türkçe kurallar (eski davranış)."""
    return en_title(s) if _turkish.get() is False else tr_title(s)


# ---------- satır sınıflandırma ----------

def _slug_prefix(line: str) -> str | None:
    s = line.strip()
    for prefix in SLUG_TR:
        if tr_lower(s).startswith(prefix):
            return prefix
    for prefix in SLUG_EN:
        if s.lower().startswith(prefix):
            return prefix
    return None


def is_slug(line: str) -> bool:
    return _slug_prefix(line) is not None


def sequence_title(line: str) -> str | None:
    """"# SEKANS: Havalimanı" -> "Havalimanı"; sekans satırı değilse None (başlıksız satır boş dize döner)."""
    m = SEQUENCE_MARKER.match(line.strip())
    return " ".join(m.group(1).split()) if m else None


def _breaks(line: str) -> bool:
    """Paragrafı ve repliği bitiren satır: sahne başlığı ya da sekans satırı."""
    return is_slug(line) or sequence_title(line) is not None


def _has_lowercase(s: str) -> bool:
    return any(c.islower() for c in s if c.isalpha())


def is_transition(line: str) -> bool:
    s = line.strip()
    return bool(s) and not _has_lowercase(s) and (s.endswith(":") or tr_lower(s).startswith(TRANSITION_WORDS))


def is_cue(line: str) -> bool:
    """Konuşan karakter satırı: büyük harfli, en fazla 4 kelime ve 40 karakter."""
    name = PAREN.sub("", line).strip()
    if not name or is_slug(name) or is_transition(name):
        return False
    if not any(c.isalpha() for c in name) or _has_lowercase(name):
        return False
    if name[-1] in "!?:,;…" or "!" in name or "?" in name:      # "KOŞ!" bir bağırma, karakter adı değil
        return False
    return len(name.split()) <= MAX_CUE_WORDS and len(name) <= MAX_CUE_CHARS


def _starts_dialogue(lines: list[str], i: int) -> bool:
    """Karakter satırı ancak hemen ardından (boş satırsız) bir metin gelirse konuşmadır."""
    return is_cue(lines[i]) and i + 1 < len(lines) and bool(lines[i + 1]) and not is_slug(lines[i + 1])


def cue_name(line: str) -> str:
    return name_title(PAREN.sub("", line).strip())


# ---------- duygu ve aksiyon ----------

def _mask_names(low: str, names) -> str:
    """Kadro adlarını (ve eklerini) siler: "Gül" ya da "Ateş" adlı karakter duygu/aksiyon kökü sayılmasın."""
    for n in names:
        low = re.sub(r"(?<!\w)" + re.escape(tr_lower(n)) + r"\w*", " ", low)
    return low


def emotion_of(*texts: str, default: str = "neutral", names=()) -> str:
    """En çok kök eşleşen duygu; eşitlikte sözlük sırası. Hiç eşleşme yoksa `default`."""
    raw = " ".join(t for t in texts if t)
    blob = _mask_names(tr_lower(raw), names)
    english = _mask_names(raw.lower(), names)              # İngilizce kalıplar Türkçe küçültmeyle (I -> ı) bozulmasın
    best, best_hits = default, 0
    for emotion, stems in EMOTION_STEMS.items():
        hits = sum(len(re.findall(r"(?<!\w)" + re.escape(stem), blob)) for stem in stems)
        hits += sum(len(re.findall(r"(?<!\w)" + pattern, english)) for pattern in EMOTION_PATTERNS_EN[emotion])
        if hits > best_hits:
            best, best_hits = emotion, hits
    return best


def is_action(text: str, names=()) -> bool:
    lows = (_mask_names(tr_lower(text), names), _mask_names(text.lower(), names))
    return any(re.search(r"(?<!\w)" + stem, low) for stem in ACTION_STEMS for low in lows)


def _marked(pattern: re.Pattern, text: str) -> list[str]:
    names: list[str] = []
    for m in pattern.finditer(text):
        for raw in m.group(1).split(","):
            name = name_title(" ".join(raw.split()))
            if name and name not in names:
                names.append(name)
    return names


def take_marks(text: str) -> tuple[str, list[str], list[str]]:
    """[NESNE: ...] ve [KARAKTER: ...] işaretlerini metinden çıkarır: (temiz metin, nesneler, karakterler)."""
    props, chars = _marked(PROP_MARKER, text), _marked(CHARACTER_MARKER, text)
    stripped = CHARACTER_MARKER.sub(" ", PROP_MARKER.sub(" ", text))
    clean = re.sub(r"\s+([.,;:!?…])", r"\1", re.sub(r"\s{2,}", " ", stripped)).strip()
    return clean, props, chars


def take_props(text: str) -> tuple[str, list[str]]:
    """[NESNE: Taksi, Bavul] işaretlerini metinden çıkarır: (temiz metin, nesne adları). Adlar başlık biçimindedir,
    böylece "taksi" ile "Taksi" aynı varlık olur. Karakter işaretleri de metinden atılır."""
    clean, props, _ = take_marks(text)
    return clean, props


def location_name(heading: str) -> str | None:
    """Sahne başlığındaki mekân, gün zamanı eki atılarak: "DIŞ. ÇÖKEN KÖPRÜ - ŞAFAK" -> "Çöken Köprü". Başlık yoksa None."""
    if not is_slug(heading):
        return None
    place = TIME_OF_DAY_SEP.split(_location(heading))[0].strip(" -–—.")
    if not place:
        return None
    english = _turkish.get() is False or (_turkish.get() is None and _slug_prefix(heading) in SLUG_EN
                                          and not TR_LETTERS & set(heading))
    return en_title(place) if english else tr_title(place)


def mentions(text: str, cast: list[str]) -> list[str]:
    low = tr_lower(text)
    return [c for c in cast if re.search(r"(?<!\w)" + re.escape(tr_lower(c)), low)]


# ---------- süre ----------

def dialogue_seconds(lines: list[dict]) -> float:
    return sum(len(d["line"].split()) / WPS + LINE_GAP_S for d in lines)


def _round_up_half(x: float) -> float:
    return math.ceil(x * 2) / 2


def shot_duration(description: str, dialogue: list[dict], has_action_text: bool) -> float:
    """Konuşma süresi (kelime / 2,5 + boşluklar) ve aksiyon okuma süresinin büyüğü; 0,5 sn'ye yuvarlanır ve
    3-10 sn arasına sıkıştırılır. Yuvarlama, küçük replik düzeltmelerinin görüntüyü yeniden üretmesini önler."""
    spoken = dialogue_seconds(dialogue) + LEAD_IN_S if dialogue else 0.0
    acted = min(8.0, len(description.split()) / ACTION_WPS) if has_action_text else 0.0
    return min(MAX_DURATION_S, max(MIN_DURATION_S, _round_up_half(max(spoken, acted))))


# ---------- 1. geçiş: satırlardan bloklara ----------

def _split_long_speech(text: str) -> list[str]:
    """Tek başına çekim süresini aşan konuşma, cümle sınırlarından bölünür; yoksa sesi klipten uzun olup kesilirdi."""
    if len(text.split()) / WPS + LINE_GAP_S <= MAX_DIALOGUE_S:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate.split()) / WPS + LINE_GAP_S > MAX_DIALOGUE_S:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    return parts + [current] if current else parts


def tokenize(text: str) -> list[dict]:
    """Bloklar: {"type": "slug", "heading"} | {"type": "sequence", "title"} | {"type": "speech", "name", "hint", "text"}
    | {"type": "action", "text"}."""
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        if (title := sequence_title(line)) is not None:      # "# SEKANS:" geçiş satırı sanılmasın: önce bakılır
            blocks.append({"type": "sequence", "title": title})
            i += 1
        elif not line or is_transition(line):
            i += 1
        elif is_slug(line):
            blocks.append({"type": "slug", "heading": line})
            i += 1
        elif _starts_dialogue(lines, i):
            hints = PAREN.findall(line)
            i += 1
            said = []
            while i < len(lines) and lines[i] and not _breaks(lines[i]):
                if lines[i].startswith("(") and lines[i].endswith(")"):
                    hints.append(lines[i])
                else:
                    said.append(lines[i])
                i += 1
            if said:
                blocks.append({"type": "speech", "name": cue_name(line), "hint": " ".join(hints), "text": " ".join(said)})
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i] and not _breaks(lines[i]) and not _starts_dialogue(lines, i):
                paragraph.append(lines[i])
                i += 1
            blocks.append({"type": "action", "text": " ".join(paragraph)})
    return blocks


# ---------- 2. geçiş: bloklardan çekimlere ----------

def _build_scenes(blocks: list[dict], cast: list[str]) -> list[dict]:
    """Çekimleri olan sahneler: [{heading, sequence, props, shots: [{description, characters, dialogue, emotion, kind,
    props}]}]. Sahnenin `props` listesi, tek başına bir satırda duran nesne işaretleridir (sahnenin tüm çekimlerine
    uygulanır). `sequence`: (sıra, başlık); sahne, başlığı okunduğu anda geçerli olan sekansa aittir."""
    scenes: list[dict] = []
    sequence = (0, None)

    def scene():
        if not scenes:
            scenes.append({"heading": "SAHNE", "sequence": sequence, "props": [], "characters": [], "shots": []})
        return scenes[-1]

    for block in blocks:
        if block["type"] == "sequence":
            sequence = (sequence[0] + 1, block["title"] or None)
            continue
        if block["type"] == "slug":
            scenes.append({"heading": block["heading"], "sequence": sequence, "props": [], "characters": [], "shots": []})
            continue
        sc = scene()
        shots = sc["shots"]
        previous_emotion = shots[-1]["emotion"] if shots else "neutral"
        text, props, marked = take_marks(block["text"])

        if block["type"] == "action":
            if not text:                                   # yalnızca işaret: sahne düzeyinde nesne ve karakter
                sc["props"] += [p for p in props if p not in sc["props"]]
                sc["characters"] += [c for c in marked if c not in sc["characters"]]
                continue
            kind = "action" if is_action(text, cast) else "establish" if not shots else "beat"
            present = marked + [c for c in mentions(text, cast) if c not in marked]
            shots.append({"description": text, "characters": present, "dialogue": [],
                          "emotion": emotion_of(text, default=previous_emotion, names=cast), "kind": kind,
                          "props": props})
            continue

        if not text:
            continue
        name = block["name"]
        emotion = emotion_of(block["hint"], text, default=previous_emotion, names=cast)
        for part in _split_long_speech(text):
            line = {"character": name, "line": part, "emotion": emotion}
            last = shots[-1] if shots else None
            fits = (last is not None and name in last["characters"] and len(last["dialogue"]) < MAX_LINES_PER_SHOT
                    and dialogue_seconds(last["dialogue"] + [line]) <= MAX_DIALOGUE_S)
            if fits:
                last["dialogue"].append(line)
                last["emotion"] = emotion
                last["props"] += [p for p in props if p not in last["props"]]
            else:
                shots.append({"description": f"{name} konuşuyor: {part[:80]}", "characters": [name],
                              "dialogue": [line], "emotion": emotion, "kind": "dialogue", "props": list(props)})
    return [sc for sc in scenes if sc["shots"]]


def _camera(kind: str, emotion: str) -> tuple[dict, float]:
    """Kamera ve aksiyon karmaşıklığı. Öncelik: aksiyon > sahnenin ilk çekimi > yalnızca diyalog > diğer."""
    intense = emotion in INTENSE
    if kind == "action":
        return {"shot": "wide", "movement": "handheld" if intense else "tracking", "angle": "low", "lens_mm": 24}, 0.8
    if kind == "establish":
        return {"shot": "wide", "movement": "pan", "angle": "eye", "lens_mm": 24}, 0.2
    if kind == "dialogue":
        return {"shot": "close_up", "movement": "dolly_in" if intense else "static", "angle": "eye", "lens_mm": 50}, 0.1
    return {"shot": "medium", "movement": "static", "angle": "eye", "lens_mm": 35}, 0.3


def _location(heading: str) -> str:
    prefix = _slug_prefix(heading)
    place = heading.strip()[len(prefix):] if prefix else heading
    return place.strip(" -–—.").strip()


def _visual_prompt(heading: str, shot: dict) -> str:
    """Yalnızca görüntüyü anlatır; replik metni girmez, böylece replik düzeltmesi görüntüyü etkilemez."""
    where = _location(heading)
    if shot["kind"] == "dialogue":
        body = f"{shot['characters'][0]} yakın planda konuşuyor"
    else:
        body = shot["description"]
    return f"{where}. {body}" if where and where != "SAHNE" else body


def camera_for(kind: str, emotion: str) -> tuple[dict, float]:
    """Çekim türü ve duygudan kamera ile aksiyon karmaşıklığı (LLM modu da aynı kuralları kullanır)."""
    return _camera(kind, emotion)


def visual_prompt_for(heading: str, shot: dict) -> str:
    return _visual_prompt(heading, shot)


def split_scenes_with_sequences(text: str) -> list[tuple[str, tuple]]:
    """Metni sahne başlıklarından parçalara böler: [(parça, sekans anahtarı)]. Her parça kendi başlığıyla başlar;
    başlıktan önceki metin ayrı bir parçadır. Sekans satırları parçalardan çıkarılır (LLM onları sahne sanmasın) ve
    parçanın ilk dolu satırında geçerli olan sekans, parçanın anahtarı olur. Yalnızca sekans satırından oluşan parça
    oluşmaz."""
    chunks, current, sequence, key = [], [], (0, None), None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if (title := sequence_title(line)) is not None:
            sequence = (sequence[0] + 1, title or None)
            continue
        if is_slug(line) and any(x.strip() for x in current):
            chunks.append(("\n".join(current), key))
            current, key = [], None
        if key is None and line.strip():
            key = sequence
        current.append(line)
    if any(x.strip() for x in current):
        chunks.append(("\n".join(current), key))
    return chunks


def split_scenes(text: str) -> list[str]:
    """Sahne parçaları (sekans satırları çıkarılmış); bkz. split_scenes_with_sequences."""
    return [chunk for chunk, _ in split_scenes_with_sequences(text)]


def number_sequences(keys: list[tuple]) -> tuple[list[str], list[dict]]:
    """Sahnelerin sekans anahtarları [(sıra, başlık)] -> (sahne başına kod, [{code, title}]). Yalnızca sahnesi olan
    sekanslar numaralanır (SEQ01, SEQ02...); başlıksız sekans "Sekans N" olur."""
    order = list(dict.fromkeys(keys))
    codes = {key: f"SEQ{n:02d}" for n, key in enumerate(order, start=1)}
    sequences = [{"code": codes[key], "title": key[1] or f"Sekans {n}"} for n, key in enumerate(order, start=1)]
    return [codes[k] for k in keys], sequences


def parse(text: str, known=()) -> dict:
    """known: projede zaten olan karakter adları; senaryoda adı geçiyorsa (konuşmasa da) kadroya girer."""
    token = _turkish.set(is_turkish_script(text))
    try:
        return _parse(text, known)
    finally:
        _turkish.reset(token)


def _parse(text: str, known) -> dict:
    blocks = tokenize(text)
    cast: list[str] = []
    for block in blocks:
        names = [block["name"]] if block["type"] == "speech" else take_marks(block.get("text", ""))[2]
        cast += [n for n in names if n not in cast]
    body = " ".join(b.get("text", "") for b in blocks if b["type"] in ("action", "speech"))
    cast += [n for n in mentions(body, [k for k in known if k not in cast])]

    shots = []
    scenes = _build_scenes(blocks, cast)
    codes, sequences = number_sequences([sc["sequence"] for sc in scenes])
    for scene_no, (sc, sequence) in enumerate(zip(scenes, codes), start=1):
        location = location_name(sc["heading"])
        for shot_no, shot in enumerate(sc["shots"], start=1):
            shot["characters"] += [c for c in sc["characters"] if c not in shot["characters"]]
            camera, complexity = _camera(shot["kind"], shot["emotion"])
            shots.append({
                "scene_id": f"S{scene_no:03d}_SH{shot_no:02d}",
                "sequence": sequence,
                "heading": sc["heading"],
                "location": location,
                "props": shot["props"] + [p for p in sc["props"] if p not in shot["props"]],
                "description": shot["description"],
                "visual_prompt": _visual_prompt(sc["heading"], shot),
                "camera": camera,
                "emotion": shot["emotion"],
                "characters": shot["characters"],
                "dialogue": shot["dialogue"],
                "duration_s": shot_duration(shot["description"], shot["dialogue"], shot["kind"] != "dialogue"),
                "action_complexity": complexity,
            })
    return {"cast": cast, "sequences": sequences, "shots": shots}


SAMPLE_SCRIPT = """# SEKANS: Dökümhane

İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE

Kızıl bir ışık paslı kazanların arasından süzülüyor. Kerem yavaşça içeri girer.

KEREM
(tedirgin)
Burada kimse yok mu?

Ayşe gölgelerden çıkar, elinde titreyen bir fener var.

AYŞE
Sesini alçalt. Makineler hâlâ dinliyor.

# SEKANS: Köprü

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
