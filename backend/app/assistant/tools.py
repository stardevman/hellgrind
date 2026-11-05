"""Panel asistanının araçları. Ağ yok: veritabanı, nesne deposu ve yerel FFmpeg. Ücretli olabilecek eylemler (yeniden
üretim, yeni aday, aday seçimi: dudak senkronu çalışabilir) doğrudan çalışmaz; onay bekleyen bir `AssistantAction` açar ve
panelde "İzin ver / Bu oturum için tümüne izin ver / Vazgeç" kartı çıkar. Yönetmen sohbette "bu oturum için tümüne izin
ver" dediyse eylem hemen başlar.

Araç tanımları (TOOLS) API'ye giden JSON şemalarıdır; uygulamalar `run(ctx, name, input)` ile çağrılır ve API'nin
tool_result içerik bloklarını döner (metin, kareler için görsel: {"type": "image", "source": {"type": "hg_key"}})."""
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import settings

MAX_TEXT = 3000          # uzun metin alanlarının araç yanıtındaki üst sınırı
FRAME_WIDTH = 768        # Claude'a gösterilen karelerin genişliği (px)
MAX_FRAMES = 8

# Onay gerektiren araçlar ve paneldeki kart başlıkları
PAID = {"rerender_shot": "Çekimi yeniden üret", "more_candidates": "Yeni aday üret", "use_candidate": "Bu adayı kullan"}

TOOLS = [
    {"name": "project_overview",
     "description": "Projenin özeti: bütçe (limit, harcanan, ayrılan, kalan), çalışma modu (ücretsiz/ücretli), her çekimin "
                    "kodu, durumu, süresi, tarifi (kısaltılmış), motoru, en iyi skoru, anomalileri, son hatası, "
                    "storyboard onayı; son olaylar. Bir şeyi değerlendirmeden önce buradan başla.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "shot_details",
     "description": "Tek çekimin bütün ayrıntıları: senaryo metni, tarif, yönetmen notu, kamera, istem (yönetmen istemi "
                    "dahil), kadro ve replikler, adaylar (kimlik, skorlar, anomaliler, eleme nedeni, sıra, bedel), hata.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string", "description": "Çekim kodu, ör. S013 ya da S013_SH01"}},
         "required": ["shot"], "additionalProperties": False}},
    {"name": "view_shot",
     "description": "Çekimin videosundan eşit aralıklı kareleri görsel olarak getirir (montajlı final, yoksa 1. aday, ya da "
                    "verilen aday). Video yoksa storyboard/ilk kare görseli gelir. Görüntüde sorun aramak için kullan.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string"},
         "frames": {"type": "integer", "minimum": 1, "maximum": MAX_FRAMES, "description": "Kare sayısı (varsayılan 4)"},
         "variation_id": {"type": "string", "description": "İsteğe bağlı: belirli bir adayın kimliği (ilk 8 hane yeter)"}},
         "required": ["shot"], "additionalProperties": False}},
    {"name": "update_shot",
     "description": "Çekimin metnini düzeltir (ücretsiz, geri alınabilir, hiçbir şey üretmez): tarif, yönetmen notu, süre "
                    "ya da yönetmen istemi. Değişiklik bir sonraki üretimde kullanılır; görüntüyü değiştirmek için ardından "
                    "rerender_shot öner.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string"},
         "description": {"type": "string", "description": "Yeni çekim tarifi (görüntüde ne olacağı)"},
         "director_note": {"type": "string", "description": "Yönetmen notu: istemlere eklenen düzeltme (boş: siler)"},
         "duration_s": {"type": "number", "minimum": 2, "maximum": 15},
         "prompt_override": {"type": "string", "description": "Yönetmen istemi: motora olduğu gibi gider (boş: siler)"}},
         "required": ["shot"], "additionalProperties": False}},
    {"name": "rerender_shot",
     "description": "Çekimin görüntüsünü baştan üretir. ÜCRETLİ: çağrı yönetmene tahmini bedelle onay kartı gösterir; "
                    "onaylanınca başlar. Neden yeniden üretileceğini kısa yaz.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["shot", "reason"], "additionalProperties": False}},
    {"name": "more_candidates",
     "description": "Çekim için n yeni aday üretir (önceki adaylar silinmez, en yüksek skorlu kurguya girer). ÜCRETLİ: "
                    "onay kartı gösterilir.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string"}, "n": {"type": "integer", "minimum": 1, "maximum": 3},
         "reason": {"type": "string"}},
         "required": ["shot", "reason"], "additionalProperties": False}},
    {"name": "use_candidate",
     "description": "Çekimde başka bir adayı kurguya alır (yalnızca ses ve montaj yeniden çalışır; ücretli modda dudak "
                    "senkronu gerekebilir). Onay kartı gösterilir.",
     "input_schema": {"type": "object", "properties": {
         "shot": {"type": "string"}, "variation_id": {"type": "string"}, "reason": {"type": "string"}},
         "required": ["shot", "variation_id", "reason"], "additionalProperties": False}},
    {"name": "assemble_film",
     "description": "Tamamlanan çekimleri filmde yeniden birleştirir (ücretsiz, yerel). Düzeltmelerden sonra kullan.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}

# Paneldeki etkinlik satırı (asistan ne yaptı)
ACTIVITY = {"project_overview": "Proje durumuna baktı", "shot_details": "{shot} ayrıntılarını okudu",
            "view_shot": "{shot} karelerine baktı", "update_shot": "{shot} metnini düzeltti",
            "rerender_shot": "{shot} için yeniden üretim önerdi", "more_candidates": "{shot} için yeni aday önerdi",
            "use_candidate": "{shot} için aday değişikliği önerdi", "assemble_film": "Filmi yeniden birleştirdi"}


class ToolError(Exception):
    """Aracın Türkçe hatası: modele is_error ile döner."""


@dataclass
class Ctx:
    project_id: str
    session_id: str
    allow_all: bool = False
    actions: list = field(default_factory=list)       # bu turda açılan/çalışan eylemlerin kimlikleri


def activity(name: str, inp: dict) -> str:
    return ACTIVITY.get(name, name).format(shot=(inp or {}).get("shot") or "çekim")


def _text(data) -> list:
    return [{"type": "text", "text": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)}]


