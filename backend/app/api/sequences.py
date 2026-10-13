"""Sekanslar ve film düzeyinde ses (Faz 11).

Sekans: filmin bir bölümü; isteğe bağlı bütçe tavanı vardır. Tavan dolunca yalnızca o sekans durur; tavan yükseltilince
(ya da kaldırılınca) yeniden açılır.

Zaman çizelgesi kanalları sekans ya da film düzeyindedir: prosedürel müzik ya da bestecinin yüklediği zamanlanmış stem'ler.
Bir kanal değişince hiçbir çekim yeniden üretilmez; yalnızca film yeniden birleştirilir (ücretsiz, yerel FFmpeg).
"""
import json
import os
import tempfile
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import ProjectId, SequenceId, TrackId, project_or_404
from app.budget import killswitch
from app.config import settings
from app.core.hashing import stable_hash
from app.curation import curated
from app.db import get_session, session_scope
from app.engine.references import EXTENSIONS, MAX_UPLOAD_BYTES, sha_of, streams
from app.hermes.ingest import music_hash
from app.models import Project, Scene, Sequence, TimelineTrack
from app.quality.anomaly import probe_duration
from app.schemas import SequencePatch, TrackIn, TrackPatch

router = APIRouter(tags=["sequences"])
TRACK_KINDS = ("music", "sfx", "ambience")


def _media(key: str | None) -> str | None:
    return f"/api/media?key={key}" if key else None


def track_out(t: TimelineTrack) -> dict:
    return {"id": str(t.id), "sequence_id": str(t.sequence_id) if t.sequence_id else None, "kind": t.kind,
            "source": t.source, "mood": (t.params or {}).get("mood"), "filename": (t.params or {}).get("filename"),
            "duration_s": (t.params or {}).get("duration_s"), "url": _media(t.file_key), "start_s": t.start_s,
            "end_s": t.end_s, "gain_db": t.gain_db, "fade_in_s": t.fade_in_s, "fade_out_s": t.fade_out_s,
            "is_active": t.is_active}


def sequences_out(s: Session, project_id: str) -> list[dict]:
    """Sekanslar, çekim sayıları, harcama, tavan ve aktif kanallarıyla. Sorgu sayısı sekans sayısından bağımsızdır."""
    rows = s.scalars(select(Sequence).where(Sequence.project_id == project_id).order_by(Sequence.seq_order)).all()
    counts = defaultdict(lambda: {"shots": 0, "done": 0})
    for seq_id, status, n in s.execute(select(Scene.sequence_id, Scene.status, func.count())
                                       .where(Scene.project_id == project_id).group_by(Scene.sequence_id, Scene.status)):
        counts[seq_id]["shots"] += n
        if status == "done":
            counts[seq_id]["done"] += n
    tracks = defaultdict(list)
    for t in s.scalars(select(TimelineTrack).where(TimelineTrack.project_id == project_id, TimelineTrack.is_active)
                       .order_by(TimelineTrack.start_s, TimelineTrack.created_at)):
        tracks[t.sequence_id].append(track_out(t))
    return [{"id": str(q.id), "code": q.code, "title": q.title, "seq_order": q.seq_order, "status": q.status,
             "budget_cap_usd": float(q.budget_cap_usd) if q.budget_cap_usd is not None else None,
             "spent_usd": float(q.spent_usd), "reserved_usd": float(q.reserved_usd), **counts[q.id],
             "tracks": tracks[q.id],
             "voice_locked_at": q.voice_locked_at.timestamp() if q.voice_locked_at else None} for q in rows]


def sequence_or_404(s: Session, sequence_id: str, lock: bool = False) -> Sequence:
    q = select(Sequence).where(Sequence.id == sequence_id)
    row = s.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise HTTPException(404, "Sekans bulunamadı")
    return row


def reassemble(project_id: str) -> list[str]:
    """Kanal değişince (değişiklik kesinleştikten sonra çağrılır) filmi yeniden birleştirir, tamamlanmış sahne varsa.
    Para harcamaz: dondurulmuş projede de çalışır."""
    with session_scope() as s:
        done = s.scalar(select(func.count()).select_from(Scene).where(
            Scene.project_id == project_id, Scene.status == "done", Scene.final_video_key.is_not(None)))
        has_draft = s.get(Project, project_id).draft_timeline_key is not None
    if not done:
        return []
    from app.worker.tasks import auto_assemble             # tasks -> celery_app döngüsünü önlemek için geç import
    if not auto_assemble(project_id, "final", "müzik"):   # bitmemiş çekim varken eksik montaj üretilmez
        return []
    if has_draft:                                           # kaba kurgu da aynı müziği çalsın
        auto_assemble(project_id, "draft", "müzik")
    return ["ASSEMBLE"]


# ---------- sekanslar ----------

