"""Yönetmenlik kuralları ve film düzeyinde yönetmen kontrolü (ağ yok, ücretsiz).

Kaynak: lemo-opuscar `DIRECTOR.md` (LemoLab, MIT, https://github.com/lemomo-ai/lemo-opuscar). Oradaki yönetmenlik
yöntemi, kodla çizilen kısa filmler için yazılmıştır; burada yalnızca üretken videoya ve Hell Grind'in kurallarına uyan
kısımlar alındı ve uyarlandı (videoda konuşma yok, olay uydurulmaz, çekim listesi senaryodan gelir).

Üç kullanım:
- `WRITER_RULES`: sahne istemi yazıcısının (prompt_writer) sistem istemine eklenen sahneleme ve oyunculuk kuralları.
- `ASSISTANT_GUIDE`: panel asistanının sistem istemine eklenen "yönetmen gözüyle inceleme" yöntemi.
- `review(shots)`: filmin çekim listesi üzerinde deterministik kontroller (kamera ve çerçeve çeşitliliği, tempo, nefes
  çekimi, açılış). Bulgular Türkçe; asistanın `directing_review` aracı ve `GET /projects/{id}/directing-review` döner.
"""
from statistics import mean, median, pstdev

# ---------- istem yazıcısı ----------

WRITER_RULES = """Directing craft (apply inside the structure above; never add events that are not in the script):
- Every camera move has a reason: it reveals, follows or emphasises something in the script. If nothing motivates a \
move, keep the camera still rather than drifting.
- Key moments read at a glance: the acting character fills at least a third of the frame height, with a clear \
silhouette, not pushed against the frame edge, and contrasts with the background (no colour on the same colour, not \
too dark to read).
- Performance: every meaningful move has anticipation, the action and follow-through. Characters show a change of \
emotion within the shot through face and body; they do not just slide across the frame.
- One clear action per beat. After a gag, a surprise or a key action, hold a short beat so the viewer can read it \
before the next action starts.
- Keep staging continuous across shots: same screen side for each character, consistent eyelines and directions of \
movement."""

WRITER_SOUND_RULE = """- Sound: every visible action has a sound that matches its material (paper, wood, fabric, \
metal, glass); keep a near-silence just before the turn of the scene."""


def writer_rules(audio: bool) -> str:
    return WRITER_RULES + ("\n" + WRITER_SOUND_RULE if audio else "")


# ---------- panel asistanı ----------

ASSISTANT_GUIDE = """Reviewing like a director (when asked to review the film, a scene or "what is wrong"):
- Judge in this order: sound, rhythm, camera, directing (acting, staging, emotional arc). Good-looking frames are \
only the starting point.
- Start a film review with directing_review (free, deterministic checks on camera variety, framing, tempo, the \
breath shot and the opening), then look at the shots it names with view_shot before you judge them.
- The story test: one subject, one goal, one turn; a hook in the first seconds; an ending that echoes the opening. \
Say which of these is missing, in one sentence each.
- The failures seen most often, look for them in the frames: subject too small, too far away or against the frame \
edge; too dark, or a colour on the same colour; a gag or key action too fast to understand; a character who slides \
instead of acting (no anticipation or follow-through); worried vs angry eyebrows (one flip apart); characters or \
props that are not in the shot text.
- One fix round: collect everything you find, rank it (most visible first) and propose the fixes together, text \
fixes first (free), regeneration only where text cannot fix it. Do not re-review in circles.
- References teach grammar only: never suggest copying a character, design, melody or shot from an existing work."""


# ---------- film düzeyinde kontrol ----------

SHOT_TR = {"extreme_wide": "çok geniş", "wide": "geniş", "pov": "öznel", "medium": "orta",
           "over_shoulder": "omuz üstü", "close_up": "yakın", "extreme_close_up": "çok yakın"}
MOVE_TR = {"static": "sabit", "dolly_in": "yaklaşma", "dolly_out": "uzaklaşma", "pan": "pan", "tilt": "tilt",
           "crane": "vinç", "orbit": "yörünge", "tracking": "takip", "handheld": "el kamerası"}

MIN_SHOTS = 4            # bundan az çekimde film düzeyi kontroller anlamsız
MIN_MOVES = 4            # DIRECTOR.md §8: en az dört farklı kamera hareketi
RUN_LIMIT = 3            # art arda bu kadar aynı çerçeve ya da hareket uyarı verir
FLAT_CV = 0.12           # süre değişkenlik katsayısı bunun altındaysa tempo tekdüze
BREATH_RATIO = 1.5       # nefes çekimi: ortanca sürenin en az bu katı
BREATH_MIN_SHOTS = 8
SLOW_OPEN_S = 6.0        # sabit ve bu süreden uzun açılış çekimi yavaş sayılır


def _label(table: dict, value) -> str:
    return table.get(value, value or "belirsiz")