def _cut(text, n: int = MAX_TEXT):
    text = text or ""
    return text if len(text) <= n else text[:n] + f"… (+{len(text) - n} karakter)"


def _num(x):
    return None if x is None else round(float(x), 3)


def find_scene(s, project_id: str, shot: str):
    from app.models import Scene
    code = (shot or "").strip().upper()
    if not code:
        raise ToolError("Çekim kodu boş.")
    rows = list(s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)))
    exact = [x for x in rows if x.scene_code.upper() == code]
    if exact:
        return exact[0]
    near = [x for x in rows if x.scene_code.upper().startswith(code)]
    if len(near) == 1:
        return near[0]
    if near:
        raise ToolError(f"{shot} birden çok çekime uyuyor: {', '.join(x.scene_code for x in near[:12])}")
    raise ToolError(f"{shot} adlı çekim yok.")


def _variations(s, sc) -> list:
    from app.models import Variation
    return list(s.scalars(select(Variation).where(Variation.scene_id == sc.id).order_by(Variation.created_at)))


def _find_variation(s, sc, vid: str):
    vid = (vid or "").strip().lower()
    hits = [v for v in _variations(s, sc) if str(v.id).startswith(vid)] if vid else []
    if len(hits) != 1:
        raise ToolError(f"{sc.scene_code} için '{vid}' kimlikli tek bir aday bulunamadı.")
    return hits[0]


# ---------- okuma ----------