@router.get("/projects/{project_id}/sequences")
def list_sequences(project_id: ProjectId, s: Session = Depends(get_session)):
    project_or_404(s, project_id)
    return sequences_out(s, project_id)


@router.patch("/sequences/{sequence_id}")
@curated("sequence_edit")
def patch_sequence(sequence_id: SequenceId, body: SequencePatch, s: Session = Depends(get_session)):
    """Başlık ve tavan. Tavan yükseltilir ya da kaldırılırsa duran sekans yeniden açılır (sahneleri `pending` olur);
    harcamanın altına indirilirse sekans hemen durur."""
    q = sequence_or_404(s, sequence_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is not None:
        q.title = " ".join(changes["title"].split())
    cap_changed = "budget_cap_usd" in changes
    if cap_changed:
        q.budget_cap_usd = changes["budget_cap_usd"]
    s.commit()
    pid = str(q.project_id)
    if cap_changed:
        cap = q.budget_cap_usd
        if q.status == "capped":
            killswitch.reopen_sequence(pid, q.id)
        elif cap is not None and float(q.spent_usd + q.reserved_usd) >= float(cap):
            killswitch.cap_sequence(pid, q.id, "budget_cap_lowered")
    s.expire_all()
    return next(x for x in sequences_out(s, pid) if x["id"] == sequence_id)


# ---------- zaman çizelgesi kanalları ----------

def _scope(s: Session, project_id: str | None, sequence_id: str | None) -> tuple[str, str | None]:
    """(proje, sekans) kapsamı; sekans verilmişse projesi oradan okunur."""
    if sequence_id:
        return str(sequence_or_404(s, sequence_id).project_id), sequence_id
    project_or_404(s, project_id)
    return project_id, None


def _add_procedural(project_id: str, sequence_id: str | None, body: TrackIn) -> dict:
    with session_scope() as s:
        if body.replace:                                     # yüklenen stem'ler korunur
            for t in s.scalars(select(TimelineTrack).where(
                    TimelineTrack.project_id == project_id, TimelineTrack.sequence_id == sequence_id,
                    TimelineTrack.is_active, TimelineTrack.kind == body.kind, TimelineTrack.source == "procedural")):
                t.is_active = False
        t = TimelineTrack(project_id=project_id, sequence_id=sequence_id, kind=body.kind, source="procedural",
                          params={"mood": body.mood}, start_s=body.start_s, end_s=body.end_s, gain_db=body.gain_db,
                          fade_in_s=body.fade_in_s, fade_out_s=body.fade_out_s, content_hash=music_hash(body.mood))
        s.add(t)
        s.flush()
        out = track_out(t)
    killswitch.event(project_id, "track_changed", track_id=out["id"], sequence_id=sequence_id, track_kind=body.kind,
                     mood=body.mood)
    return {"track": out, "stages": reassemble(project_id)}


def _add_upload(project_id: str, sequence_id: str | None, path: str, filename: str, kind: str, params: dict) -> dict:
    """Bestecinin stem'i: içerik-adresli yüklenir; aynı dosya aynı anahtarı alır. Var olan kanalların yerini almaz
    (`replaces` verilmedikçe): bir sekansta birden çok zamanlanmış stem olabilir."""
    if "audio" not in streams(path):
        raise HTTPException(422, "Dosyada ses akışı yok.")
    duration = round(probe_duration(path), 3)
    if duration <= 0:
        raise HTTPException(422, "Dosyanın süresi 0.")
    sha, ext = sha_of(path), os.path.splitext(filename)[1].lower()
    key = f"uploads/tracks/{project_id}/{sha}{ext}"
    if not storage.exists(key):
        storage.put_file(path, key, "audio/mpeg" if ext == ".mp3" else "audio/wav" if ext == ".wav" else "audio/mp4")
    with session_scope() as s:
        if params.get("replaces"):
            old = s.get(TimelineTrack, params["replaces"])
            if old is None or str(old.project_id) != project_id:
                raise HTTPException(404, "Değiştirilecek kanal bu projede yok")
            old.is_active = False
        t = TimelineTrack(project_id=project_id, sequence_id=sequence_id, kind=kind, source="upload", file_key=key,
                          params={"filename": os.path.basename(filename), "duration_s": duration},
                          start_s=params["start_s"], end_s=params["end_s"], gain_db=params["gain_db"],
                          fade_in_s=params["fade_in_s"], fade_out_s=params["fade_out_s"],
                          content_hash=stable_hash({"kind": kind, "source": "upload", "sha": sha}))
        s.add(t)
        s.flush()
        out = track_out(t)
    killswitch.event(project_id, "track_changed", track_id=out["id"], sequence_id=sequence_id, track_kind=kind,
                     filename=os.path.basename(filename))
    return {"track": out, "stages": reassemble(project_id)}


async def _create_track(request: Request, project_id: str | None, sequence_id: str | None, query: dict) -> dict:
    """JSON gövde: prosedürel müzik (TrackIn). Başka gövde: ham ses dosyası (stem), ayarlar sorgu parametrelerinde."""
    def scope():
        with session_scope() as s:
            return _scope(s, project_id, sequence_id)
    pid, seq = await run_in_threadpool(scope)
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = TrackIn.model_validate(json.loads(await request.body() or b"{}"))
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, str(e))
        return await run_in_threadpool(_add_procedural, pid, seq, body)

    filename = query["filename"]
    if not filename:
        raise HTTPException(422, "Ses dosyası yüklerken filename sorgu parametresi gerekli (ör. ?filename=tema.wav)")
    if os.path.splitext(filename)[1].lower() not in EXTENSIONS["audio"]:
        raise HTTPException(422, f"Desteklenen uzantılar: {', '.join(EXTENSIONS['audio'])}")
    if query["kind"] not in TRACK_KINDS:
        raise HTTPException(422, f"Kanal türü {', '.join(TRACK_KINDS)} olmalı")
    if query["end_s"] is not None and query["end_s"] <= query["start_s"]:
        raise HTTPException(422, "end_s, start_s'den büyük olmalı")
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "stem" + os.path.splitext(filename)[1].lower())
        size = 0
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Dosya çok büyük (en çok 200 MB)")
                f.write(chunk)
        if size == 0:
            raise HTTPException(422, "Boş dosya")
        return await run_in_threadpool(_add_upload, pid, seq, path, filename, query["kind"], query)


