"""LIPSYNC ve MUX aşamaları: gerçek PostgreSQL, MinIO, espeak-ng ve FFmpeg üzerinde.

Servisler yoksa bu dosyadaki testler atlanır.
"""
import json
import sys
import uuid

import pytest
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.core.hashing import stable_hash, visual_fingerprint
from app.core.rerender import fingerprints, plan_rerender, stage_names
from app.db import session_scope
from app.engine.iteration import load_characters
from app.engine.procedural import render_clip
from app.hermes.ingest import ingest
from app.models import AudioTrack, Scene
from app.post import pipeline
from tests.media_utils import loudness, pcm, probe, rms

SCRIPT = "İÇ. ODA - GECE\n\nKerem masaya oturur.\n\nKEREM\n- Burada kimse yok mu?\n"
TWO_LINES = SCRIPT + "\nKEREM\nHâlâ buradayım.\n"
TWO_LINES_SECOND_CHANGED = SCRIPT + "\nKEREM\nYarın görüşürüz.\n"       # aynı kelime sayısı: çekim süresi ve görsel değişmez


@pytest.fixture
def world(s3, mk_project, tmp_path, monkeypatch):
    """Rafine havuzu hazır, tek çekimlik bir sahne: gerçek klip depoda, diyalog + müzik kanalları ingest'ten."""
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tts_engine", "espeak")
    pid = mk_project()
    ingest(pid, SCRIPT)
    with session_scope() as s:
        sid = str(s.scalar(select(Scene.id).where(Scene.project_id == pid)))
    clip = render_clip(str(tmp_path / "pool.mp4"), seed=1, width=160, height=90, duration=4.0, fps=8,
                       emotion="tension", characters=["Kerem"])
    vid = str(uuid.uuid4())
    key = f"variations/{sid}/{vid}.mp4"
    storage.put_file(clip, key)
    with session_scope() as s:
        s.execute(text("""INSERT INTO variations (id, scene_id, tier, engine, seed, prompt_revision, status, video_key,
                                                   refined_rank, composite_score)
                          VALUES (:v, :s, 'full', 'fal_wan', 1, 0, 'refined', :k, 1, 0.95)"""), {"v": vid, "s": sid, "k": key})
        sc = s.get(Scene, sid)
        sc.status, sc.duration_s = "refined", 4.0
        sc.visual_hash = visual_fingerprint(sc, load_characters(s, sid))        # görsel aşama tamamlanmış gibi
    return NS(pid=pid, sid=sid, vid=vid, key=key, clip=clip)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def scene(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        return NS(status=sc.status, lipsync=sc.lipsync_video_key, final=sc.final_video_key, dialogue_hash=sc.dialogue_hash,
                  bed_hash=sc.bed_hash, mux_hash=sc.mux_hash, last_error=sc.last_error)


def tracks(sid, kind=None, active=True):
    with session_scope() as s:
        q = select(AudioTrack).where(AudioTrack.scene_id == sid)
        if active:
            q = q.where(AudioTrack.is_active)
        rows = [r for r in s.scalars(q.order_by(AudioTrack.start_offset_s, AudioTrack.id)).all()
                if kind is None or r.kind == kind]
        return [NS(id=str(t.id), kind=t.kind, text=t.text, file_key=t.file_key, duration_s=t.duration_s,
                   character_id=str(t.character_id) if t.character_id else None, params=t.params, source=t.source)
                for t in rows]


def plan(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        active = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sid, AudioTrack.is_active)).all()
        stages, _ = plan_rerender(sc, load_characters(s, sid), active)
    return stage_names(stages)


def fail_if_called(name):
    def boom(*a, **k):
        pytest.fail(f"{name} çağrılmamalıydı (iş zaten yapılmış)")
    return boom


# ---------- LIPSYNC ----------

def test_lipsync_generates_missing_voices_and_copies_the_video_in_free_mode(world):
    key = pipeline.voice_and_lipsync(world.sid)

    (dialogue,) = tracks(world.sid, "dialogue")
    sc = scene(world.sid)
    assert dialogue.file_key == f"audio/{world.sid}/{dialogue.id}.wav" and storage.exists(dialogue.file_key)
    assert dialogue.duration_s and dialogue.duration_s > 0.5                  # "- " ile başlayan satır da seslendirildi
    assert key == sc.lipsync == f"lipsync/{world.sid}/{world.vid}_{sc.dialogue_hash}.mp4"
    assert storage.read_bytes(key) == storage.read_bytes(world.key)           # ücretsiz mod: video olduğu gibi
    assert sc.status == "lipsync"


