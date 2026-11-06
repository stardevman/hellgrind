"""Ücretli ek işlemler için yönetmenin izni (video üretimi dışındaki her ücretli çağrı).

Ücretli modda (DRY_RUN=0) video üretimi dışındaki her ücretli çağrı (dudak senkronu, görsel, seslendirme, LLM ile metin,
upscale) rezervasyondan önce yönetmenin iznini ister: `killswitch.spend()` önce `require()` çağırır. İzin yoksa bekleyen
bir istek açılır ve `ConsentRequired` fırlar: çağrı yapılmaz, bütçeden rezervasyon alınmaz. Panel bekleyen istekleri
gösterir ve yönetmen seçer:

  İzin ver                        bu işlem (aynı proje, aynı hedef, aynı tür) ONCE_TTL_S boyunca
  Bu oturum için tümüne izin ver  bütün türler, panel oturumu açık kaldıkça (panelin X-HG-Session başlığı tazeler)
  Reddet                          işlem yapılmaz; dudak senkronunda çekim senkronsuz biter

İzin verilince bekleyen iş kendiliğinden sürer: sahne aşaması sahnenin yeniden planlanmasıyla (yalnızca eksik aşamalar
çalışır), tek başına görev aynı argümanlarla yeniden kuyruğa verilerek; istek içindeki (eşzamanlı) çağrıyı panel 428
yanıtından sonra yineler. Ücretsiz modda hiçbir şey sorulmaz (sanal maliyet). Video üretimi (motor:çözünürlük etiketli
çağrılar) ve makinenin boşta bedeli izin istemez: video yönetmenin açık üretim isteğiyle ve plan onayıyla başlar.

`ConsentRequired`, `ProjectFrozen`'dan türer: donmayı yukarı ileten her yol (ör. "except ProjectFrozen: raise") izin
beklemesini de durdurmadan yukarı iletir; onu yakalayan yerler (görev sarmalayıcısı, API) önce bu sınıfa bakar.
"""
import json
import re
import time
import uuid

from app.budget import killswitch
from app.config import settings

PENDING = "hg:consent:pending"                 # bekleyen isteklerin kimlikleri (küme)
REQUEST = "hg:consent:req:{}"                  # isteğin kendisi (JSON)
ONCE = "hg:consent:once:{}:{}:{}"              # proje, hedef, tür -> tek seferlik izin
SESSION = "hg:consent:session"                 # panel oturumunun kimliği (bütün türler)
DENY = "hg:consent:deny:{}:{}"                 # sahne, tür -> reddedildi (dudak senkronu: senkronsuz biter)

ONCE_TTL_S = 30 * 60                           # "İzin ver": işlemin yeniden denemeleri ve devamı bu süre içinde
SESSION_TTL_S = 15 * 60                        # panel kapanınca oturum izni bu süre sonra biter
REQUEST_TTL_S = 24 * 3600

KINDS = {
    "lipsync": "Dudak senkronu",
    "image": "Görsel üretimi",
    "voice": "Seslendirme (ElevenLabs)",
    "llm": "Yapay zekâ ile metin (Claude)",
    "upscale": "Video büyütme (upscale)",
    "other": "Ücretli ek işlem",
}
VIDEO = re.compile(r"^[a-z0-9_]+:\d{3,4}p(:|$)", re.IGNORECASE)     # motor:çözünürlük[:ek] -> video üretimi
IMAGE_WORDS = ("storyboard", "flux", "qwen", "kontext", "reference", "first_frame", "studio", "image", "keyframe")
# Sahnenin üretim zinciri: izin bekleyen aşama, izin verilince sahnenin yeniden planlanmasıyla sürer.
SCENE_CHAIN_TASKS = ("hg.storyboard_scene", "hg.generate_scene", "hg.lipsync_scene", "hg.grade_scene",
                     "hg.mux_scene")
RESEND_TASKS = ("hg.upscale_clip", "hg.end_board_scene", "hg.studio_image", "hg.voice_temp", "hg.write_prompt")


class ConsentRequired(killswitch.ProjectFrozen):
    """Video dışı ücretli çağrı için yönetmenin izni yok: çağrı yapılmadı, bekleyen istek açıldı."""

    def __init__(self, request: dict):
        self.request = request
        super().__init__(message(request))


