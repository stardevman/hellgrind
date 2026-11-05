"""Asistanın önerdiği eylemleri yönetmenin onayından sonra çalıştırır. Eylemler panelin kendi uç noktalarıyla aynı
fonksiyonlardan geçer (kürasyon kaydı, Kill-Switch, bütçe rezervasyonu, onaylı plan kuralları aynen geçerli). Onay,
ücretli çağrı izin akışına (app.budget.consent, varsa) da "izin verildi" olarak yazılır: üretim sırasında aynı iş için
ikinci kez sorulmaz."""
import logging

from fastapi import HTTPException
from sqlalchemy import func

log = logging.getLogger("hellgrind.assistant")

# Eylemin tetikleyebileceği ücretli çağrı türleri (consent türleri); video üretimi izin akışında muaftır
CONSENT_KINDS = {"rerender_shot": ("lipsync", "image", "voice", "llm"), "more_candidates": ("lipsync", "image", "llm"),
                 "use_candidate": ("lipsync",)}


def grant_consent(row, scope: str = "once", panel_session: str | None = None) -> None:
    """Yönetmenin sohbetteki onayını ücretli çağrı izin akışına yazar. Modül yoksa (henüz birleşmediyse) bir şey yapmaz."""
    try:
        from app.budget import consent                 # type: ignore[attr-defined]
    except ImportError:
        return
    for kind in CONSENT_KINDS.get(row.tool, ()):
        try:
            consent.allow(str(row.project_id), kind, str(row.scene_id) if row.scene_id else None, scope,
                          panel_session, by="assistant")
        except Exception:                               # noqa: BLE001 — izin yazılamazsa üretim yine sorar
            log.warning("asistan onayı izin akışına yazılamadı (%s)", kind, exc_info=True)


def _run(s, row) -> dict:
    from app.api import scenes
    from app.assistant.tools import _find_variation
    from app.models import Scene
    sc = s.get(Scene, row.scene_id)
    if sc is None:
        return {"ok": False, "error": "Çekim artık yok."}
    sid = str(sc.id)
    if row.tool == "rerender_shot":
        out = scenes.render(scene_id=sid, force_visual=True, thaw=False, dry_run=False, s=s)
    elif row.tool == "more_candidates":
        out = scenes.more_candidates(scene_id=sid, n=int(row.input.get("n") or 1), s=s)
    elif row.tool == "use_candidate":
        v = _find_variation(s, sc, row.input.get("variation_id"))
        out = scenes.promote(scene_id=sid, variation_id=str(v.id), override=True, full=False, s=s)
    else:
        return {"ok": False, "error": f"Bilinmeyen eylem: {row.tool}"}
    if out.get("blocked"):
        return {"ok": False, "error": out["blocked"].get("message") or "Üretim engellendi: eksik referans",
                "detail": out["blocked"]}
    if out.get("awaiting_plan"):
        return {"ok": False, "error": "Plan onayı bekleniyor: Kurgu sayfasında planı onaylayın.", "detail": out}
    return {"ok": True, "stages": out.get("stages"), **({k: out[k] for k in ("n", "total_usd_estimate") if k in out})}


def execute(s, row, by: str = "approve", panel_session: str | None = None) -> dict:
    """Eylemi çalıştırır, sonucu satıra yazar. Dönen: {"ok", "stages" | "error"}."""
    grant_consent(row, "session" if by == "session" else "once", panel_session)
    row.status, row.decided_at = "approved", func.now()
    s.commit()
    try:
        out = _run(s, row)
    except HTTPException as e:
        s.rollback()
        out = {"ok": False, "error": str(e.detail), "http": e.status_code}
    except Exception as e:                              # noqa: BLE001 — sonuç sohbete yazılır
        s.rollback()
        log.exception("asistan eylemi başarısız")
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    row = s.merge(row)
    row.status, row.result = ("done" if out.get("ok") else "failed"), {**out, "by": by}
    s.commit()
    return out


def decline(s, row) -> None:
    from app.curation import record
    row.status, row.decided_at, row.result = "declined", func.now(), {"by": "director"}
    s.commit()
    record(str(row.project_id), "assistant_decline", str(row.scene_id) if row.scene_id else None,
           payload={"tool": row.tool, "summary": row.summary, "input": row.input})
