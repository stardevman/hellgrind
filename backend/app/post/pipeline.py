"""Post-prodüksiyon: Stateless Re-render'ın LIPSYNC ve MUX aşamaları.

Ücretsiz modda: espeak-ng ile konuşma, prosedürel müzik, dudak senkronu atlanır (rafine video olduğu gibi kopyalanır).
Çıktılar içerik-adreslidir; aynı girdi aynı anahtarı üretir ve dosya zaten varsa iş atlanır:
  audio/{motor}/{content_hash}.wav          diyalog sesi (aynı replik sahneler arasında paylaşılır)
  audio/music/{duygu}_{süre}s.wav           prosedürel müzik
  lipsync/{sahne}/{variation_id}_{dialogue_hash}.mp4
  final/{sahne}/{mux_hash}.mp4
"""
import logging
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select

from app import storage
from app.budget import killswitch, pricing
from app.config import project_settings, settings
from app.core.hashing import stable_hash
from app.core.rerender import Fingerprints, fingerprints
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine.providers import TransientError
from app.models import AudioTrack, Character, Project, Scene, Variation
from app.post.audio import synthesize, synthesize_music
from app.post.mux import AudioInput, mix_audio, mux
from app.quality.anomaly import probe_duration
from app.style import bible

log = logging.getLogger("hellgrind.post")

MAX_PAID_ATTEMPTS = 3                          # geçici sağlayıcı hatalarında ücretli çağrı başına deneme
SKIP_STATUSES = ("frozen", "needs_review", "blocked")     # bu durumdaki sahnede post aşaması çalışmaz
MUSIC_BUCKET_S = 5                             # müzik dosyası bu adımlarla yuvarlanır: sahneler arasında paylaşılır


@dataclass
class Snapshot:
    scene: Scene
    tracks: list[AudioTrack]       # aktif kanallar, başlangıç ofsetine göre sıralı
    fp: Fingerprints
    cfg: dict


@contextmanager
def workspace():
    """Aşamaya özel geçici klasör; ne olursa olsun silinir."""
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as d:
        yield d


def _snapshot(scene_id: str) -> Snapshot:
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene_id, AudioTrack.is_active)
                                .order_by(AudioTrack.start_offset_s, AudioTrack.kind)))
        fp = fingerprints(scene, load_characters(s, scene_id), tracks, load_assets(s, scene_id),
                          bible.active_version(s, scene.project_id))
        cfg = project_settings(s.get(Project, scene.project_id).settings)
        s.expunge_all()
        return Snapshot(scene, tracks, fp, cfg)


def _rank_one(scene_id: str) -> tuple[str, str] | None:
    with session_scope() as s:
        v = s.scalars(select(Variation).where(Variation.scene_id == scene_id, Variation.refined_rank == 1)).first()
        return (str(v.id), v.video_key) if v else None


def _set_track(track_id, **fields) -> None:
    with session_scope() as s:
        row = s.get(AudioTrack, track_id)
        for key, value in fields.items():
            setattr(row, key, value)


# ---------- ücretli çağrılar (yalnızca DRY_RUN=0 ve anahtar tanımlıyken) ----------

def effective_tts_engine() -> str:
    """Ücretsiz modda "elevenlabs" seçilmiş olsa bile espeak kullanılır: ücretli çağrı hiçbir koşulda sızmaz."""
    return "espeak" if settings.tts_engine == "elevenlabs" and settings.dry_run else settings.tts_engine


def _spend_retrying(project_id: str, scene_id, estimate: float, label: str, key_name: str, call):
    """Ücretli çağrı: guard (engelliyse deftere iz bırakmadan durur), sonra her deneme kendi rezervasyonunu alır ve başarısızsa
    iade eder. Yalnızca geçici hatalar (429, 5xx, zaman aşımı) yeniden denenir."""
    from app.live import guard                  # ücretli kilit: yalnızca fonksiyon içinde
    guard(key_name)
    for attempt in range(1, MAX_PAID_ATTEMPTS + 1):
        try:
            with killswitch.spend(project_id, estimate, label, scene_id=scene_id) as charge:
                result = call()
                charge.actual = estimate
            return result
        except TransientError as e:
            log.warning("%s geçici hata (deneme %d/%d): %s", label, attempt, MAX_PAID_ATTEMPTS, e)
    raise TransientError(f"{label}: {MAX_PAID_ATTEMPTS} denemede de başarısız")


