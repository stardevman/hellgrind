"""Testler arası paylaşılan sahne yardımcıları (veritabanı ve nesne deposu gerektirir)."""
import uuid

from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.core.rerender import plan_rerender
from app.core.state import load_assets, load_characters
from app.db import session_scope
from app.models import AudioTrack, Scene, Variation


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


def scene_assets(pid):
    """{scene_code: varlıklar}: görsel özete giren bağlı varlıklar (üretim kodu da aynı yükleyiciyi kullanır)."""
    with session_scope() as s:
        out = {sc.scene_code: list(load_assets(s, sc.id)) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        s.expunge_all()
        return out


def plans(pid):
    assets = scene_assets(pid)
    return {code: plan_rerender(sc, chars, tracks, assets[code])[0] for code, (sc, chars, tracks) in load_scenes(pid).items()}


def mark_finished(pid, status="done"):
    """Her sahneyi, şimdiki girdilerle başarıyla render edilmiş gibi işaretler (özetler ve anahtarlar yazılır)."""
    assets = scene_assets(pid)
    for code, (sc, chars, tracks) in load_scenes(pid).items():
        _, fp = plan_rerender(sc, chars, tracks, assets[code])
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