def _runs(shots: list[dict], key: str) -> list[tuple[str, list[str]]]:
    """Art arda RUN_LIMIT ya da daha fazla aynı değer: [(değer, [kodlar])]."""
    out, cur, codes = [], None, []
    for sh in shots + [{"code": None, key: object()}]:
        v = sh.get(key)
        if v == cur and v is not None:
            codes.append(sh["code"])
            continue
        if cur is not None and len(codes) >= RUN_LIMIT:
            out.append((cur, codes))
        cur, codes = v, [sh["code"]]
    return out


def shot_rows(scenes) -> list[dict]:
    """Scene satırlarından kontrol girdisi: filmde olan çekimler, sırayla."""
    rows = []
    for sc in sorted(scenes, key=lambda x: (x.seq_order or 0, x.scene_code)):
        if getattr(sc, "in_cut", True) is False:
            continue
        cam = getattr(sc, "camera_plan", None) or sc.camera or {}
        rows.append({"code": sc.scene_code, "duration_s": float(sc.duration_s or 0),
                     "shot": cam.get("shot"), "movement": cam.get("movement")})
    return rows


def review(shots: list[dict]) -> dict:
    """Çekim listesi ([{code, duration_s, shot, movement}], film sırasıyla) için yönetmen kontrolü.
    {"shots": n, "findings": [{"code", "severity": "warn"|"info", "message", "shots": [...]}], "summary": {...}}"""
    n = len(shots)
    findings: list[dict] = []
    durs = [s["duration_s"] for s in shots if s["duration_s"] > 0]
    moves = sorted({s["movement"] for s in shots if s.get("movement")})
    sizes = sorted({s["shot"] for s in shots if s.get("shot")})
    summary = {"total_s": round(sum(durs), 1), "camera_moves": [_label(MOVE_TR, m) for m in moves],
               "framings": [_label(SHOT_TR, x) for x in sizes]}
    if n < MIN_SHOTS:
        return {"shots": n, "findings": [], "summary": summary,
                "note": f"Film düzeyi kontroller için en az {MIN_SHOTS} çekim gerekir."}

    real_moves = [m for m in moves if m != "static"]
    if len(real_moves) < MIN_MOVES:
        findings.append({"code": "camera_variety", "severity": "warn", "shots": [],
                         "message": f"Filmde {len(real_moves)} farklı kamera hareketi var "
                                    f"({', '.join(_label(MOVE_TR, m) for m in real_moves) or 'hiç'}); en az "
                                    f"{MIN_MOVES} olsun. Her hareketin bir gerekçesi olmalı: göstermek, izlemek ya da "
                                    "vurgulamak."})
    for value, codes in _runs(shots, "shot"):
        findings.append({"code": "framing_run", "severity": "warn", "shots": codes,
                         "message": f"{len(codes)} çekim art arda {_label(SHOT_TR, value)} çerçeve "
                                    f"({codes[0]}–{codes[-1]}). Arada çerçeveyi değiştirin (geniş, orta, yakın)."})
    for value, codes in _runs(shots, "movement"):
        if value == "static":
            msg = (f"{len(codes)} çekim art arda sabit kamera ({codes[0]}–{codes[-1]}). Biri hareketli olabilir "
                   "(yaklaşma, pan, takip), yeter ki gerekçesi olsun.")
        else:
            msg = (f"{len(codes)} çekim art arda aynı hareket: {_label(MOVE_TR, value)} ({codes[0]}–{codes[-1]}). "
                   "Tekrarlanan hareket gerekçesini yitirir.")
        findings.append({"code": "movement_run", "severity": "info", "shots": codes, "message": msg})

    if len(durs) >= MIN_SHOTS and mean(durs) > 0 and pstdev(durs) / mean(durs) < FLAT_CV:
        findings.append({"code": "tempo_flat", "severity": "warn", "shots": [],
                         "message": f"Çekim süreleri neredeyse eşit (ortalama {mean(durs):.1f} sn). Tek hızda akan film "
                                    "yorar: kısa ve uzun çekimleri karıştırın, bir hızlanma ya da yavaşlama ekleyin."})
    if len(durs) >= BREATH_MIN_SHOTS:
        med = median(durs)
        if not any(d >= med * BREATH_RATIO for d in durs):
            findings.append({"code": "no_breath", "severity": "info", "shots": [],
                             "message": f"Filmde nefes aldıran uzun bir çekim yok (ortanca {med:.1f} sn). Dönüm "
                                        "noktasından önce ya da duygusal doruğa bir uzun, sakin çekim koyun."})
    first = shots[0]
    if first["duration_s"] > SLOW_OPEN_S and first.get("movement") in (None, "static"):
        findings.append({"code": "slow_open", "severity": "info", "shots": [first["code"]],
                         "message": f"Açılış çekimi {first['code']} {first['duration_s']:.1f} sn ve sabit. İlk "
                                    "saniyelerde merak uyandıran bir şey olsun; yavaş bir logo ya da boş açılış "
                                    "izleyiciyi kaçırır."})
    return {"shots": n, "findings": findings, "summary": summary}
