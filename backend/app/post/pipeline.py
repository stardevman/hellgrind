"""Post-prodüksiyon: Stateless Re-render'ın LIPSYNC, GRADE ve MUX aşamaları.

Ücretsiz modda: espeak-ng ile konuşma, prosedürel müzik, dudak senkronu atlanır (rafine video olduğu gibi kopyalanır).
Çıktılar içerik-adreslidir; aynı girdi aynı anahtarı üretir ve dosya zaten varsa iş atlanır:
  audio/{motor}/{content_hash}.wav          diyalog sesi (aynı replik sahneler arasında paylaşılır)
  audio/music/{duygu}_{süre}s.wav           prosedürel müzik
  lipsync/{sahne}/{variation_id}[_r{revizyon}]_{dialogue_hash}.mp4   (rafine #1 elle düzeltildiyse revizyon kimliğiyle)
  graded/{sahne}/{özet}.mp4                 renk ayarlı video (LUT + çekim ayarı; etkisizse yok)
  final/{sahne}/{mux_hash}.mp4
"""
import logging
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select

from app import storage
from app.budget import pricing
from app.config import project_settings, settings
from app.core.hashing import film_grade_token, stable_hash
from app.core.rerender import Fingerprints, fingerprints
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine import paid
from app.models import AudioTrack, Character, Project, Revision, Scene, Variation
from app.post import grade
from app.post.audio import synthesize, synthesize_music
from app.post.mux import AudioInput, mix_audio, mux
from app.quality.anomaly import probe_duration
from app.style import bible

log = logging.getLogger("hellgrind.post")

SKIP_STATUSES = ("frozen", "needs_review", "blocked")     # bu durumdaki sahnede post aşaması çalışmaz
MUSIC_BUCKET_S = 5                             # müzik dosyası bu adımlarla yuvarlanır: sahneler arasında paylaşılır


@dataclass
class Snapshot:
    scene: Scene
    tracks: list[AudioTrack]       # aktif kanallar, başlangıç ofsetine göre sıralı
    fp: Fingerprints
    cfg: dict
    lut_key: str | None = None


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
        project = s.get(Project, scene.project_id)
        fp = fingerprints(scene, load_characters(s, scene_id), tracks, load_assets(s, scene_id),
                          bible.active_version(s, scene.project_id),
                          film_grade_token(project.lut_hash, project.settings))
        cfg = project_settings(project.settings)
        lut_key = project.lut_key
        s.expunge_all()
        return Snapshot(scene, tracks, fp, cfg, lut_key)


def _rank_one(scene_id: str) -> tuple[str, str] | None:
    """Rafine #1: (kaynak kimliği, video anahtarı). Etkin bir elle düzeltme varsa onun videosu kullanılır ve kimlik
    revizyonu da içerir; böylece revizyon yeni bir dudak senkronu (ve montaj) anahtarı üretir."""
    with session_scope() as s:
        v = s.scalars(select(Variation).where(Variation.scene_id == scene_id, Variation.refined_rank == 1)).first()
        if v is None:
            return None
        rev = s.get(Revision, v.revision_id) if v.revision_id else None
        return (f"{v.id}_r{rev.id}", rev.video_key) if rev else (str(v.id), v.video_key)


def keeps_real_length(params: dict | None) -> bool:
    """Referans modu klipleri (bütün sahne parçasından kesilen segment, onun tamamlanmış ya da büyütülmüş kopyası,
    çekim başına referans isteği) gerçek uzunluğuyla montaja girer: duration_s yalnızca plandır, kırpılmaz."""
    params = params or {}
    return "segment" in params or params.get("input_mode") == "reference"


def _real_length(scene: Scene) -> float | None:
    """Rafine #1 bir referans modu kliğiyse gerçek uzunluğu (sn), değilse None."""
    with session_scope() as s:
        v = s.scalars(select(Variation).where(Variation.scene_id == scene.id, Variation.refined_rank == 1)).first()
        if v is not None and keeps_real_length(v.params):
            return float((v.params or {}).get("duration_s") or scene.duration_s or 0)
    return None