def project_overview(s, ctx: Ctx, inp: dict):
    from app.budget import killswitch
    from app.models import Project, Scene
    p = s.get(Project, ctx.project_id)
    scenes = list(s.scalars(select(Scene).where(Scene.project_id == ctx.project_id).order_by(Scene.seq_order)))
    shots = []
    for sc in scenes:
        vs = _variations(s, sc)
        winner = next((v for v in vs if v.refined_rank == 1), None)
        best = max((v.composite_score for v in vs if v.composite_score is not None), default=None)
        shots.append({"shot": sc.scene_code, "status": sc.status, "duration_s": sc.duration_s, "engine": sc.engine,
                      "description": _cut(sc.description or sc.script_text, 220), "in_cut": sc.in_cut,
                      "winner_score": _num(winner.composite_score) if winner else None, "best_score": _num(best),
                      "candidates": len(vs), "anomalies": (winner.anomalies if winner else None) or None,
                      "board": sc.board_status, "spent_usd": _num(sc.spent_usd),
                      "error": _cut(sc.last_error, 300) or None, "has_final": bool(sc.final_video_key)})
    try:
        events = [json.loads(x) for x in killswitch.r().lrange(killswitch.EVENTS.format(ctx.project_id), 0, 14)]
        frozen = killswitch.is_frozen(ctx.project_id)
    except Exception:                                   # noqa: BLE001 — Redis yoksa özet yine verilir
        events, frozen = [], None
    limit, spent, reserved = float(p.budget_limit_usd), float(p.spent_usd), float(p.reserved_usd)
    return _text({"project": p.title, "status": p.status, "frozen": frozen,
                  "mode": "ücretsiz (sanal bedel)" if settings.dry_run else "ücretli (gerçek harcama)",
                  "budget": {"limit_usd": limit, "spent_usd": round(spent, 4), "reserved_usd": round(reserved, 4),
                             "remaining_usd": round(limit - spent - reserved, 4)},
                  "film": {"assembled": bool(p.timeline_key)}, "shots": shots, "recent_events": events})


def shot_details(s, ctx: Ctx, inp: dict):
    from app.models import AudioTrack, Character, SceneCharacter
    sc = find_scene(s, ctx.project_id, inp.get("shot"))
    cast = [{"name": name, "role": role, "position": pos} for name, role, pos in s.execute(
        select(Character.name, SceneCharacter.role, SceneCharacter.screen_position)
        .join(SceneCharacter, SceneCharacter.character_id == Character.id).where(SceneCharacter.scene_id == sc.id))]
    lines = [{"text": t.text, "start_s": t.start_offset_s, "duration_s": t.duration_s} for t in s.scalars(
        select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.kind == "dialogue",
                                 AudioTrack.is_active.is_(True)))]
    spec = dict(sc.prompt_spec or {})
    variations = [{"id": str(v.id)[:8], "tier": v.tier, "status": v.status, "rank": v.refined_rank,
                   "score": _num(v.composite_score), "clip": _num(v.clip_score), "face": _num(v.face_score),
                   "anomalies": v.anomalies or None, "purge_reason": (v.params or {}).get("purge_reason"),
                   "cost_usd": _num(v.cost_usd), "has_video": bool(v.video_key)} for v in _variations(s, sc)]
    return _text({"shot": sc.scene_code, "status": sc.status, "method": sc.method, "engine": sc.engine,
                  "duration_s": sc.duration_s, "emotion": sc.emotion, "camera": sc.camera,
                  "script_text": _cut(sc.script_text), "description": _cut(sc.description),
                  "director_note": sc.director_note, "prompt": _cut(spec.get("prompt")),
                  "prompt_override": _cut(spec.get("override")) or None, "cast": cast, "dialogue": lines,
                  "board_status": sc.board_status, "board_note": sc.board_note, "in_cut": sc.in_cut,
                  "spent_usd": _num(sc.spent_usd), "error": _cut(sc.last_error, 1200) or None,
                  "candidates": variations})


def _duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(video: str, n: int, out_dir: str) -> list[tuple[str, float]]:
    """Videonun eşit aralıklı n karesi (JPEG, FRAME_WIDTH genişliğinde): [(dosya, saniye)]."""
    length = _duration(video)
    frames = []
    for i in range(n):
        t = round(length * (i + 0.5) / n, 2) if length else 0.0
        path = os.path.join(out_dir, f"f{i}.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", path], check=True, timeout=120)
        frames.append((path, t))
    return frames


