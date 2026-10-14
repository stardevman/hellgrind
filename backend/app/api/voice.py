"""Ses-önce akışı uç noktaları: Ses sayfasının verisi, geçici ses, ses denetimi, final kayıt yükleme, sekans ses kilidi ve animatik.

Final kayıt iki adımda bağlanır: dosya `POST /projects/{id}/voice/upload` ile içerik-adresli depoya yüklenir, sonra
mevcut `PUT /scenes/{id}/audio` (kind: dialogue, source: upload, replaces: replik) ile repliğin yerine geçer. Kilitli
sekansta replik değiştirilemez; kilit ve kilidin açılması kürasyon kaydına düşer (voice_lock, voice_unlock).
"""
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import storage
from app.api.deps import ProjectId, SequenceId, TrackId, ensure_not_frozen, project_or_404
from app.api.uploads import receive
from app.budget import killswitch
from app.config import settings
from app.curation import curated
from app.db import get_session, session_scope
from app.engine.references import sha_of, streams
from app.models import AudioTrack, Scene, Sequence
from app.post import voice
from app.quality.anomaly import probe_duration
from app.schemas import VoiceLockIn

router = APIRouter(tags=["voice"])
WORKING = ("probing", "generating", "correcting", "audio", "lipsync", "muxing", "grading", "storyboard")


@router.get("/projects/{project_id}/voice")
def get_voice(project_id: ProjectId, s: Session = Depends(get_session)):
    """Replikler sekans ve çekim başlıkları altında: kaynak (geçici / final), ölçülen süre, çekimdeki yeri, kilit."""
    project_or_404(s, project_id)
    return voice.state(s, project_id)


@router.get("/audio-tracks/{track_id}/peaks")
def track_peaks(track_id: TrackId, s: Session = Depends(get_session)):
    """Repliğin dalga formu (0-1 tepe değerleri). Dosya değişmedikçe önbellekten gelir."""
    t = s.get(AudioTrack, track_id)
    if t is None:
        raise HTTPException(404, "Ses kanalı yok")
    if not t.file_key:
        raise HTTPException(409, "Bu replik henüz seslendirilmedi.")
    return JSONResponse({"peaks": voice.peaks_for(t.file_key), "duration_s": t.duration_s},
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/projects/{project_id}/voice/temp")
@curated("voice_temp")
def make_temp(project_id: ProjectId, sequence_id: str | None = None, s: Session = Depends(get_session)):
    """Seslendirilmemiş sentez repliklerinin geçici sesini kuyruğa verir (yerel Okur; model yoksa espeak)."""
    project_or_404(s, project_id)
    ensure_not_frozen(project_id)
    from app.worker.tasks import voice_temp
    voice_temp.delay(project_id, sequence_id)
    return {"queued": True}


@router.post("/projects/{project_id}/voice/check")
@curated("voice_check")
def check_lines(project_id: ProjectId, s: Session = Depends(get_session)):
    """Ses denetimini kuyruğa verir: sesi olan ama denetlenmemiş replikler yazıya dökülür ve metinle karşılaştırılır."""
    project_or_404(s, project_id)
    if not voice.checker_on():
        raise HTTPException(409, "Ses denetimi kapalı ya da Duyu-small modeli kurulu değil.")
    from app.worker.tasks import voice_check
    voice_check.delay(project_id)
    return {"queued": True}


@router.post("/projects/{project_id}/voice/upload", status_code=201)
@curated("voice_upload")
async def upload_line(project_id: ProjectId, request: Request, filename: str = Query(..., max_length=200)):
    """Final kayıt (ham gövde): içerik-adresli depolanır; süre ölçülür. Repliğe bağlamak için PUT /scenes/{id}/audio."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in voice.AUDIO_EXTENSIONS:
        raise HTTPException(422, f"Ses dosyası bekleniyordu ({', '.join(voice.AUDIO_EXTENSIONS)}).")

    def ready():
        with session_scope() as s:
            project_or_404(s, project_id)
    await run_in_threadpool(ready)
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        path = os.path.join(tmp, "line" + ext)
        await receive(request, path)

        def store():
            if "audio" not in streams(path):
                raise HTTPException(422, "Dosyada ses akışı yok.")
            duration = round(probe_duration(path), 2)
            if duration <= 0:
                raise HTTPException(422, "Dosyanın süresi 0.")
            key = f"uploads/voice/{project_id}/{sha_of(path)}{ext}"
            if not storage.exists(key):
                storage.put_file(path, key, "audio/mpeg" if ext == ".mp3" else "audio/wav" if ext == ".wav" else "audio/mp4")
            return {"key": key, "duration_s": duration, "filename": os.path.basename(filename),
                    "match": dict(zip(("scene_code", "n"), voice.parse_line_file(os.path.basename(filename)) or ()))}
        return await run_in_threadpool(store)


@router.post("/sequences/{sequence_id}/voice-lock")
@curated(lambda kw, r: "voice_lock" if kw["body"].locked else "voice_unlock")
def voice_lock(sequence_id: SequenceId, body: VoiceLockIn, s: Session = Depends(get_session)):
    """Sekansın sesini kilitler ya da kilidi açar. Kilitte görüntüsü üretilmemiş çekimlerin süresi sesten hesaplanır."""
    seq = s.get(Sequence, sequence_id)
    if seq is None:
        raise HTTPException(404, "Sekans yok")
    pid = str(seq.project_id)
    busy = [sc.scene_code for sc in s.scalars(select(Scene).where(Scene.sequence_id == seq.id)) if sc.status in WORKING]
    if body.locked and busy:
        raise HTTPException(409, f"Üretim sürüyor ({', '.join(busy)}); bitince kilitleyin.")
    try:
        result = voice.lock(s, seq) if body.locked else voice.unlock(s, seq)
    except voice.VoiceNotReady as e:
        raise HTTPException(409, str(e))
    s.commit()
    killswitch.event(pid, "voice_locked" if body.locked else "voice_unlocked", sequence_id=sequence_id, code=seq.code,
                     **{k: v for k, v in result.items() if k in ("retimed", "drift")})
    return {"sequence_id": sequence_id, "code": seq.code, **result}


@router.post("/projects/{project_id}/animatic")
@curated("animatic")
def make_animatic(project_id: ProjectId, s: Session = Depends(get_session)):
    """Animatiği kuyruğa verir: onaylı kareler (yoksa çekim kartı) geçici ya da final sesle, sesin süresine göre."""
    project_or_404(s, project_id)
    from app.worker.tasks import assemble_animatic
    assemble_animatic.delay(project_id)
    return {"queued": True}