def trim_target(scene: Scene) -> float:
    """Dudak senkronu öncesi kırpma hedefi; 0 = kırpma yok (referans modu klipleri, keeps_real_length)."""
    return 0.0 if _real_length(scene) is not None else float(scene.duration_s or 0)


def _set_track(track_id, **fields) -> None:
    with session_scope() as s:
        row = s.get(AudioTrack, track_id)
        for key, value in fields.items():
            setattr(row, key, value)


# ---------- ücretli çağrılar (yalnızca DRY_RUN=0 ve anahtar tanımlıyken) ----------

def paid_audio(cfg: dict | None) -> bool:
    """Ses için ücretli servis (ElevenLabs, fal dudak senkronu) çağrılabilir mi: ücretli mod ve projenin `paid_audio`
    ayarı (deneme ön ayarında kapalı)."""
    return not settings.dry_run and bool((cfg or {}).get("paid_audio", True))


def paid_lipsync(cfg: dict | None) -> bool:
    """Dudak senkronu (fal.ai) çağrılır mı: ücretli ses açık ve fal.ai anahtarı tanımlı. Video MiniMax H3 ile kendi
    sunucumuzda üretildiğinde fal.ai isteğe bağlıdır; anahtarı yoksa video senkronsuz kullanılır."""
    return paid_audio(cfg) and paid.available("fal_key")


def local_tts_engine() -> str:
    """Yerel konuşma motoru: Okur'un model dosyaları varsa okur, yoksa espeak."""
    from app.post import okur
    return "okur" if okur.available(settings.quality_model_dir) else "espeak"


def effective_tts_engine(cfg: dict | None = None) -> str:
    """Seçilen motor; ücretsiz modda ya da projede ücretli ses kapalıyken "elevenlabs" yerine yerel motor. "okur"
    seçiliyken model dosyaları yoksa espeak (dosya anahtarı gerçekten kullanılan motoru gösterir)."""
    engine = settings.tts_engine
    if engine == "elevenlabs" and not paid_audio(cfg):
        return local_tts_engine()
    return local_tts_engine() if engine == "okur" else engine


def _paid_tts(t: AudioTrack, scene: Scene, path: str) -> float:
    from app.live.elevenlabs import tts
    with session_scope() as s:
        voice_id = s.get(Character, t.character_id).voice_id if t.character_id else None
    return paid.call(str(scene.project_id), pricing.estimate_tts(t.text or ""), "elevenlabs", "elevenlabs_api_key",
                     lambda: tts(t.text or "", voice_id, t.emotion or scene.emotion, path), scene_id=scene.id)


# ---------- ses dosyaları ----------

def voice_seed(character_id) -> int:
    """Karakter kimliğinin özetinden: her karakterin sesi her seferinde aynı kalır."""
    return int(stable_hash(str(character_id))[:6], 16) if character_id else 0


def ensure_dialogue_audio(t: AudioTrack, scene: Scene, tmp: str, cfg: dict | None = None) -> None:
    """Replik sesini içerik-adresli anahtara yazar (yoksa üretir) ve kanal satırına bağlar."""
    if t.source != "tts":                                           # kullanıcının yüklediği ses: hazır olmalı
        if not t.file_key:
            raise RuntimeError(f"diyalog kanalı {t.id} için ses dosyası yok")
        return
    engine = effective_tts_engine(cfg)
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
                                  voice_seed=voice_seed(t.character_id), engine=engine,
                                  model_dir=settings.quality_model_dir)
        storage.put_file(path, key, "audio/mpeg" if ext == ".mp3" else "audio/wav")
    _set_track(t.id, file_key=key, duration_s=duration)


def music_file(mood: str, at_least_s: float, path: str) -> tuple[str, int]:
    """En az `at_least_s` saniyelik prosedürel müzik, içerik-adresli: audio/music/{duygu}_{süre}s.wav. Süre
    MUSIC_BUCKET_S adımlarıyla yuvarlanır; böylece sahneler ve sekanslar aynı dosyayı paylaşır. (anahtar, süre)."""
    seconds = math.ceil(at_least_s / MUSIC_BUCKET_S) * MUSIC_BUCKET_S
    key = f"audio/music/{mood}_{seconds}s.wav"
    if storage.exists(key):
        storage.download(key, path)
    else:
        synthesize_music(path, mood, seconds)
        storage.put_file(path, key, "audio/wav")
    return key, seconds