def _image_block(path: str, session_id: str) -> dict:
    """Kareyi nesne deposuna içerik özetiyle koyar; sohbet kaydında anahtar saklanır (istekte aynı baytlar açılır)."""
    import hashlib

    from app import storage
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:32]
    key = f"assistant/{session_id}/{digest}.jpg"
    if not storage.exists(key):
        storage.put_file(path, key, content_type="image/jpeg")
    return {"type": "image", "source": {"type": "hg_key", "key": key, "media_type": "image/jpeg"}}


def view_shot(s, ctx: Ctx, inp: dict):
    from app import storage
    sc = find_scene(s, ctx.project_id, inp.get("shot"))
    n = max(1, min(MAX_FRAMES, int(inp.get("frames") or 4)))
    if inp.get("variation_id"):
        v = _find_variation(s, sc, inp["variation_id"])
        key, what = v.video_key, f"aday {str(v.id)[:8]}"
    else:
        winner = next((v for v in _variations(s, sc) if v.refined_rank == 1 and v.video_key), None)
        key, what = ((sc.final_video_key, "montajlı final") if sc.final_video_key else
                     (winner.video_key, f"1. aday {str(winner.id)[:8]}") if winner else (None, None))
    with tempfile.TemporaryDirectory(dir=settings.work_dir if os.path.isdir(settings.work_dir) else None) as tmp:
        if key:
            video = storage.download(key, os.path.join(tmp, "v.mp4"))
            frames = extract_frames(video, n, tmp)
            blocks = [{"type": "text", "text": f"{sc.scene_code} — {what}, {n} kare:"}]
            for i, (path, t) in enumerate(frames, start=1):
                blocks += [{"type": "text", "text": f"Kare {i} ({t:g} sn)"}, _image_block(path, ctx.session_id)]
            return blocks
        still = sc.keyframe_key or sc.board_key
        if not still:
            raise ToolError(f"{sc.scene_code} için henüz video ya da kare yok (durum: {sc.status}).")
        path = storage.download(still, os.path.join(tmp, "k" + os.path.splitext(still)[1]))
        jpg = os.path.join(tmp, "k.jpg")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", jpg],
                       check=True, timeout=60)
        label = "yönetmenin yüklediği ilk kare" if sc.keyframe_key else "storyboard karesi"
        return [{"type": "text", "text": f"{sc.scene_code}: video yok; {label}:"}, _image_block(jpg, ctx.session_id)]


# ---------- ücretsiz düzeltmeler ----------

def update_shot(s, ctx: Ctx, inp: dict):
    from app.api.scenes import patch_scene
    from app.schemas import ScenePatch
    sc = find_scene(s, ctx.project_id, inp.get("shot"))
    before = {"description": sc.description, "director_note": sc.director_note, "duration_s": sc.duration_s,
              "prompt_override": (sc.prompt_spec or {}).get("override")}
    patch = {k: inp[k] for k in ("description", "duration_s", "prompt_override") if inp.get(k) is not None}
    changed = []
    if "director_note" in inp:
        sc.director_note = (inp.get("director_note") or "").strip() or None
        changed.append("director_note")
        s.commit()
    result = {}
    if patch:
        result = patch_scene(scene_id=str(sc.id), body=ScenePatch(**patch), s=s)   # kürasyon kaydı (scene_edit) orada
        changed += list(patch)
    if not changed:
        raise ToolError("Değiştirilecek alan verilmedi.")
    from app.curation import record
    record(ctx.project_id, "assistant_edit", str(sc.id), payload={"fields": changed, "before": before,
                                                                  "session": ctx.session_id})
    return _text({"ok": True, "shot": sc.scene_code, "changed": changed, "before": before,
                  "hint": result.get("hint") or "Değişiklik bir sonraki üretimde kullanılır."})


