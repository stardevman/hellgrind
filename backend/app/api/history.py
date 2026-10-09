"""Servis geçmişi: ücretli servislere (ücretsiz modda sanal olarak) yapılan her çağrı. Hangi servis ve model, ne için,
hangi proje ve çekimde, ne kadar; sağlayıcının istek kimliği, süre, hata. Liste, özet ve CSV dışa aktarma aynı süzgeçleri
kullanır. Kayıtlar app.budget.history tarafından bütçe defteriyle aynı işlemde yazılır.
"""
import csv
import io
import uuid
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.services import SERVICES
from app.budget.history import OPERATIONS
from app.db import get_session

router = APIRouter(tags=["history"])

MODE_SQL = {"real": "c.dry_run IS FALSE", "virtual": "c.dry_run IS TRUE", "unknown": "c.dry_run IS NULL"}
MODE_OF = {False: "real", True: "virtual", None: "unknown"}
MODE_TR = {"real": "gerçek", "virtual": "sanal", "unknown": "bilinmiyor"}
STATUS_TR = {"running": "sürüyor", "succeeded": "başarılı", "failed": "başarısız"}
CSV_LIMIT = 100_000

AGG = """COUNT(*) AS calls,
         COUNT(*) FILTER (WHERE c.status = 'failed') AS failed,
         COUNT(*) FILTER (WHERE c.status = 'running') AS running,
         COUNT(*) FILTER (WHERE c.dry_run IS FALSE) AS real_calls,
         COUNT(*) FILTER (WHERE c.dry_run IS TRUE) AS virtual_calls,
         COALESCE(SUM(c.amount_usd) FILTER (WHERE c.dry_run IS FALSE), 0) AS real_usd,
         COALESCE(SUM(c.amount_usd) FILTER (WHERE c.dry_run IS TRUE), 0) AS virtual_usd,
         COALESCE(SUM(c.amount_usd) FILTER (WHERE c.dry_run IS NULL), 0) AS unknown_usd,
         COALESCE(SUM(c.estimate_usd) FILTER (WHERE c.status = 'running'), 0) AS reserved_usd"""
AGG_KEYS = ("calls", "failed", "running", "real_calls", "virtual_calls", "real_usd", "virtual_usd", "unknown_usd",
            "reserved_usd")


def service_name(service: str) -> str:
    return SERVICES[service]["name"] if service in SERVICES else "Diğer"


class Filter:
    """Ortak süzgeç: proje, çekim, servis, işlem, mod (gerçek/sanal), durum ve son N gün."""

    def __init__(self, project_id: uuid.UUID | None = None, scene_id: uuid.UUID | None = None,
                 service: str | None = Query(None, max_length=40), operation: str | None = Query(None, max_length=40),
                 mode: Literal["all", "real", "virtual", "unknown"] = "all",
                 status: Literal["all", "running", "succeeded", "failed"] = "all",
                 days: int | None = Query(None, ge=1, le=3650)):
        where, self.params = ["TRUE"], {}
        for column, value in (("project_id", project_id), ("scene_id", scene_id), ("service", service),
                              ("operation", operation)):
            if value is not None:
                where.append(f"c.{column} = :{column}")
                self.params[column] = value
        if mode != "all":
            where.append(MODE_SQL[mode])
        if status != "all":
            where.append("c.status = :status")
            self.params["status"] = status
        if days is not None:
            where.append("c.started_at > now() - make_interval(days => :days)")
            self.params["days"] = days
        self.sql = " AND ".join(where)


def _num(value) -> float | None:
    return None if value is None else round(float(value), 4)


def _item(r) -> dict:
    d = r.details or {}
    return {
        "id": r.id, "reservation": str(r.reservation),
        "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        # Boşta bekleme satırı anlık yazılır: süre, worker'ın boşta geçirdiği gerçek saniyedir.
        "duration_s": (float(d["idle_seconds"]) if d.get("idle_seconds") is not None
                       else round((r.finished_at - r.started_at).total_seconds(), 2) if r.finished_at else None),
        "service": r.service, "service_name": service_name(r.service),
        "console": SERVICES.get(r.service, {}).get("console"),
        "operation": r.operation, "operation_title": OPERATIONS.get(r.operation, r.operation),
        "label": r.label, "model": r.model, "model_inferred": bool(d.get("model_inferred")),
        "request_id": r.request_id, "mode": MODE_OF[r.dry_run], "mode_inferred": bool(d.get("mode_inferred")),
        "backfilled": bool(d.get("backfilled")), "status": r.status,
        "estimate_usd": _num(r.estimate_usd), "amount_usd": _num(r.amount_usd),
        "error": r.error, "error_type": d.get("error_type"), "attempt": d.get("attempt"), "resolution": d.get("resolution"),
        "steps": d.get("steps", []),
        "project_id": str(r.project_id) if r.project_id else None, "project_title": r.title,
        "subject": r.subject, "scene_id": str(r.scene_id) if r.scene_id else None,
        "variation_id": str(r.variation_id) if r.variation_id else None,
        "asset_id": str(r.asset_id) if r.asset_id else None, "asset_type": r.asset_type,
        "tier": r.tier, "wave_no": r.wave_no,
    }


