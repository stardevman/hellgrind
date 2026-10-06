"""MiniMax H3 çıktı güvencesi: makinede üretilen hiçbir çıktı yalnızca makinede kalmaz.

Her iş gönderilir gönderilmez kayda geçer (`h3_outputs`: makine, sunucu, iş kimliği, tür). İş bitince sunucunun yanıtı
(çıktı dosyaları, GPU süresi) kayda yazılır; çıktı hemen indirilir:
- indirilen bayt sayısı sunucunun bildirdiği Content-Length ile karşılaştırılır,
- dosya ve iş meta verisi (sunucu yanıtı + kaynak bilgisi, JSON) yerel nesne deposuna içerik adresli yazılır
  (`h3-outputs/ab/<sha256>.<uzantı>`),
- depodaki kopyanın boyutu ve SHA-256'sı yeniden okunarak indirilenle karşılaştırılır; ancak o zaman kayıt `stored` olur.

Eski makine yalnızca kendi kayıtlarının hiçbiri `submitted`/`completed` değilken silinir (`failed`: sunucu iş üretemedi,
`lost`: yönetmen kaybı kabul etti). İndirilemeyen çıktılar panelde listelenir: makine çalışıyorsa boşta bekleme görevi
onları yeniden indirmeyi dener; yönetmen eski makineyi başlatıp indirtebilir ya da kaybı kabul edip silebilir.
"""
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid

from sqlalchemy import text

from app import storage
from app.config import settings
from app.db import session_scope
from app.engine.providers import TransientError

log = logging.getLogger("hellgrind.live.outputs")

UNSAFE = ("submitted", "completed")
PREFIX = "h3-outputs"
LISTED = 50
SWEEP_LIMIT = 5
SETTLE_S = 600               # bu kadar eski "completed" kaydı indirmesi yarım kalmış sayılır (worker çökmüş)


class NotStored(TransientError):
    """Çıktı yerel depoya doğrulanarak yazılamadı: yeniden denenir (iş sunucuda durur, yeniden üretilmez)."""


def _instance() -> str | None:
    return settings.vast_instance_id or None


# ---------- kayıt ----------

def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


