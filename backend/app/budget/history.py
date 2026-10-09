"""Ücretli servis geçmişi: her ücretli (ücretsiz modda sanal) çağrının ayrıntılı kaydı.

Satırı bütçe rezervasyonu açar (`killswitch.reserve` -> `open_call`), commit ya da release kapatır (`close_call`); ikisi de
defterle aynı işlemdedir, yani geçmiş ile bütçe birbirinden ayrışmaz. Çağrı sürerken ücretli modüller (app.live) gerçekten
çağırdıkları modeli ve sağlayıcının istek kimliğini `step()` ile not eder; notlar çağrının bağlamında (contextvar) birikir
ve kapanışta satıra yazılır. Ücretsiz modda adım notu yoktur: satırın modeli, ücretli modda çağrılacak olandır (sanal).
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import text

from app.config import settings

OPERATIONS = {                       # işlem -> panelde ve CSV'de görünen ad
    "video": "Video üretimi",
    "first_frame": "İlk kare (önce görsel)",
    "storyboard": "Storyboard karesi",
    "reference": "Varlık referansı",
    "motion_test": "Hareket testi",
    "previs": "Previs rehberinden video",
    "revision": "Düzenlenmiş kareden video",
    "lipsync": "Dudak senkronu",
    "tts": "Diyalog seslendirme",
    "script_parse": "Senaryo ayrıştırma",
    "idle": "GPU makinesi boşta",
    "other": "Diğer",
}
PURPOSES = ("first_frame", "storyboard", "reference", "motion_test", "previs", "revision", "lipsync", "idle")
VIDEO_ENGINES = ("fal_wan", "seedance", "higgsfield")      # fal.ai motorları; MiniMax H3 kendi sunucumuzda ("vast")
MAX_ERROR = 600


@dataclass(frozen=True)
class Described:
    service: str
    operation: str
    models: tuple[str, ...]
    resolution: str | None = None

    @property
    def model(self) -> str | None:
        return " + ".join(self.models) or None


QWEN_IMAGE = "Qwen/Qwen-Image-2512"             # comfy-workflows/keyframe.json (metinden ilk kare)
QWEN_EDIT = "Qwen/Qwen-Image-Edit-2511"         # comfy-workflows/edit_keyframe.json (karakter referansından ilk kare)


def describe(label: str) -> Described:
    """Defter etiketinden (ör. "h3:720p", "flux_lora:first_frame:480p", "fal:lipsync") servis, işlem ve ücretli modda
    çağrılan model(ler). Modeller çağrı anındaki ayarlardan (panelin Ayarlar sayfası) okunur."""
    head, *rest = label.split(":")
    purpose = next((p for p in rest if p in PURPOSES), None)
    resolution = next((p for p in rest if p.endswith("p") and p[:-1].isdigit()), None)
    if head == "h3":                           # MiniMax H3: RunPod Serverless (varsayılan) ya da vast.ai (deneysel)
        service = "vast" if settings.h3_platform == "vast" else "runpod"
        if purpose == "storyboard" or (purpose == "reference" and "qwen" in rest):   # aynı worker'da görsel modeli
            return Described(service, purpose, (QWEN_EDIT if "qwen_edit" in rest else QWEN_IMAGE,), resolution)
        return Described(service, purpose or "video", (settings.h3_model,), resolution)
    if head == "elevenlabs":
        return Described("elevenlabs", "tts", (settings.elevenlabs_model,))
    if head == "anthropic":
        return Described("anthropic", "script_parse", (settings.hermes_model,))
    if head == "fal" and purpose == "lipsync":
        return Described("fal", "lipsync", (settings.fal_lipsync_endpoint,))
    if head == "flux_lora":
        return Described("fal", purpose or "other", (settings.fal_flux_endpoint,), resolution)
    if head == "flux_kontext":                 # karakterin referans görseliyle kare
        return Described("fal", purpose or "other", (settings.fal_kontext_endpoint,), resolution)
    if head == "v2v":
        return Described("fal", "previs", (settings.fal_wan_v2v_endpoint,), resolution)
    if head in VIDEO_ENGINES:
        video = {"fal_wan": settings.fal_wan_endpoint, "seedance": settings.fal_seedance_endpoint}.get(head, head)
        # Karakter referansı Flux karesinden yörünge videosudur; diğer videolar hazır kareden (önce görsel) üretilir.
        models = (settings.fal_flux_endpoint, video) if purpose == "reference" else (video,)
        return Described("fal", purpose or "video", models, resolution)
    return Described("other", "other", (), resolution)


# ---------- çağrı bağlamı ----------

@dataclass
class Call:
    """Sürmekte olan bir çağrının notları: sağlayıcıya giden her istek (model, istek kimliği, parametreler)."""
    steps: list[dict] = field(default_factory=list)


_current: ContextVar[Call | None] = ContextVar("hg_service_call", default=None)


@contextmanager
def recording():
    """Bütçeli çağrının gövdesi boyunca `step()` notlarını toplar (killswitch.spend kullanır)."""
    call = Call()
    token = _current.set(call)
    try:
        yield call
    finally:
        _current.reset(token)


def step(model: str, request_id=None, **info) -> None:
    """Ücretli modülden: sağlayıcıya bir istek gitti. Bütçeli bir çağrının dışında (ör. birim testi) yok sayılır.
    `info` yalnızca açıklayıcı parametrelerdir (çözünürlük, süre, belirteç...); adres ve anahtar yazılmaz."""
    call = _current.get()
    if call is None:
        return
    entry = {"model": model, **({"request_id": str(request_id)} if request_id else {}),
             **{k: v for k, v in info.items() if v is not None}}
    call.steps.append(json.loads(json.dumps(entry, ensure_ascii=False, default=str)))


def error_text(error) -> str | None:
    """Kayda yazılacak hata metni: kısaltılmış ve anahtar değerleri maskelenmiş."""
    if error is None:
        return None
    if isinstance(error, BaseException):
        error = str(error).strip() or type(error).__name__
    from app.live import redact                       # anahtar değerleri yalnızca app.live içinden okunur (tembel)
    return redact(str(error))[:MAX_ERROR]


# ---------- satır (bütçe işlemiyle aynı işlemde) ----------

def open_call(s, rid, project_id, project_title, label: str, estimate: float, *, scene_id=None, variation_id=None,
              sequence_id=None, asset_id=None, details: dict | None = None) -> None:
    d = describe(label)
    if asset_id is None and variation_id is not None:     # hareket testi: varyasyon varlığa aittir
        asset_id = s.execute(text("SELECT asset_id FROM variations WHERE id = :v"), {"v": variation_id}).scalar()
    subject = None
    if scene_id is not None:
        subject = s.execute(text("SELECT scene_code FROM scenes WHERE id = :s"), {"s": scene_id}).scalar()
    if subject is None and asset_id is not None:
        subject = s.execute(text("SELECT name FROM assets WHERE id = :a"), {"a": asset_id}).scalar()
    s.execute(text("""
        INSERT INTO service_calls (reservation, project_id, sequence_id, scene_id, variation_id, asset_id, project_title,
                                   subject, service, operation, label, model, dry_run, estimate_usd, details)
        VALUES (:rid, :p, :q, :s, :v, :a, :title, :subject, :service, :op, :label, :model, :dry, :est, CAST(:d AS jsonb))
    """), {"rid": rid, "p": project_id, "q": sequence_id, "s": scene_id, "v": variation_id, "a": asset_id,
           "title": project_title, "subject": subject, "service": d.service, "op": d.operation, "label": label,
           "model": d.model, "dry": settings.dry_run, "est": estimate,
           "d": json.dumps({**({"resolution": d.resolution} if d.resolution else {}), **(details or {})},
                           ensure_ascii=False, default=str)})


def close_call(s, rid, status: str, amount: float, call: Call | None = None, error=None) -> None:
    """Çağrıyı kapatır. Adım notu varsa model ve istek kimliği gerçekten çağrılandır."""
    steps = call.steps if call else []
    details = {"steps": steps} if steps else {}
    if isinstance(error, BaseException):
        details["error_type"] = type(error).__name__
    models = list(dict.fromkeys(st["model"] for st in steps))
    request_id = next((st["request_id"] for st in reversed(steps) if st.get("request_id")), None)
    s.execute(text("""
        UPDATE service_calls SET status = :st, amount_usd = :amt, finished_at = now(), error = :err,
                                 model = COALESCE(:model, model), request_id = COALESCE(:req, request_id),
                                 details = details || CAST(:d AS jsonb)
        WHERE reservation = :rid
    """), {"rid": rid, "st": status, "amt": amount, "err": error_text(error), "model": " + ".join(models) or None,
           "req": request_id, "d": json.dumps(details, ensure_ascii=False)})


def annotate_last(scene_id, label_prefix: str, details: dict) -> None:
    """Çekimin bu etiketle başlayan en son servis çağrısına ayrıntı ekler (ör. kare kalite kapısının ölçümleri)."""
    from app.db import session_scope
    with session_scope() as s:
        s.execute(text("""
            UPDATE service_calls SET details = details || CAST(:d AS jsonb)
            WHERE id = (SELECT id FROM service_calls WHERE scene_id = :s AND label LIKE :l
                        ORDER BY started_at DESC LIMIT 1)
        """), {"s": scene_id, "l": label_prefix + "%", "d": json.dumps(details, ensure_ascii=False, default=str)})


# ---------- eski kayıtlar ----------

def mode_at(ts: float, switches: list[tuple[float, bool]]) -> bool:
    """O andaki dry_run. `switches`: [(zaman, ücretli moda mı geçildi)], zamana göre sıralı. Andan önceki son geçiş modu
    belirler; önce hiç geçiş yoksa ilk geçişten önceki mod onun tersidir; kayıtta hiç geçiş yoksa şimdiki mod."""
    before = [paid for t, paid in switches if t <= ts]
    if before:
        return not before[-1]
    if switches:
        return switches[0][1]
    return settings.dry_run


def complete_backfill(s, audit: list[dict]) -> int:
    """Migration'ın aktardığı (geçmiş sisteminden önceki) satırları tamamlar: modu bilinmeyenlere ayar değişiklik
    kaydındaki mod geçişlerinden (details.mode_inferred), modeli olmayanlara etiketten bugünkü ayarlarla
    (details.model_inferred). Her satır bir kez işlenir."""
    switches = sorted((float(e["ts"]), str(e.get("detail", "")).startswith("ücretli"))
                      for e in audit if e.get("field") == "DRY_RUN" and e.get("action") == "mode" and "ts" in e)
    rows = s.execute(text("""SELECT id, label, started_at, dry_run, model FROM service_calls
                             WHERE details->>'backfilled' = 'true' AND details->>'completed' IS NULL""")).all()
    for row_id, label, started_at, dry_run, model in rows:
        extra: dict = {"completed": True}
        if dry_run is None:
            dry_run = mode_at(started_at.timestamp(), switches)
            extra["mode_inferred"] = True
        if model is None and (model := describe(label).model):
            extra["model_inferred"] = True
        s.execute(text("""UPDATE service_calls SET dry_run = :dry, model = :model, details = details || CAST(:d AS jsonb)
                          WHERE id = :i"""), {"i": row_id, "dry": dry_run, "model": model, "d": json.dumps(extra)})
    return len(rows)