def test_lipsync_key_is_content_addressed_by_variation_and_dialogue(world):
    with session_scope() as s:
        sc = s.get(Scene, world.sid)
        fp = fingerprints(sc, load_characters(s, world.sid), s.scalars(select(AudioTrack).where(
            AudioTrack.scene_id == world.sid, AudioTrack.is_active)).all())
    assert pipeline.voice_and_lipsync(world.sid) == f"lipsync/{world.sid}/{world.vid}_{fp.dialogue}.mp4"


def test_lipsync_twice_does_no_new_work(world, monkeypatch):
    first = pipeline.voice_and_lipsync(world.sid)
    monkeypatch.setattr(pipeline, "synthesize", fail_if_called("synthesize"))
    monkeypatch.setattr(pipeline.storage, "copy", fail_if_called("storage.copy"))
    assert pipeline.voice_and_lipsync(world.sid) == first


def test_one_character_keeps_one_voice_across_lines(world, monkeypatch):
    ingest(world.pid, TWO_LINES)                                               # aynı karakterden ikinci replik
    seeds = []
    real = pipeline.synthesize
    monkeypatch.setattr(pipeline, "synthesize", lambda t, out, **kw: seeds.append(kw["voice_seed"]) or real(t, out, **kw))

    pipeline.voice_and_lipsync(world.sid)

    dialogue = tracks(world.sid, "dialogue")
    expected = int(stable_hash(dialogue[0].character_id)[:6], 16)
    assert len(dialogue) == 2 and seeds == [expected, expected]


def test_free_mode_never_uses_the_paid_voice_even_if_the_track_asks_for_it(world):
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET source = 'elevenlabs' WHERE scene_id = :s AND kind = 'dialogue'"),
                  {"s": world.sid})
    pipeline.voice_and_lipsync(world.sid)
    assert "app.live.elevenlabs" not in sys.modules
    assert tracks(world.sid, "dialogue")[0].file_key.endswith(".wav")


def test_scene_without_dialogue_still_gets_a_lipsync_video(world):
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET is_active = false WHERE scene_id = :s AND kind = 'dialogue'"), {"s": world.sid})
    assert storage.exists(pipeline.voice_and_lipsync(world.sid))


@pytest.mark.parametrize("status", ["frozen", "needs_review"])
def test_stages_skip_frozen_and_review_scenes(world, status):
    with session_scope() as s:
        s.get(Scene, world.sid).status = status
    assert pipeline.voice_and_lipsync(world.sid) == f"skip ({status})"
    assert pipeline.mux_scene(world.sid) == f"skip ({status})"
    assert scene(world.sid).lipsync is None and scene(world.sid).status == status


def test_lipsync_without_a_refined_video_is_skipped(world):
    with session_scope() as s:
        s.execute(text("UPDATE variations SET refined_rank = NULL WHERE scene_id = :s"), {"s": world.sid})
    assert pipeline.voice_and_lipsync(world.sid) == "skip (rafine video yok)"


def test_mux_without_a_lipsync_video_is_skipped(world):
    assert pipeline.mux_scene(world.sid).startswith("skip")
    assert scene(world.sid).final is None


# ---------- MUX ----------

def test_mux_writes_the_final_video_and_marks_the_scene_done(world):
    pipeline.voice_and_lipsync(world.sid)
    key = pipeline.mux_scene(world.sid)

    sc = scene(world.sid)
    with session_scope() as s:
        scn = s.get(Scene, world.sid)
        fp = fingerprints(scn, load_characters(s, world.sid), s.scalars(select(AudioTrack).where(
            AudioTrack.scene_id == world.sid, AudioTrack.is_active)).all())
    expected_hash = stable_hash([sc.lipsync, fp.dialogue, fp.bed, -23.0])
    assert key == sc.final == f"final/{world.sid}/{expected_hash}.mp4"
    assert (sc.status, sc.mux_hash, sc.bed_hash, sc.dialogue_hash) == ("done", expected_hash, fp.bed, fp.dialogue)


def test_final_video_has_speech_and_music_for_the_whole_shot(world, tmp_path):
    pipeline.voice_and_lipsync(world.sid)
    path = storage.download(pipeline.mux_scene(world.sid), str(tmp_path / "final.mp4"))

    streams, duration = probe(path)
    audio = [s for s in streams if s["codec_type"] == "audio"][0]
    assert [s["codec_type"] for s in streams] == ["video", "audio"] and abs(duration - 4.0) < 0.15
    assert float(audio["duration"]) == pytest.approx(duration, abs=0.15)      # ses akışı videoyu tamamen kapsar
    assert rms(pcm(path, 0.5, 1.5)) > 0.01 and rms(pcm(path, 3.0, 3.8)) > 0.005    # son replikten sonra müzik sürer