def kind_of(label: str | None) -> str | None:
    """Defter etiketinden izin türü; None: izin gerekmez (video üretimi, makinenin boşta bedeli)."""
    low = (label or "").lower()
    if low.startswith("h3:idle"):
        return None
    if low.startswith("upscale"):
        return "upscale"
    if VIDEO.match(low):
        return None
    if "lipsync" in low:
        return "lipsync"
    if low.startswith("elevenlabs"):
        return "voice"
    if low.startswith("anthropic"):
        return "llm"
    if any(w in low for w in IMAGE_WORDS):
        return "image"
    return "other"


def message(req: dict) -> str:
    where = f" ({req['scene_code']})" if req.get("scene_code") else ""
    return (f"Ücretli işlem için izin bekleniyor: {KINDS.get(req.get('kind'), req.get('kind'))}{where}, "
            f"yaklaşık {float(req.get('usd') or 0):.2f} $. Paneldeki izin penceresinden izin ver ya da reddet.")


def _r():
    return killswitch.r()


def _scene_code(scene_id) -> str | None:
    if not scene_id:
        return None
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:
        return s.execute(text("SELECT scene_code FROM scenes WHERE id = :s"), {"s": str(scene_id)}).scalar()


def _current_task():
    """Çalışan Celery görevi (adı, argümanları); görev dışında ya da görevin iş parçacığında None."""
    try:
        from celery import current_task
        if current_task and current_task.request and current_task.request.id:
            return current_task
    except Exception:                                        # noqa: BLE001 — Celery bağlamı yok
        pass
    return None


def target_of(scene_id=None, asset_id=None) -> str:
    """İznin hedefi. Tek başına çalışan görev (ör. Ses sayfasının toplu seslendirmesi, görsel stüdyosu) tek hedeftir:
    bir izin o görevin bütün çağrılarını kapsar; sahne zinciri sahneyi, istek içi çağrı varlığı ya da projeyi hedefler."""
    task = _current_task()
    if task is not None and task.name not in SCENE_CHAIN_TASKS:
        from app.core.hashing import stable_hash
        return f"task:{task.name}:{stable_hash([list(task.request.args or []), task.request.kwargs or {}])[:12]}"
    if scene_id:
        return f"scene:{scene_id}"
    if asset_id:
        return f"asset:{asset_id}"
    return "project"


# ---------- izin durumu ----------

def session_active() -> bool:
    return bool(_r().exists(SESSION))


def session_state() -> dict:
    sid = _r().get(SESSION)
    return {"active": bool(sid), "expires_in_s": max(0, _r().ttl(SESSION)) if sid else 0}


def touch(session_id: str | None) -> None:
    """Panelin her isteği oturum iznini tazeler (yalnızca izni veren oturum). Panel kapanınca SESSION_TTL_S sonra biter."""
    if session_id and _r().get(SESSION) == session_id:
        _r().expire(SESSION, SESSION_TTL_S)


def revoke_session() -> bool:
    return bool(_r().delete(SESSION))


def denied(scene_id, kind: str) -> bool:
    return bool(scene_id) and bool(_r().exists(DENY.format(scene_id, kind)))


def clear_denials(scene_id) -> None:
    """Yönetmenin açık üretim isteği: reddedilen işlemler bu çekimde yeniden sorulur."""
    for kind in KINDS:
        _r().delete(DENY.format(scene_id, kind))


def allowed(project_id: str, kind: str, target: str) -> bool:
    return session_active() or bool(_r().exists(ONCE.format(project_id, target, kind)))


# ---------- istek ----------

def require(project_id: str, label: str, estimate: float, scene_id=None, asset_id=None) -> None:
    """Ücretli çağrıdan (rezervasyondan) önce: ücretsiz modda, izin gerektirmeyen türde ya da izin varken hiçbir şey
    yapmaz; yoksa bekleyen isteği açar (aynı proje, hedef ve tür için tek istek) ve ConsentRequired fırlatır."""
    if settings.dry_run:
        return
    kind = kind_of(label)
    if kind is None:
        return
    target = target_of(scene_id, asset_id)
    if allowed(str(project_id), kind, target):
        return
    raise ConsentRequired(_open(str(project_id), kind, target, label, estimate, scene_id, asset_id))


