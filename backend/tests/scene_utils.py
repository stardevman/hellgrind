"""Testler arası paylaşılan sahne yardımcıları (veritabanı ve nesne deposu gerektirir)."""
import uuid

from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.core.rerender import plan_rerender
from app.core.state import load_assets, load_characters
from app.db import session_scope
from app.models import AudioTrack, Project, Scene, Variation
from app.style import bible


def load_scenes(pid):
    """{scene_code: (scene, karakterler, kanallar)} — hepsi veritabanından okunur (oturumdan koparılmış)."""
    with session_scope() as s:
        out = {}
        for sc in s.scalars(select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order)):
            tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)
                                    .order_by(AudioTrack.start_offset_s, AudioTrack.kind)))
            out[sc.scene_code] = (sc, list(load_characters(s, sc.id)), tracks)
        s.expunge_all()
        return out


def style_version(pid):
    with session_scope() as s:
        return bible.active_version(s, pid)


def lut_hash(pid):
    """Film geneli renk girdisi (LUT + kare tutma), üretim koduyla aynı biçimde."""
    from app.core.hashing import film_grade_token
    with session_scope() as s:
        p = s.get(Project, pid)
        return film_grade_token(p.lut_hash, p.settings)


def scene_assets(pid):
    """{scene_code: varlıklar}: görsel özete giren bağlı varlıklar (üretim kodu da aynı yükleyiciyi kullanır)."""
    with session_scope() as s:
        out = {sc.scene_code: list(load_assets(s, sc.id)) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        s.expunge_all()
        return out


def plans(pid):
    assets, style, lut = scene_assets(pid), style_version(pid), lut_hash(pid)
    return {code: plan_rerender(sc, chars, tracks, assets[code], style, lut)[0]
            for code, (sc, chars, tracks) in load_scenes(pid).items()}


def mark_finished(pid, status="done"):
    """Her sahneyi, şimdiki girdilerle başarıyla render edilmiş gibi işaretler (özetler ve anahtarlar yazılır)."""
    assets, style, lut = scene_assets(pid), style_version(pid), lut_hash(pid)
    for code, (sc, chars, tracks) in load_scenes(pid).items():
        _, fp = plan_rerender(sc, chars, tracks, assets[code], style, lut)
        with session_scope() as s:
            s.execute(text("""UPDATE scenes SET visual_hash = :v, dialogue_hash = :d, bed_hash = :b, status = CAST(:st AS scene_status),
                              lipsync_video_key = 'l.mp4', final_video_key = 'f.mp4' WHERE id = :i"""),
                      {"v": fp.visual, "d": fp.dialogue, "b": fp.bed, "st": status, "i": sc.id})


def add_variation(sid, score=0.9, *, tier="full", status="passed", rank=None, run="run-1", file=True, **params):
    """Bir varyasyon satırı ve (istenirse) depoda küçük bir video dosyası. (kimlik, anahtar) döndürür."""
    vid = uuid.uuid4()
    key = f"variations/{sid}/{vid}.mp4" if file else None
    if key:
        storage.client().put_object(Bucket=settings.s3_bucket, Key=key, Body=b"video")
    with session_scope() as s:
        s.add(Variation(id=vid, scene_id=sid, tier=tier, engine="fal_wan", seed=vid.int % 10**9, prompt_revision=0,
                        status=status, composite_score=score, clip_score=score, video_key=key, refined_rank=rank,
                        anomalies=[], params={"run": run, "resolution": "720p", **params}))
    return str(vid), key


def scene_row(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        s.expunge(sc)
        return sc


def track_rows(sid, active=True):
    """Kanallar; active=False tüm sürümleri (pasifler dahil) getirir."""
    with session_scope() as s:
        q = select(AudioTrack).where(AudioTrack.scene_id == sid).order_by(AudioTrack.start_offset_s, AudioTrack.kind)
        rows = list(s.scalars(q.where(AudioTrack.is_active) if active else q))
        s.expunge_all()
        return rows


def add_scene_music(pid, gain_db=-8.0):
    """Faz 11 öncesi ingest'in her çekime eklediği prosedürel müzik: Bölüm A testleri sahne düzeyindeki müziği sınar
    (Faz 11'den beri müzik sekans düzeyindedir, sahne müziği isteğe bağlıdır)."""
    from app.hermes.ingest import music_hash
    with session_scope() as s:
        for sc in s.scalars(select(Scene).where(Scene.project_id == pid)):
            s.add(AudioTrack(scene_id=sc.id, kind="music", source="procedural", params={"mood": sc.emotion},
                             content_hash=music_hash(sc.emotion), start_offset_s=0.0, gain_db=gain_db,
                             is_active=True, version=1))
