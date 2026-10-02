"""Post-prodüksiyon aşamaları (Stateless Re-render'ın LIPSYNC ve MUX adımları).

DRY_RUN=1: espeak-ng ile ücretsiz konuşma, prosedürel müzik, dudak senkronu atlanır (video kopyalanır).
DRY_RUN=0: ElevenLabs + fal lip-sync (app/live), her çağrı Kill-Switch rezervasyonundan geçer.
"""
import os
import uuid

from sqlalchemy import select

from app import storage
from app.budget import killswitch, pricing
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.core.hashing import stable_hash
from app.core.rerender import fingerprints
from app.db import session_scope
from app.engine.iteration import load_characters
from app.models import AudioTrack, Character, Project, Scene, Variation
from app.post.audio import synthesize, synthesize_music
from app.post.mux import AudioInput, mux


def _tmp(ext: str) -> str:
    os.makedirs(settings.work_dir, exist_ok=True)
    return os.path.join(settings.work_dir, f"{uuid.uuid4()}{ext}")


def _state(scene_id):
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene_id, AudioTrack.is_active)
                           .order_by(AudioTrack.start_offset_s)).all()
        fp = fingerprints(scene, load_characters(s, scene_id), tracks)
        project = s.get(Project, scene.project_id)
        cfg = {**DEFAULT_PROJECT_SETTINGS, **(project.settings or {})}
        return scene, tracks, fp, cfg


def _set(scene_id, **fields):
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        for k, v in fields.items():
            setattr(sc, k, v)


def _best_refined(scene_id):
    with session_scope() as s:
        v = s.scalar(select(Variation).where(Variation.scene_id == scene_id, Variation.refined_rank == 1))
        return (str(v.id), v.video_key) if v else (None, None)


def _voice(t: AudioTrack, scene: Scene, out: str) -> float:
    if not settings.dry_run and t.source == "elevenlabs":
        from app.live.elevenlabs import tts
        with session_scope() as s:
            voice_id = s.get(Character, t.character_id).voice_id if t.character_id else None
        with killswitch.spend(str(scene.project_id), pricing.estimate_tts(t.text), "elevenlabs", scene_id=scene.id):
            return tts(t.text or "", voice_id, t.emotion or scene.emotion, out)
    seed = int(stable_hash(str(t.character_id))[:6], 16) if t.character_id else 0
    return synthesize(t.text or "", out, emotion=t.emotion or scene.emotion, voice_seed=seed,
                      engine=settings.tts_engine)


def voice_and_lipsync(scene_id: str) -> str:
    scene, tracks, fp, _ = _state(scene_id)
    if scene.status in ("frozen", "needs_review"):
        return f"skip ({scene.status})"
    vid, video_key = _best_refined(scene_id)
    if video_key is None:
        return "skip (rafine video yok)"
    _set(scene_id, status="audio")

    # 1) Eksik diyalog seslerini üret
    for t in tracks:
        if t.kind == "dialogue" and not t.file_key:
            ext = ".mp3" if (not settings.dry_run and t.source == "elevenlabs") else ".wav"
            path = _tmp(ext)
            dur = _voice(t, scene, path)
            key = f"audio/{scene_id}/{t.id}{ext}"
            storage.put_file(path, key, "audio/mpeg" if ext == ".mp3" else "audio/wav")
            os.remove(path)
            with session_scope() as s:
                row = s.get(AudioTrack, t.id)
                row.file_key, row.duration_s = key, dur

    # 2) Dudak senkronu — anahtar seçilen varyasyona ve diyaloğa bağlı (içerik-adresli)
    out_key = f"lipsync/{scene_id}/{vid}_{fp.dialogue}.mp4"
    if not storage.exists(out_key):
        _set(scene_id, status="lipsync")
        if settings.dry_run or not any(t.kind == "dialogue" for t in tracks):
            storage.copy(video_key, out_key)                 # ücretsiz mod: video olduğu gibi
        else:
            _live_lipsync(scene, tracks, video_key, out_key)
    _set(scene_id, lipsync_video_key=out_key, dialogue_hash=fp.dialogue)
    return out_key


def _live_lipsync(scene, tracks, video_key, out_key):
    """ÜCRETLİ: diyalogları tek kanala miksler, fal lip-sync'e gönderir."""
    from app.live.fal_video import lipsync
    tmp = []
    try:
        video = storage.download(video_key, _tmp(".mp4")); tmp.append(video)
        dialogue = []
        for t in tracks:
            if t.kind == "dialogue" and t.file_key:
                p = storage.download(t.file_key, _tmp(os.path.splitext(t.file_key)[1])); tmp.append(p)
                dialogue.append(AudioInput(p, t.start_offset_s, t.gain_db))
        voice_only = mux(video, dialogue, [], _tmp(".mp4")); tmp.append(voice_only)
        est = pricing.estimate_lipsync(scene.duration_s)
        with killswitch.spend(str(scene.project_id), est, "fal:lipsync", scene_id=scene.id) as charge:
            out = lipsync(video, voice_only, _tmp(".mp4")); tmp.append(out)
            charge.actual = est
        storage.put_file(out, out_key)
    finally:
        for f in tmp:
            if os.path.exists(f):
                os.remove(f)


def mux_scene(scene_id: str) -> str:
    scene, tracks, fp, cfg = _state(scene_id)
    if scene.status in ("frozen", "needs_review") or not scene.lipsync_video_key:
        return f"skip ({scene.status})"

    mux_hash = stable_hash([scene.lipsync_video_key, fp.dialogue, fp.bed, cfg["loudness_lufs"]])
    out_key = f"final/{scene_id}/{mux_hash}.mp4"

    if not storage.exists(out_key):
        _set(scene_id, status="muxing")
        tmp_files = []
        try:
            video = storage.download(scene.lipsync_video_key, _tmp(".mp4")); tmp_files.append(video)
            dialogue, bed = [], []
            for t in tracks:
                if t.kind == "dialogue":
                    if not t.file_key:
                        continue
                    p = storage.download(t.file_key, _tmp(os.path.splitext(t.file_key)[1])); tmp_files.append(p)
                    dialogue.append(AudioInput(p, t.start_offset_s, t.gain_db))
                else:
                    if t.file_key:
                        p = storage.download(t.file_key, _tmp(os.path.splitext(t.file_key)[1]))
                    else:                                       # ücretsiz prosedürel müzik
                        p = synthesize_music(_tmp(".wav"), (t.params or {}).get("mood") or scene.emotion,
                                             duration=max(10.0, scene.duration_s + 1))
                    tmp_files.append(p)
                    bed.append(AudioInput(p, t.start_offset_s, t.gain_db))
            out = mux(video, dialogue, bed, _tmp(".mp4"), cfg["loudness_lufs"]); tmp_files.append(out)
            storage.put_file(out, out_key)
        finally:
            for f in tmp_files:
                if os.path.exists(f):
                    os.remove(f)

    _set(scene_id, final_video_key=out_key, bed_hash=fp.bed, mux_hash=mux_hash, status="done")
    return out_key