def _paid_tts(t: AudioTrack, scene: Scene, path: str) -> float:
    from app.live.elevenlabs import tts
    with session_scope() as s:
        voice_id = s.get(Character, t.character_id).voice_id if t.character_id else None
    return _spend_retrying(str(scene.project_id), scene.id, pricing.estimate_tts(t.text or ""), "elevenlabs",
                           "elevenlabs_api_key", lambda: tts(t.text or "", voice_id, t.emotion or scene.emotion, path))


# ---------- ses dosyaları ----------

def voice_seed(character_id) -> int:
    """Karakter kimliğinin özetinden: her karakterin sesi her seferinde aynı kalır."""
    return int(stable_hash(str(character_id))[:6], 16) if character_id else 0


def ensure_dialogue_audio(t: AudioTrack, scene: Scene, tmp: str) -> None:
    """Replik sesini içerik-adresli anahtara yazar (yoksa üretir) ve kanal satırına bağlar."""
    if t.source != "tts":                                           # kullanıcının yüklediği ses: hazır olmalı
        if not t.file_key:
            raise RuntimeError(f"diyalog kanalı {t.id} için ses dosyası yok")
        return
    engine = effective_tts_engine()
    ext = ".mp3" if engine == "elevenlabs" else ".wav"
    key = f"audio/{engine}/{t.content_hash}{ext}"
    if t.file_key == key and t.duration_s and storage.exists(key):
        return
    path = os.path.join(tmp, f"{t.id}{ext}")
    if storage.exists(key):                                         # başka sahne ya da önceki çalıştırma üretmiş
        storage.download(key, path)
        duration = round(probe_duration(path), 2)
    else:
        if engine == "elevenlabs":
            duration = _paid_tts(t, scene, path)
        else:
            duration = synthesize(t.text or "", path, emotion=t.emotion or scene.emotion,
                                  voice_seed=voice_seed(t.character_id), engine=engine)
        storage.put_file(path, key, "audio/mpeg" if ext == ".mp3" else "audio/wav")
    _set_track(t.id, file_key=key, duration_s=duration)


def ensure_bed_audio(t: AudioTrack, scene: Scene, tmp: str) -> str:
    """Fon kanalının yerel dosyası. Prosedürel müzik içerik-adresli anahtardan gelir (yoksa üretilir)."""
    path = os.path.join(tmp, f"{t.id}.wav")
    if t.source == "procedural":
        mood = (t.params or {}).get("mood") or t.emotion or scene.emotion
        seconds = math.ceil((scene.duration_s + 1) / MUSIC_BUCKET_S) * MUSIC_BUCKET_S
        key = f"audio/music/{mood}_{seconds}s.wav"
        if storage.exists(key):
            storage.download(key, path)
        else:
            synthesize_music(path, mood, seconds)
            storage.put_file(path, key, "audio/wav")
        if t.file_key != key:
            _set_track(t.id, file_key=key, duration_s=seconds)
        return path
    if not t.file_key:
        raise RuntimeError(f"{t.kind} kanalı {t.id} için ses dosyası yok")
    return storage.download(t.file_key, os.path.join(tmp, f"{t.id}{os.path.splitext(t.file_key)[1]}"))


# ---------- LIPSYNC ----------

