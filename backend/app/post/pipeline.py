"""Post-prodüksiyon aşamaları (Stateless Re-render'ın LIPSYNC ve MUX adımları)."""
import os
import shutil
import uuid

from sqlalchemy import select

from app import storage
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.core.rerender import fingerprints
from app.db import session_scope
from app.engine.iteration import load_characters
from app.models import AudioTrack, Project, Scene
from app.post.audio import synthesize_dry
from app.post.mux import AudioInput, mux
from app.quality import gate


def _tmp(ext: str) -> str:
    os.makedirs(settings.work_dir, exist_ok=True)
    return os.path.join(settings.work_dir, f"{uuid.uuid4()}{ext}")


def _state(scene_id):
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene_id, AudioTrack.is_active)).all()
        fp = fingerprints(scene, load_characters(s, scene_id), tracks)
        project = s.get(Project, scene.project_id)
        cfg = {**DEFAULT_PROJECT_SETTINGS, **(project.settings or {})}
        return scene, tracks, fp, cfg


def voice_and_lipsync(scene_id: str) -> str:
    scene, tracks, fp, _ = _state(scene_id)
    if scene.status in ("frozen", "needs_review"):
        return f"skip ({scene.status})"
    video_key = gate.best_refined_key(scene_id)
    if video_key is None:
        return "skip (rafine video yok)"

    # 1) Eksik diyalog seslerini üret (Kademe 1: sahte TTS)
    for t in tracks:
        if t.kind == "dialogue" and not t.file_key:
            path = _tmp(".wav")
            dur = synthesize_dry(t.text or "", path)
            key = f"audio/{scene_id}/{t.id}.wav"
            storage.put_file(path, key, "audio/wav")
            os.remove(path)
            with session_scope() as s:
                row = s.get(AudioTrack, t.id)
                row.file_key, row.duration_s = key, dur

    # 2) Dudak senkronu (Kademe 1: video aynen kopyalanır; Kademe 2'de fal lip-sync)
    out_key = f"lipsync/{scene_id}/{fp.visual}_{fp.dialogue}.mp4"     # içerik-adresli
    if not storage.exists(out_key):
        storage.copy(video_key, out_key)
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        sc.lipsync_video_key, sc.dialogue_hash, sc.status = out_key, fp.dialogue, "lipsync"
    return out_key


def mux_scene(scene_id: str) -> str:
    scene, tracks, fp, cfg = _state(scene_id)
    if scene.status in ("frozen", "needs_review") or not scene.lipsync_video_key:
        return f"skip ({scene.status})"

    from app.core.hashing import stable_hash
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
                    p = storage.download(t.file_key, _tmp(".wav")); tmp_files.append(p)
                    dialogue.append(AudioInput(p, t.start_offset_s, t.gain_db))
                else:
                    if t.file_key:
                        p = storage.download(t.file_key, _tmp(".wav")); tmp_files.append(p)
                    else:                                       # Kademe 1: örnek müzik
                        p = _tmp(".wav"); shutil.copyfile(os.path.join(settings.samples_dir, "music.wav"), p)
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


def _set(scene_id, **fields):
    with session_scope() as s:
        sc = s.get(Scene, scene_id)
        for k, v in fields.items():
            setattr(sc, k, v)