def _track_query(kind: str = Query("music"), filename: str | None = Query(None, max_length=200),
                 start_s: float = Query(0.0, ge=0, le=36000), end_s: float | None = Query(None, gt=0, le=36000),
                 gain_db: float = Query(0.0, ge=-60, le=20), fade_in_s: float = Query(0.0, ge=0, le=60),
                 fade_out_s: float = Query(0.0, ge=0, le=60), replaces: str | None = Query(None)) -> dict:
    return dict(kind=kind, filename=filename, start_s=start_s, end_s=end_s, gain_db=gain_db, fade_in_s=fade_in_s,
                fade_out_s=fade_out_s, replaces=replaces)


@router.get("/sequences/{sequence_id}/tracks")
def list_tracks(sequence_id: SequenceId, s: Session = Depends(get_session)):
    sequence_or_404(s, sequence_id)
    rows = s.scalars(select(TimelineTrack).where(TimelineTrack.sequence_id == sequence_id)
                     .order_by(TimelineTrack.is_active.desc(), TimelineTrack.start_s, TimelineTrack.created_at))
    return [track_out(t) for t in rows]


@router.post("/sequences/{sequence_id}/tracks", status_code=201)
@curated("track_change")
async def create_sequence_track(sequence_id: SequenceId, request: Request, query: dict = Depends(_track_query)):
    """Sekans kanalı. JSON gövde ({"mood": "joy", "gain_db": -10}) prosedürel müzik; ham ses gövdesi
    (?filename=tema.wav&start_s=4&gain_db=-6) bestecinin zamanlanmış stem'i. Hiçbir çekim yeniden üretilmez."""
    return await _create_track(request, None, sequence_id, query)


@router.post("/projects/{project_id}/tracks", status_code=201)
@curated("track_change")
async def create_film_track(project_id: ProjectId, request: Request, query: dict = Depends(_track_query)):
    """Film düzeyinde kanal (bütün filme yayılır; zamanlar filmin başına göredir)."""
    return await _create_track(request, project_id, None, query)


@router.patch("/timeline-tracks/{track_id}")
@curated("track_change")
def patch_track(track_id: TrackId, body: TrackPatch, s: Session = Depends(get_session)):
    t = s.get(TimelineTrack, track_id)
    if t is None or not t.is_active:
        raise HTTPException(404, "Aktif kanal bulunamadı")
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None or key == "end_s":
            setattr(t, key, value)
    if t.end_s is not None and t.end_s <= t.start_s:
        raise HTTPException(422, "end_s, start_s'den büyük olmalı")
    s.commit()
    killswitch.event(str(t.project_id), "track_changed", track_id=track_id, track_kind=t.kind)
    return {"track": track_out(t), "stages": reassemble(str(t.project_id))}


@router.delete("/timeline-tracks/{track_id}")
@curated("track_remove")
def delete_track(track_id: TrackId, s: Session = Depends(get_session)):
    """Kanalı devre dışı bırakır (silmez); film yeniden birleştirilir."""
    t = s.get(TimelineTrack, track_id)
    if t is None or not t.is_active:
        raise HTTPException(404, "Aktif kanal bulunamadı")
    t.is_active = False
    s.commit()
    killswitch.event(str(t.project_id), "track_changed", track_id=track_id, track_kind=t.kind, removed=True)
    return {"ok": True, "stages": reassemble(str(t.project_id))}