def voice_and_lipsync(scene_id: str) -> str:
    """Eksik diyalog seslerini üretir ve rafine videodan dudak senkronlu videoyu çıkarır. Dönen: anahtar ya da "skip (...)"."""
    snap = _snapshot(scene_id)
    scene, project_id = snap.scene, str(snap.scene.project_id)
    if scene.status in SKIP_STATUSES:
        return f"skip ({scene.status})"
    best = _rank_one(scene_id)
    if best is None:
        return "skip (rafine video yok)"
    variation_id, video_key = best

    advance_scene(project_id, scene_id, status="audio")
    with workspace() as tmp:
        for t in snap.tracks:
            if t.kind == "dialogue":
                ensure_dialogue_audio(t, scene, tmp)

        out_key = f"lipsync/{scene_id}/{variation_id}_{snap.fp.dialogue}.mp4"
        if not storage.exists(out_key):
            advance_scene(project_id, scene_id, status="lipsync")
            _lipsync(scene, video_key, out_key, tmp)
    fields = dict(lipsync_video_key=out_key, dialogue_hash=snap.fp.dialogue)
    if scene.lipsync_video_key != out_key:
        fields["final_video_key"] = None       # kaynak video değişti: eski montaj artık geçersiz, plan yeniden montajlasın
    advance_scene(project_id, scene_id, **fields)
    return out_key


def _lipsync(scene: Scene, video_key: str, out_key: str, tmp: str) -> None:
    """Ücretsiz modda ya da senkronlanacak konuşma yoksa rafine video olduğu gibi kopyalanır; ücretli modda fal'a gider."""
    with session_scope() as s:                     # ses dosyaları az önce bağlandı: anlık görüntü bayat, yeniden oku
        dialogue = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene.id, AudioTrack.is_active,
                                                           AudioTrack.kind == "dialogue").order_by(AudioTrack.start_offset_s)))
        s.expunge_all()
    if settings.dry_run or not dialogue:
        storage.copy(video_key, out_key)
        return
    from app.live.fal_video import lipsync
    video = storage.download(video_key, os.path.join(tmp, "video.mp4"))
    duration = probe_duration(video)
    voices = [AudioInput(storage.download(t.file_key, os.path.join(tmp, f"{t.id}{os.path.splitext(t.file_key)[1]}")),
                         t.start_offset_s, t.gain_db) for t in dialogue]
    voice = mix_audio(voices, os.path.join(tmp, "voice.wav"), duration)
    out = os.path.join(tmp, "lipsynced.mp4")
    _spend_retrying(str(scene.project_id), scene.id, pricing.estimate_lipsync(duration), "fal:lipsync", "fal_key",
                    lambda: lipsync(video, voice, out))
    storage.put_file(out, out_key)


# ---------- MUX ----------

def mux_scene(scene_id: str) -> str:
    """Video, diyaloglar ve fonu tek dosyada birleştirir. Dönen: anahtar ya da "skip (...)"."""
    snap = _snapshot(scene_id)
    scene, project_id = snap.scene, str(snap.scene.project_id)
    if scene.status in SKIP_STATUSES:
        return f"skip ({scene.status})"
    if not scene.lipsync_video_key:
        return "skip (dudak senkronlu video yok)"

    mux_hash = stable_hash([scene.lipsync_video_key, snap.fp.dialogue, snap.fp.bed, snap.cfg["loudness_lufs"]])
    out_key = f"final/{scene_id}/{mux_hash}.mp4"
    if not storage.exists(out_key):
        advance_scene(project_id, scene_id, status="muxing")
        with workspace() as tmp:
            video = storage.download(scene.lipsync_video_key, os.path.join(tmp, "video.mp4"))
            dialogue, bed = [], []
            for t in snap.tracks:
                if t.kind == "dialogue":
                    if not t.file_key:         # sessizce atlamak, replikleri eksik bir film üretirdi
                        raise RuntimeError(f"diyalog kanalı {t.id} seslendirilmemiş; önce LIPSYNC aşaması çalışmalı")
                    path = storage.download(t.file_key, os.path.join(tmp, f"{t.id}{os.path.splitext(t.file_key)[1]}"))
                    dialogue.append(AudioInput(path, t.start_offset_s, t.gain_db))
                else:
                    bed.append(AudioInput(ensure_bed_audio(t, scene, tmp), t.start_offset_s, t.gain_db))
            out = mux(video, dialogue, bed, os.path.join(tmp, "final.mp4"), snap.cfg["loudness_lufs"])
            storage.put_file(out, out_key)
    advance_scene(project_id, scene_id, final_video_key=out_key, bed_hash=snap.fp.bed, mux_hash=mux_hash, status="done")
    return out_key