def _open(project_id: str, kind: str, target: str, label: str, estimate: float, scene_id, asset_id) -> dict:
    for req in pending(project_id):
        if req["kind"] == kind and req["target"] == target:      # aynı işlem zaten bekliyor
            return req
    req = {"id": uuid.uuid4().hex, "project_id": project_id, "kind": kind, "kind_tr": KINDS[kind], "target": target,
           "label": label, "usd": round(float(estimate or 0), 4), "scene_id": str(scene_id) if scene_id else None,
           "scene_code": _scene_code(scene_id), "asset_id": str(asset_id) if asset_id else None,
           "created_at": time.time(), "resume": None}
    _save(req)
    _r().sadd(PENDING, req["id"])
    killswitch.event(project_id, "consent_requested", operation=kind, label=label, usd=req["usd"], scene_id=req["scene_id"],
                     code=req["scene_code"])
    return req


def _save(req: dict) -> None:
    _r().set(REQUEST.format(req["id"]), json.dumps(req), ex=REQUEST_TTL_S)


def get(request_id: str) -> dict | None:
    raw = _r().get(REQUEST.format(request_id))
    return json.loads(raw) if raw else None


def pending(project_id: str | None = None) -> list[dict]:
    out = []
    for rid in sorted(_r().smembers(PENDING)):
        req = get(rid)
        if req is None:                                          # süresi doldu
            _r().srem(PENDING, rid)
            continue
        if project_id is None or req["project_id"] == str(project_id):
            out.append(req)
    return sorted(out, key=lambda x: x["created_at"])


def set_resume(request_id: str, resume: dict | None) -> None:
    """İsteği yakalayan yer (görev sarmalayıcısı) izin verilince nasıl sürüleceğini yazar:
    {"type": "scene", "scene_id"} ya da {"type": "task", "name", "args", "kwargs"}."""
    req = get(request_id)
    if req is not None and req.get("resume") is None:
        req["resume"] = resume
        _save(req)


def resume_for_task(task, scene_id=None) -> dict | None:
    """Yinelenmesi güvenli tek başına görev (çıktısı içerik-adresli ya da ücretli çağrıdan önce duran) kendisiyle sürer;
    sahnenin diğer işleri sahnenin yeniden planlanmasıyla (yalnızca eksik aşamalar: ör. yeni aday ya da 1080p tamamlama
    yeniden gönderilmez, ödenmiş video ikinci kez üretilmesin)."""
    if task is not None and task.name in RESEND_TASKS:
        return {"type": "task", "name": task.name, "args": list(task.request.args or []),
                "kwargs": dict(task.request.kwargs or {})}
    return {"type": "scene", "scene_id": str(scene_id)} if scene_id else None


# ---------- karar ----------

def allow(project_id: str, kind: str, target: str, scope: str = "once", session_id: str | None = None,
          by: str = "panel") -> None:
    """Bekleyen istek olmadan da izin (ör. panel asistanının sohbetteki onayı, eylemden hemen önce)."""
    if scope == "session":
        if not session_id:
            raise ValueError("Oturum izni için panel oturumunun kimliği gerekli")
        _r().set(SESSION, session_id, ex=SESSION_TTL_S)
    else:
        if kind not in KINDS:
            raise ValueError(f"Bilinmeyen izin türü: {kind}")
        _r().set(ONCE.format(project_id, target, kind), by, ex=ONCE_TTL_S)
    if project_id:
        killswitch.event(project_id, "consent_allowed", operation=kind, target=target, scope=scope, by=by)
        _record({"project_id": project_id, "kind": kind, "label": None, "usd": None, "target": target,
                 "scene_id": target.split(":", 1)[1] if target.startswith("scene:") else None, "scene_code": None},
                "consent_session" if scope == "session" else "consent_grant", by)


def grant(request_ids: list[str], scope: str = "once", session_id: str | None = None, by: str = "panel") -> dict:
    """Bekleyen isteklere izin verir ve işleri sürdürür. scope=session: bütün türler bu panel oturumu boyunca ve
    bekleyen bütün istekler. Dönen: {"granted": [...], "resumed": [...], "retry": [...] (paneli yinelemesi gereken)}."""
    if scope == "session":
        allow("", "other", "", "session", session_id, by)
        request_ids = [x["id"] for x in pending()]
    granted, resumed, retry = [], [], []
    for rid in request_ids:
        req = get(rid)
        if req is None:
            continue
        if scope != "session":
            _r().set(ONCE.format(req["project_id"], req["target"], req["kind"]), by, ex=ONCE_TTL_S)
        _close(req)
        _record(req, "consent_session" if scope == "session" else "consent_grant", by)
        granted.append(req["id"])
        outcome = _resume(req)
        (resumed if outcome else retry).append({"id": req["id"], "kind": req["kind"], "scene_code": req["scene_code"],
                                                "resumed": outcome})
    return {"granted": granted, "resumed": resumed, "retry": retry}