def _rows(s: Session, f: Filter, limit: int, before: int | None = None):
    return s.execute(text(f"""
        SELECT c.id, c.reservation, c.started_at, c.finished_at, c.service, c.operation, c.label, c.model, c.request_id,
               c.dry_run, c.status, c.estimate_usd, c.amount_usd, c.error, c.details, c.project_id,
               COALESCE(p.title, c.project_title) AS title, c.subject, c.scene_id, c.variation_id, c.asset_id,
               a.type AS asset_type, v.tier, v.wave_no
        FROM service_calls c
        LEFT JOIN projects p ON p.id = c.project_id
        LEFT JOIN variations v ON v.id = c.variation_id
        LEFT JOIN assets a ON a.id = c.asset_id
        WHERE {f.sql} AND (CAST(:before AS bigint) IS NULL OR c.id < :before)
        ORDER BY c.id DESC LIMIT :limit
    """), {**f.params, "before": before, "limit": limit}).all()


@router.get("/history")
def list_calls(f: Filter = Depends(), limit: int = Query(100, ge=1, le=500), before: int | None = Query(None, ge=1),
               s: Session = Depends(get_session)):
    """Çağrılar, en yeniden eskiye. Sonraki sayfa: `before=<next>`."""
    rows = _rows(s, f, limit + 1, before)
    items = [_item(r) for r in rows[:limit]]
    return {"items": items, "next": items[-1]["id"] if len(rows) > limit else None}


def _group(s: Session, f: Filter, columns: str, group: str, order: str = "real_usd DESC, virtual_usd DESC, calls DESC"):
    return s.execute(text(f"""
        SELECT {columns}, {AGG} FROM service_calls c LEFT JOIN projects p ON p.id = c.project_id
        WHERE {f.sql} GROUP BY {group} ORDER BY {order}
    """), f.params).mappings().all()


def _figures(row) -> dict:
    return {k: int(row[k]) if k.endswith(("calls", "failed", "running")) else round(float(row[k]), 4) for k in AGG_KEYS}


@router.get("/history/summary")
def summary(f: Filter = Depends(), tz: str = Query("UTC", max_length=64), s: Session = Depends(get_session)):
    """Toplamlar ve dağılımlar: servis, model, işlem, proje ve gün (gün sınırı `tz` saat dilimine göre)."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, f"Bilinmeyen saat dilimi: {tz}")
    totals = s.execute(text(f"SELECT {AGG} FROM service_calls c WHERE {f.sql}"), f.params).mappings().one()
    span = s.execute(text(f"SELECT MIN(c.started_at), MAX(c.started_at) FROM service_calls c WHERE {f.sql}"), f.params).one()
    return {
        "totals": _figures(totals),
        "first_at": span[0].isoformat() if span[0] else None, "last_at": span[1].isoformat() if span[1] else None,
        "by_service": [{"service": r["service"], "name": service_name(r["service"]), **_figures(r)}
                       for r in _group(s, f, "c.service", "c.service")],
        "by_model": [{"service": r["service"], "model": r["model"], **_figures(r)}
                     for r in _group(s, f, "c.service, c.model", "c.service, c.model")],
        "by_operation": [{"operation": r["operation"], "title": OPERATIONS.get(r["operation"], r["operation"]), **_figures(r)}
                         for r in _group(s, f, "c.operation", "c.operation")],
        "by_project": [{"project_id": str(r["project_id"]) if r["project_id"] else None, "title": r["title"],
                        "deleted": r["project_id"] is None, **_figures(r)}
                       for r in _group(s, f, "c.project_id, COALESCE(p.title, c.project_title) AS title", "1, 2")],
        "by_day": _days(s, f, tz),
    }


def _days(s: Session, f: Filter, tz: str) -> list[dict]:
    rows = s.execute(text(f"""
        SELECT (c.started_at AT TIME ZONE :tz)::date AS day, {AGG} FROM service_calls c
        WHERE {f.sql} GROUP BY 1 ORDER BY 1 DESC
    """), {**f.params, "tz": tz}).mappings().all()
    return [{"day": r["day"].isoformat(), **_figures(r)} for r in rows]


CSV_COLUMNS = ("Zaman (UTC)", "Servis", "Model", "İşlem", "Proje", "Çekim ya da varlık", "Mod", "Durum", "Tahmin (USD)",
               "İşlenen (USD)", "Süre (sn)", "İstek kimliği", "Defter etiketi", "Deneme", "Hata")


@router.get("/history.csv")
def export_csv(f: Filter = Depends(), s: Session = Depends(get_session)):
    """Süzgece uyan bütün çağrılar (en yeniden eskiye), UTF-8 (BOM'lu) CSV."""
    out = io.StringIO()
    out.write("﻿")                                          # Excel UTF-8'i tanısın
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for r in _rows(s, f, CSV_LIMIT):
        x = _item(r)
        w.writerow([datetime.fromisoformat(x["started_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), x["service_name"],
                    x["model"] or "", x["operation_title"], x["project_title"] or "", x["subject"] or "",
                    MODE_TR[x["mode"]], STATUS_TR.get(x["status"], x["status"]), f"{x['estimate_usd']:.4f}",
                    "" if x["amount_usd"] is None else f"{x['amount_usd']:.4f}",
                    "" if x["duration_s"] is None else x["duration_s"], x["request_id"] or "", x["label"],
                    x["attempt"] or "", x["error"] or ""])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="servis-gecmisi.csv"'})