def ensure_bed_audio(t: AudioTrack, scene: Scene, tmp: str) -> str:
    """Fon kanalının yerel dosyası. Prosedürel müzik içerik-adresli anahtardan gelir (yoksa üretilir)."""
    path = os.path.join(tmp, f"{t.id}.wav")
    if t.source == "procedural":
        mood = (t.params or {}).get("mood") or t.emotion or scene.emotion
        key, seconds = music_file(mood, max(float(scene.duration_s or 0), _real_length(scene) or 0) + 1, path)
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
                ensure_dialogue_audio(t, scene, tmp, snap.cfg)

        # Ücretli modda senkronsuz kopya ayrı anahtardadır: ücretli ses sonradan açılınca senkron gerçekten yapılır.
        sync = paid_lipsync(snap.cfg)
        unsynced = "" if settings.dry_run or sync else "_unsynced"
        keep_end = _continues(scene)
        target = trim_target(scene)
        trim = f"_d{target:g}{'e' if keep_end else 's'}" if target else "_full"
        out_key = f"lipsync/{scene_id}/{variation_id}_{snap.fp.dialogue}{trim}{unsynced}.mp4"
        if not storage.exists(out_key):
            advance_scene(project_id, scene_id, status="lipsync")
            _lipsync(scene, video_key, out_key, tmp, sync=sync, keep_end=keep_end, target=target)
    fields = dict(lipsync_video_key=out_key, dialogue_hash=snap.fp.dialogue)
    if scene.lipsync_video_key != out_key:     # kaynak video değişti: eski renk ve montaj geçersiz, plan yeniden yapsın
        fields.update(final_video_key=None, graded_video_key=None, grade_hash=None)
    advance_scene(project_id, scene_id, **fields)
    return out_key


TRIM_TOLERANCE_S = 1.5 / 24      # bundan kısa fazlalık kesilmez


def _continues(scene: Scene) -> bool:
    """Sonraki çekim continuous mu (klibin son karesi sonraki çekimin ilk karesine varır: kesme baştan yapılır)."""
    from app.core.chain import next_continuous
    with session_scope() as s:
        return next_continuous(s, s.get(Scene, scene.id)) is not None


def trim_window(duration: float, target: float, keep_end: bool) -> tuple[float, float] | None:
    """MiniMax H3 kare sayısını 17n+5'e yukarı yuvarlar (4 sn -> 107 kare, 4,46 sn): montajda hedef süreye kesilir.
    (başlangıç, uzunluk) ya da None (kesme gerekmez). keep_end: continuous çekimde varış ucu korunur, baştan kesilir."""
    if not target or duration <= target + TRIM_TOLERANCE_S:
        return None
    return (round(duration - target, 3) if keep_end else 0.0), round(target, 3)


def trimmed(video: str, target: float, keep_end: bool, out: str) -> str:
    """Hedef süreye kesilmiş kopya (yeniden kodlanır; kare hassas). Kesme gerekmezse kaynak döner."""
    window = trim_window(probe_duration(video), target, keep_end)
    if window is None:
        return video
    start, length = window
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", video, "-t", f"{length:.3f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", out], check=True, capture_output=True)
    return out


def _lipsync(scene: Scene, video_key: str, out_key: str, tmp: str, sync: bool = True, keep_end: bool = False,
             target: float | None = None) -> None:
    """Ücretsiz modda, projede ücretli ses kapalıyken ya da senkronlanacak konuşma yoksa rafine video olduğu gibi
    kopyalanır; aksi halde fal'a gider. Klip hedef süreden uzunsa önce kesilir (trim_window)."""
    with session_scope() as s:                     # ses dosyaları az önce bağlandı: anlık görüntü bayat, yeniden oku
        dialogue = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == scene.id, AudioTrack.is_active,
                                                           AudioTrack.kind == "dialogue").order_by(AudioTrack.start_offset_s)))
        s.expunge_all()
    source = storage.download(video_key, os.path.join(tmp, "video.mp4"))
    target = float(scene.duration_s or 0) if target is None else target
    video = trimmed(source, target, keep_end, os.path.join(tmp, "trimmed.mp4"))
    if settings.dry_run or not sync or not dialogue:
        if video == source:
            storage.copy(video_key, out_key)
        else:
            storage.put_file(video, out_key)
        return
    from app.live.fal_video import lipsync
    duration = probe_duration(video)
    voices = [AudioInput(storage.download(t.file_key, os.path.join(tmp, f"{t.id}{os.path.splitext(t.file_key)[1]}")),
                         t.start_offset_s, t.gain_db) for t in dialogue]
    voice = mix_audio(voices, os.path.join(tmp, "voice.wav"), duration)
    out = os.path.join(tmp, "lipsynced.mp4")
    paid.call(str(scene.project_id), pricing.estimate_lipsync(duration), "fal:lipsync", "fal_key",
              lambda: lipsync(video, voice, out), scene_id=scene.id)
    storage.put_file(out, out_key)