def deny(request_ids: list[str], by: str = "panel") -> dict:
    """İşlem yapılmaz. Dudak senkronu reddedilince çekim senkronsuz biter (ses ve montaj sürer, ücret doğmaz); diğer
    türlerde bekleyen sahne `failed` olur ve sebebi yazılır (yeniden üretilirse izin yeniden sorulur)."""
    out = []
    for rid in request_ids:
        req = get(rid)
        if req is None:
            continue
        _close(req)
        _record(req, "consent_deny", by)
        if req["kind"] == "lipsync" and req.get("scene_id"):
            _r().set(DENY.format(req["scene_id"], "lipsync"), "1")
            out.append({"id": req["id"], "resumed": _resume(req)})
            continue
        if (req.get("resume") or {}).get("type") == "scene" or req.get("scene_id"):
            _fail_scene(req)
        out.append({"id": req["id"], "resumed": None})
    return {"denied": out}


def _close(req: dict) -> None:
    _r().srem(PENDING, req["id"])
    _r().delete(REQUEST.format(req["id"]))


def _record(req: dict, action: str, by: str) -> None:
    killswitch.event(req["project_id"], action, operation=req["kind"], label=req.get("label"), usd=req.get("usd"),
                     scene_id=req.get("scene_id"), code=req.get("scene_code"), by=by)
    try:
        from app.curation import record                      # kural 7: yönetmenin kararı kaydedilir
        record(req["project_id"], action, scene_id=req.get("scene_id"),
               payload={"kind": req["kind"], "label": req.get("label"), "usd": req.get("usd"),
                        "target": req.get("target"), "by": by})
    except Exception:                                        # noqa: BLE001 — karar geri alınmaz, günlüğe düşer
        import logging
        logging.getLogger("hellgrind.consent").exception("izin kararı kürasyona yazılamadı")


def _fail_scene(req: dict) -> None:
    from app.db import session_scope
    from app.models import Scene
    with session_scope() as s:
        sc = s.get(Scene, req["scene_id"])
        if sc is not None and sc.status == "awaiting_consent":
            sc.status = "failed"
            sc.last_error = (f"İzin verilmedi: {req['kind_tr']} (yaklaşık {req['usd']:.2f} $). Çekimi yeniden üretirsen "
                             "izin yeniden sorulur.")


def _resume(req: dict) -> str | None:
    """İzin bekleyen işi sürdürür. Dönen: "scene" / "task" ya da None (istek içindeki çağrı: panel yineler)."""
    resume = req.get("resume") or {}
    if resume.get("type") == "task":
        from app.worker.celery_app import celery
        celery.send_task(resume["name"], args=resume.get("args") or [], kwargs=resume.get("kwargs") or {})
        return "task"
    if resume.get("type") == "scene":
        from app.api.plan import dispatch_result
        from app.db import session_scope
        from app.models import Scene
        with session_scope() as s:
            sc = s.get(Scene, resume["scene_id"])
            if sc is None:
                return None
            try:                                             # yalnızca eksik aşamalar; iş kalmadıysa durum geri gelir
                result = dispatch_result(s, sc)
            except Exception as e:                           # noqa: BLE001 — ör. proje donmuş: sahne bekler
                killswitch.event(req["project_id"], "consent_resume_failed", scene_id=req.get("scene_id"),
                                 error=str(e)[:200])
                return None
        killswitch.event(req["project_id"], "consent_resumed", scene_id=req.get("scene_id"),
                         stages=result.get("stages"))
        return "scene"
    return None


def resume_current(request_id: str, scene_id=None) -> None:
    """Tek başına görevin içinden: izin verilince bu görev aynı argümanlarla yeniden kuyruğa verilir."""
    set_resume(request_id, resume_for_task(_current_task(), scene_id))