def submitted(server: str, job_id: str, kind: str, task: str, project_id: str | None, remote: dict) -> None:
    """Gönderilen iş kayda geçer: sonucu alınamasa da makinede bir çıktı oluşabilir."""
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO h3_outputs (instance_id, server, backend, job_id, kind, task, project_id, remote)
            VALUES (:i, :s, :b, :j, :k, :t, :p, CAST(:r AS jsonb))
            ON CONFLICT (server, job_id) DO UPDATE SET
              instance_id = EXCLUDED.instance_id, backend = EXCLUDED.backend, kind = EXCLUDED.kind, task = EXCLUDED.task,
              project_id = EXCLUDED.project_id, remote = EXCLUDED.remote, status = 'submitted', size_bytes = NULL,
              sha256 = NULL, storage_key = NULL, meta_key = NULL, error = NULL, submitted_at = now(), stored_at = NULL
        """), {"i": _instance(), "s": server, "b": settings.h3_backend, "j": job_id, "k": kind, "t": task,
               "p": _uuid(project_id), "r": json.dumps(remote, ensure_ascii=False)})


def completed(server: str, job_id: str, done: dict) -> None:
    remote = {k: v for k, v in done.items() if k != "history"}
    with session_scope() as s:
        s.execute(text("""UPDATE h3_outputs SET status = 'completed', remote = remote || CAST(:r AS jsonb), error = NULL
                          WHERE server = :s AND job_id = :j AND status IN ('submitted', 'completed')"""),
                  {"s": server, "j": job_id, "r": json.dumps({**remote, "completed_at": time.time()}, ensure_ascii=False)})


def failed(server: str, job_id: str, error: str) -> None:
    """Sunucu işi başarısız bildirdi: makinede çıktı yoktur."""
    with session_scope() as s:
        s.execute(text("""UPDATE h3_outputs SET status = 'failed', error = :e
                          WHERE server = :s AND job_id = :j AND status IN ('submitted', 'completed')"""),
                  {"s": server, "j": job_id, "e": error[:500]})


def note(row_id: int, error: str) -> None:
    with session_scope() as s:
        s.execute(text("UPDATE h3_outputs SET error = :e WHERE id = :i"), {"i": row_id, "e": error[:500]})


def _row(server: str, job_id: str) -> dict | None:
    with session_scope() as s:
        row = s.execute(text("""SELECT id, instance_id, backend, kind, task, project_id, remote, submitted_at
                                FROM h3_outputs WHERE server = :s AND job_id = :j"""), {"s": server, "j": job_id}).first()
    if row is None:
        return None
    return {"id": row[0], "instance_id": row[1], "backend": row[2], "kind": row[3], "task": row[4],
            "project_id": str(row[5]) if row[5] else None, "remote": row[6] or {}, "submitted_at": row[7].isoformat()}


# ---------- yerel depoya yazma ----------

def _put_verified(path: str, key: str, size: int, digest: str, content_type: str) -> None:
    if not (storage.exists(key) and storage.size_of(key) == size and storage.sha256_of(key) == digest):
        storage.put_file(path, key, content_type)
        stored_size, stored_digest = storage.size_of(key), storage.sha256_of(key)
        if stored_size != size or stored_digest != digest:
            raise NotStored(f"yerel depodaki kopya indirilenle uyuşmuyor ({key}: {stored_size} bayt, "
                            f"{stored_digest[:12]}…; beklenen {size} bayt, {digest[:12]}…)")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive(server: str, job_id: str, download, extra: dict | None = None) -> str:
    """İndirilmiş ve boyutu doğrulanmış çıktıyı (vast_h3.Download) ve iş meta verisini yerel depoya yazar, depodaki
    kopyaları yeniden okuyup doğrular, kaydı `stored` yapar. Dönen değer çıktının depo anahtarı."""
    if _sha256(download.path) != download.sha256:
        raise NotStored(f"indirilen dosya okunurken değişti ({job_id})")
    row = _row(server, job_id) or {}
    ext = os.path.splitext(download.path)[1].lower() or ".bin"
    key = f"{PREFIX}/{download.sha256[:2]}/{download.sha256}{ext}"
    content_type = "video/mp4" if ext == ".mp4" else ("image/png" if ext == ".png" else
                                                       "image/jpeg" if ext in (".jpg", ".jpeg") else "application/octet-stream")
    try:
        _put_verified(download.path, key, download.size, download.sha256, content_type)
        meta = {"job_id": job_id, "server": server, "backend": row.get("backend") or settings.h3_backend,
                "instance_id": row.get("instance_id"), "kind": row.get("kind"), "task": row.get("task"),
                "submitted_at": row.get("submitted_at"), "remote": row.get("remote"), "server_name": download.name,
                "output": {"key": key, "size_bytes": download.size, "sha256": download.sha256}, **(extra or {})}
        blob = json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        meta_digest = hashlib.sha256(blob).hexdigest()
        meta_key = f"{PREFIX}/{download.sha256[:2]}/{download.sha256}.json"
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.work_dir, suffix=".json", delete=False) as f:
            f.write(blob)
        try:
            _put_verified(f.name, meta_key, len(blob), meta_digest, "application/json")
        finally:
            os.remove(f.name)
    except NotStored:
        raise
    except Exception as e:                                       # noqa: BLE001 — depo erişilemedi: yeniden denenir
        raise NotStored(f"çıktı yerel depoya yazılamadı ({type(e).__name__}: {str(e)[:200]})") from e
    with session_scope() as s:
        s.execute(text("""UPDATE h3_outputs SET status = 'stored', size_bytes = :n, sha256 = :h, storage_key = :k,
                                 meta_key = :m, error = NULL, stored_at = now()
                          WHERE server = :s AND job_id = :j"""),
                  {"s": server, "j": job_id, "n": download.size, "h": download.sha256, "k": key, "m": meta_key})
    return key


# ---------- güvence ----------

def unsafe(instance_id: str, limit: int | None = None) -> list[dict]:
    """Makinedeki, yerel depoya henüz doğrulanarak yazılmamış çıktılar."""
    with session_scope() as s:
        rows = s.execute(text(f"""SELECT id, server, backend, job_id, kind, task, status, remote, error, submitted_at
                                  FROM h3_outputs WHERE instance_id = :i AND status IN ('submitted', 'completed')
                                  ORDER BY submitted_at {'LIMIT :n' if limit else ''}"""),
                         {"i": str(instance_id), **({"n": limit} if limit else {})}).all()
    return [{"id": r[0], "server": r[1], "backend": r[2], "job_id": r[3], "kind": r[4], "task": r[5], "status": r[6],
             "remote": r[7] or {}, "error": r[8], "submitted_at": r[9].isoformat()} for r in rows]


def unsafe_count(instance_id: str) -> int:
    with session_scope() as s:
        return int(s.execute(text("""SELECT count(*) FROM h3_outputs
                                     WHERE instance_id = :i AND status IN ('submitted', 'completed')"""),
                             {"i": str(instance_id)}).scalar())


def mark_lost(instance_id: str) -> int:
    """Yönetmen kaybı kabul etti: indirilemeyen çıktılar `lost` olur (makine silinebilir)."""
    with session_scope() as s:
        return s.execute(text("""UPDATE h3_outputs SET status = 'lost',
                                        error = coalesce(error || ' · ', '') || 'yönetmen kaybı kabul etti'
                                 WHERE instance_id = :i AND status IN ('submitted', 'completed')"""),
                         {"i": str(instance_id)}).rowcount


def summary() -> dict:
    """Panel: makine başına çıktı sayıları ve yerel depoda olmayan çıktılar (yalnızca kayıt okunur)."""
    current = settings.vast_instance_id or None
    with session_scope() as s:
        counts = s.execute(text("""SELECT instance_id, status, count(*), coalesce(sum(size_bytes), 0)
                                   FROM h3_outputs GROUP BY 1, 2""")).all()
        rows = s.execute(text("""SELECT id, instance_id, kind, task, job_id, status, error, submitted_at
                                 FROM h3_outputs WHERE status IN ('submitted', 'completed')
                                 ORDER BY submitted_at DESC LIMIT :n"""), {"n": LISTED}).all()
    machines: dict[str, dict] = {}
    for instance_id, status, n, size in counts:
        m = machines.setdefault(instance_id or "", {"instance_id": instance_id, "current": instance_id == current,
                                                   "stored": 0, "unsafe": 0, "failed": 0, "lost": 0, "stored_bytes": 0})
        if status in UNSAFE:
            m["unsafe"] += int(n)
        else:
            m[status] += int(n)
        if status == "stored":
            m["stored_bytes"] += int(size)
    return {"machines": sorted(machines.values(), key=lambda m: (not m["current"], -m["unsafe"], m["instance_id"] or "")),
            "unsafe": [{"id": r[0], "instance_id": r[1], "kind": r[2], "task": r[3], "job_id": r[4], "status": r[5],
                        "error": r[6], "submitted_at": r[7].isoformat()} for r in rows]}


# ---------- yeniden indirme (ağ: yalnızca ücretli akıştan) ----------

def _comfy_guess(c, row: dict):
    """ComfyUI geçmişi kaybolduysa (sunucu yeniden başladı) dosya, işin benzersiz önekinden tahmin edilir."""
    from app.live import vast_h3
    prefix = (row["remote"] or {}).get("prefix")
    if not prefix:
        return None
    folder, base = os.path.split(prefix)
    for ext in ((".png", ".jpg", ".webp") if row["kind"] == "keyframe" else (".mp4", ".webm")):
        name = f"{base}_00001_{ext}"
        params = {"filename": name, "subfolder": folder, "type": "output"}
        with c.stream("GET", "/view", params=params) as r:      # yalnızca var mı: gövde okunmaz
            found = r.status_code == 200
        if found:
            return {"outputs": {"guess": {"files": [{"filename": name, "subfolder": folder, "type": "output"}]}}}
    return vast_h3.NOT_FOUND


def retrieve_row(row: dict, url: str, token: str) -> str:
    """Tek bir kaydı sunucudan yeniden indirir. Dönen değer: stored | failed | running | missing."""
    import httpx
    from app.live import vast_h3
    image = row["kind"] == "keyframe"
    with vast_h3._client(url, token) as c:
        backend = vast_h3.Comfy(c) if row["backend"] == "comfyui" else vast_h3.SGLang(c)
        done = row["remote"] if row["status"] == "completed" and row["remote"].get("outputs") is not None else None
        if done is None:
            try:
                done = backend.poll(row["job_id"])
            except vast_h3.JobFailed as e:
                failed(row["server"], row["job_id"], str(e))
                return "failed"
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                done = vast_h3.NOT_FOUND
            if done is None and isinstance(backend, vast_h3.Comfy):
                if backend.queued(row["job_id"]):
                    return "running"
                done = _comfy_guess(c, row)
            if done is None:
                return "running"
            if done is vast_h3.NOT_FOUND:
                note(row["id"], "sunucu bu işi tanımıyor (yeniden başlamış olabilir); çıktı makinede kalmış olabilir")
                return "missing"
            completed(row["server"], row["job_id"], done)
        download = backend.download(row["job_id"], done, image=image)
    try:
        archive(row["server"], row["job_id"], download, {"retrieved": True})
    finally:
        if os.path.exists(download.path):
            os.remove(download.path)
    return "stored"


def retrieve(instance_id: str, url: str, token: str, limit: int | None = None, settle: bool = False) -> dict:
    """Makinenin yerel depoda olmayan çıktılarını indirir. settle: yalnızca worker'ın artık beklemediği kayıtlar (boşta
    bekleme görevi; süren işlere dokunulmaz)."""
    counts = {"stored": 0, "failed": 0, "running": 0, "missing": 0, "error": 0}
    now = time.time()
    rows = unsafe(instance_id)
    if settle:
        rows = [r for r in rows if (r["status"] == "completed" and now - float(r["remote"].get("completed_at") or 0) > SETTLE_S)
                or (r["status"] == "submitted" and _age(r) > float(settings.h3_timeout_s) + SETTLE_S)]
    for row in rows[:limit] if limit else rows:
        try:
            counts[retrieve_row(row, url, token)] += 1
        except Exception as e:                                   # noqa: BLE001 — sıradaki kayıtla sürer
            counts["error"] += 1
            note(row["id"], f"yeniden indirilemedi: {type(e).__name__}: {str(e)[:200]}")
            log.warning("H3 çıktısı yeniden indirilemedi (%s): %s", row["job_id"], e)
    counts["remaining"] = unsafe_count(instance_id)
    return counts


def _age(row: dict) -> float:
    from datetime import datetime
    return time.time() - datetime.fromisoformat(row["submitted_at"]).timestamp()