def assemble_film(s, ctx: Ctx, inp: dict):
    from fastapi import HTTPException

    from app.api.panel import assemble
    try:
        return _text(assemble(project_id=ctx.project_id, s=s))
    except HTTPException as e:
        raise ToolError(str(e.detail))


# ---------- onay isteyen eylemler ----------

def estimate(s, sc, tool: str, inp: dict) -> float | None:
    from app.engine import candidates
    if tool == "use_candidate":
        return None                                    # yalnızca ses/montaj; dudak senkronu gerekirse ayrıca sorulur
    n = int(inp.get("n") or 1) if tool == "more_candidates" else 1
    try:
        return float(candidates.estimate(s, sc, n)["total_usd"])
    except Exception:                                  # noqa: BLE001 — tahmin yoksa kart yine açılır
        return None


def summary(sc, tool: str, inp: dict) -> str:
    reason = (inp.get("reason") or "").strip()
    head = {"rerender_shot": f"{sc.scene_code} görüntüsü baştan üretilsin",
            "more_candidates": f"{sc.scene_code} için {int(inp.get('n') or 1)} yeni aday üretilsin",
            "use_candidate": f"{sc.scene_code} için aday {str(inp.get('variation_id'))[:8]} kurguya alınsın"}[tool]
    return f"{head}: {reason}" if reason else head


def propose(s, ctx: Ctx, tool: str, inp: dict):
    """Onay kartı açar; oturumda "tümüne izin ver" açıksa eylemi hemen çalıştırır."""
    from app.assistant import actions
    from app.models import AssistantAction
    sc = find_scene(s, ctx.project_id, inp.get("shot"))
    if tool == "use_candidate":
        _find_variation(s, sc, inp.get("variation_id"))
    usd = estimate(s, sc, tool, inp)
    row = AssistantAction(session_id=ctx.session_id, project_id=ctx.project_id, scene_id=sc.id, tool=tool,
                          input=inp, summary=summary(sc, tool, inp), estimate_usd=usd)
    s.add(row)
    s.commit()
    ctx.actions.append(str(row.id))
    cost = (f"tahmini {usd:.2f} $" + (" (ücretsiz modda sanal)" if settings.dry_run else "")) if usd is not None \
        else "bedel: yalnızca ses ve montaj (ücretli modda dudak senkronu gerekirse ayrıca sorulur)"
    if ctx.allow_all:
        out = actions.execute(s, row, by="session")
        return _text({"started": out.get("ok", False), "action": row.summary, "cost": cost, "result": out,
                      "note": "Yönetmen bu oturum için bütün eylemlere izin verdi; eylem başlatıldı."})
    return _text({"awaiting_approval": True, "action_id": str(row.id), "action": row.summary, "cost": cost,
                  "note": "Panelde onay kartı gösterildi. Yönetmen onaylamadan hiçbir şey üretilmez. Yönetmene ne "
                          "yapılacağını ve bedelini kısaca söyle; sonucu onaydan sonra bir sonraki mesajda öğrenirsin."})


IMPL = {"project_overview": project_overview, "shot_details": shot_details, "view_shot": view_shot,
        "update_shot": update_shot, "assemble_film": assemble_film}


def run(s, ctx: Ctx, name: str, inp: dict) -> tuple[list, bool]:
    """(tool_result içeriği, hata mı)."""
    from fastapi import HTTPException
    if name not in TOOL_NAMES:
        return _text(f"Bilinmeyen araç: {name}"), True
    try:
        if name in PAID:
            return propose(s, ctx, name, inp or {}), False
        return IMPL[name](s, ctx, inp or {}), False
    except ToolError as e:
        s.rollback()
        return _text(str(e)), True
    except HTTPException as e:
        s.rollback()
        return _text(str(e.detail)), True
    except Exception as e:                              # noqa: BLE001 — araç hatası sohbeti düşürmez
        s.rollback()
        return _text(f"Araç hatası ({type(e).__name__}): {_cut(str(e), 500)}"), True