def test_project_loudness_setting_drives_the_final_mix(world, tmp_path):
    with session_scope() as s:
        s.execute(text("UPDATE projects SET settings = CAST(:c AS jsonb) WHERE id = :p"),
                  {"c": json.dumps({"loudness_lufs": -16}), "p": world.pid})
    pipeline.voice_and_lipsync(world.sid)
    key = pipeline.mux_scene(world.sid)

    final = storage.download(key, str(tmp_path / "final.mp4"))
    quiet_default = -23.0
    assert loudness(final) == pytest.approx(-16, abs=2) and abs(loudness(final) - quiet_default) > 4
    assert scene(world.sid).mux_hash == stable_hash([scene(world.sid).lipsync, scene(world.sid).dialogue_hash,
                                                     scene(world.sid).bed_hash, -16])


def test_mux_twice_does_no_new_work(world, monkeypatch):
    pipeline.voice_and_lipsync(world.sid)
    first = pipeline.mux_scene(world.sid)
    monkeypatch.setattr(pipeline, "mux", fail_if_called("mux"))
    assert pipeline.mux_scene(world.sid) == first and scene(world.sid).status == "done"


# ---------- Stateless Re-render ile uyum ----------

def test_planner_has_nothing_left_to_do_after_both_stages(world):
    assert plan(world.sid) == ["LIPSYNC", "MUX"]                               # görsel hazır, ses/montaj bekliyor
    pipeline.voice_and_lipsync(world.sid)
    pipeline.mux_scene(world.sid)
    assert plan(world.sid) == []


def test_changing_only_the_music_remuxes_without_voice_or_lipsync_work(world, monkeypatch):
    pipeline.voice_and_lipsync(world.sid)
    first_final, lipsync = pipeline.mux_scene(world.sid), scene(world.sid).lipsync
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET is_active = false WHERE scene_id = :s AND kind = 'music'"), {"s": world.sid})
        s.execute(text("""INSERT INTO audio_tracks (scene_id, kind, source, params, content_hash, gain_db, is_active, version)
                          VALUES (:s, 'music', 'procedural', CAST(:p AS jsonb), 'yeni-muzik', -4, true, 2)"""),
                  {"s": world.sid, "p": json.dumps({"mood": "fear"})})
    assert plan(world.sid) == ["MUX"]

    monkeypatch.setattr(pipeline, "synthesize", fail_if_called("synthesize"))
    monkeypatch.setattr(pipeline.storage, "copy", fail_if_called("storage.copy"))
    second_final = pipeline.mux_scene(world.sid)

    assert second_final != first_final and scene(world.sid).lipsync == lipsync
    assert storage.exists(first_final) and storage.exists(second_final)         # içerik-adresli: eski sonuç silinmez
    assert plan(world.sid) == []


def mark_visual_done(sid):
    """Replik eklemek çekim süresini (görsel girdi) uzatır; görselin yeniden üretildiğini taklit eder."""
    with session_scope() as s:
        sc = s.get(Scene, sid)
        sc.visual_hash = visual_fingerprint(sc, load_characters(s, sid))


def test_adding_a_line_lengthens_the_shot_so_the_visual_is_replanned(world):
    pipeline.voice_and_lipsync(world.sid)
    pipeline.mux_scene(world.sid)
    ingest(world.pid, TWO_LINES)
    assert plan(world.sid) == ["VISUAL", "LIPSYNC", "MUX"]


def test_changing_a_line_voices_only_the_changed_line(world, monkeypatch):
    ingest(world.pid, TWO_LINES)
    mark_visual_done(world.sid)
    pipeline.voice_and_lipsync(world.sid)
    pipeline.mux_scene(world.sid)
    old_lipsync = scene(world.sid).lipsync
    first_line_audio = tracks(world.sid, "dialogue")[0].file_key
    ingest(world.pid, TWO_LINES_SECOND_CHANGED)
    assert plan(world.sid) == ["LIPSYNC", "MUX"]

    spoken = []
    real = pipeline.synthesize
    monkeypatch.setattr(pipeline, "synthesize", lambda t, out, **kw: spoken.append(t) or real(t, out, **kw))
    pipeline.voice_and_lipsync(world.sid)

    assert spoken == ["Yarın görüşürüz."]                                      # değişmeyen replik yeniden seslendirilmedi
    assert tracks(world.sid, "dialogue")[0].file_key == first_line_audio
    assert scene(world.sid).lipsync != old_lipsync and storage.exists(old_lipsync)
    pipeline.mux_scene(world.sid)
    assert plan(world.sid) == []