# ---------- GRADE ----------

def grade_scene(scene_id: str) -> str:
    """Dudak senkronlu videoya film geneli LUT'u, çekimin renk ayarını ve (proje ayarıysa) kare tutmayı uygular. Etkisizse
    ayarı temizler (montaj dudak senkronlu videoyu kullanır). Çıktı içerik-adreslidir. Dönen: anahtar, "identity" ya da "skip (...)"."""
    snap = _snapshot(scene_id)
    scene, project_id = snap.scene, str(snap.scene.project_id)
    if scene.status in SKIP_STATUSES:
        return f"skip ({scene.status})"
    if not scene.lipsync_video_key:
        return "skip (dudak senkronlu video yok)"
    if snap.fp.grade is None:
        advance_scene(project_id, scene_id, graded_video_key=None, grade_hash=None)
        return "identity"
    out_key = f"graded/{scene_id}/{stable_hash([scene.lipsync_video_key, snap.fp.grade])}.mp4"
    if not storage.exists(out_key):
        advance_scene(project_id, scene_id, status="grading")
        with workspace() as tmp:
            video = storage.download(scene.lipsync_video_key, os.path.join(tmp, "video.mp4"))
            lut = storage.download(snap.lut_key, os.path.join(tmp, "film.cube")) if snap.lut_key else None
            out = grade.grade_video(video, os.path.join(tmp, "graded.mp4"), scene.grade, lut,
                                    on_twos=bool(snap.cfg.get("on_twos")))
            storage.put_file(out, out_key)
    advance_scene(project_id, scene_id, graded_video_key=out_key, grade_hash=snap.fp.grade)
    return out_key


def picture_of(scene: Scene) -> str | None:
    """Montajın görüntü kaynağı: renk ayarlı video, yoksa dudak senkronlu video."""
    return scene.graded_video_key or scene.lipsync_video_key


# ---------- MUX ----------

def mux_scene(scene_id: str) -> str:
    """Video, diyaloglar ve fonu tek dosyada birleştirir. Dönen: anahtar ya da "skip (...)"."""
    snap = _snapshot(scene_id)
    scene, project_id = snap.scene, str(snap.scene.project_id)
    if scene.status in SKIP_STATUSES:
        return f"skip ({scene.status})"
    if not scene.lipsync_video_key:
        return "skip (dudak senkronlu video yok)"

    source = picture_of(scene)
    mux_hash = stable_hash([source, snap.fp.dialogue, snap.fp.bed, snap.cfg["loudness_lufs"]])
    out_key = f"final/{scene_id}/{mux_hash}.mp4"
    if not storage.exists(out_key):
        advance_scene(project_id, scene_id, status="muxing")
        with workspace() as tmp:
            video = storage.download(source, os.path.join(tmp, "video.mp4"))
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
    draft = {"draft_video_key": out_key} if scene.cut_tier == "draft" else {}       # kaba kurgu bu klibi kullanır
    advance_scene(project_id, scene_id, final_video_key=out_key, bed_hash=snap.fp.bed, mux_hash=mux_hash, status="done",
                  **draft)
    return out_key
