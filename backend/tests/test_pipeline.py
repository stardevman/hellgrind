"""LIPSYNC ve MUX aşamaları: rafine bir sahneden başlayıp gerçek nesne deposu ve FFmpeg ile uçtan uca."""
import os

import pytest
from sqlalchemy import select, text

from app import storage
from app.budget import killswitch as ks
from app.config import settings
from app.core.rerender import Stage, plan_rerender
from app.core.state import load_characters
from app.db import session_scope
from app.engine.iteration import run_tiered_generation
from app.hermes import rules
from app.hermes.ingest import ingest, music_hash
from app.models import AudioTrack, Scene, Variation
from app.post import pipeline
from app.post.pipeline import mux_scene, voice_and_lipsync, voice_seed
from tests.media_utils import loudness_lufs, probe

BRIDGE = "S002_SH01"


@pytest.fixture
def work(monkeypatch):
    """Ses üretimi ve montaj çağrılarını sayar (gerçek işi yine yapar)."""
    calls = {"speech": [], "music": [], "mux": []}
    real_speech, real_music, real_mux = pipeline.synthesize, pipeline.synthesize_music, pipeline.mux
    monkeypatch.setattr(pipeline, "synthesize", lambda t, out, **kw: calls["speech"].append((t, kw)) or real_speech(t, out, **kw))
    monkeypatch.setattr(pipeline, "synthesize_music", lambda out, mood, secs: calls["music"].append((mood, secs)) or real_music(out, mood, secs))
    monkeypatch.setattr(pipeline, "mux", lambda *a, **kw: calls["mux"].append(a[3]) or real_mux(*a, **kw))
    return calls


def scene_row(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        s.expunge(sc)
        return sc


def tracks(sid, active=True):
    with session_scope() as s:
        q = select(AudioTrack).where(AudioTrack.scene_id == sid).order_by(AudioTrack.start_offset_s, AudioTrack.kind)
        rows = list(s.scalars(q.where(AudioTrack.is_active) if active else q))
        s.expunge_all()
        return rows


def plan(sid):
    with session_scope() as s:
        return plan_rerender(s.get(Scene, sid), load_characters(s, sid),
                             list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sid))))[0]


def rank_one(sid):
    with session_scope() as s:
        v = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1)).one()
        s.expunge(v)
        return v


def ledger_rows(pid):
    with session_scope() as s:
        return s.execute(text("SELECT count(*) FROM cost_ledger WHERE project_id = :p"), {"p": pid}).scalar()


def finish(sid):
    assert voice_and_lipsync(sid).startswith("lipsync/")
    assert mux_scene(sid).startswith("final/")


def fetch(key, tmp_path, name="f.mp4"):
    return storage.download(key, str(tmp_path / name))


# ---------- LIPSYNC ----------

def test_lipsync_stage_voices_the_dialogue_and_copies_the_rank_one_video_in_free_mode(refined, work, tmp_path):
    pid, sid = refined
    key = voice_and_lipsync(sid)

    best = rank_one(sid)
    sc = scene_row(sid)
    assert key == f"lipsync/{sid}/{best.id}_{sc.dialogue_hash}.mp4" == sc.lipsync_video_key
    assert storage.read_bytes(key) == storage.read_bytes(best.video_key)               # ücretsiz mod: video olduğu gibi
    dialogue, = [t for t in tracks(sid) if t.kind == "dialogue"]
    assert dialogue.file_key == f"audio/espeak/{dialogue.content_hash}.wav" and storage.exists(dialogue.file_key)
    assert dialogue.duration_s == pytest.approx(probe(fetch(dialogue.file_key, tmp_path, "d.wav"))["duration"], abs=0.05)
    assert [c[0] for c in work["speech"]] == [dialogue.text] and work["mux"] == [] and work["music"] == []
    assert sc.status == "lipsync"


def test_every_character_keeps_one_voice(refined, work):
    pid, sid = refined
    voice_and_lipsync(sid)
    (_, kw), = work["speech"]
    dialogue, = [t for t in tracks(sid) if t.kind == "dialogue"]
    assert kw["voice_seed"] == voice_seed(dialogue.character_id) and kw["engine"] == "espeak"
    assert kw["emotion"] == dialogue.emotion


def test_voice_seed_is_stable_per_character_and_zero_without_one():
    import uuid
    a, b = uuid.uuid4(), uuid.uuid4()
    assert voice_seed(a) == voice_seed(a) != voice_seed(b) and voice_seed(None) == 0


def test_the_lipsync_key_changes_with_the_chosen_variation_and_the_dialogue(refined, work):
    pid, sid = refined
    first = voice_and_lipsync(sid)
    with session_scope() as s:                                           # küratör #2'yi seçti: sıralar yer değiştirir
        v1 = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1)).one()
        v2 = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 2)).one()
        v1.refined_rank = None                   # kısmi benzersiz indeks: takas adım adım, her adım flush edilerek
        s.flush()
        v2.refined_rank = 1
        s.flush()
        v1.refined_rank = 2
        s.get(Scene, sid).lipsync_video_key = None
    second = voice_and_lipsync(sid)
    assert second != first and f"/{v2.id}_" in second
    assert storage.read_bytes(second) == storage.read_bytes(v2.video_key)
    assert len(work["speech"]) == 1                                      # ses yeniden üretilmedi


def test_a_new_lipsync_video_invalidates_the_old_final_video(refined, work):
    pid, sid = refined
    finish(sid)
    with session_scope() as s:
        s.get(Scene, sid).lipsync_video_key = None                       # küratör seçimi bunu sıfırlar
    assert scene_row(sid).final_video_key
    voice_and_lipsync(sid)
    assert scene_row(sid).final_video_key is None                        # eski montaj geçersiz: plan yeniden montajlasın
    assert plan(sid) == Stage.MUX


def test_rerunning_lipsync_with_the_same_inputs_keeps_the_final_video(refined, work):
    pid, sid = refined
    finish(sid)
    final = scene_row(sid).final_video_key
    voice_and_lipsync(sid)
    assert scene_row(sid).final_video_key == final


# ---------- MUX ----------

def test_mux_stage_builds_the_final_video_and_marks_the_scene_done(refined, work, tmp_path):
    pid, sid = refined
    voice_and_lipsync(sid)
    key = mux_scene(sid)

    sc = scene_row(sid)
    assert sc.status == "done" and sc.final_video_key == key and key == f"final/{sid}/{sc.mux_hash}.mp4"
    assert sc.bed_hash and sc.dialogue_hash
    info = probe(fetch(key, tmp_path))
    assert len(info["video"]) == 1 and len(info["audio"]) == 1
    source = probe(fetch(rank_one(sid).video_key, tmp_path, "src.mp4"))
    assert info["duration"] == pytest.approx(source["duration"], abs=0.1)        # süre videoya eşit
    assert len(work["mux"]) == 1


def test_music_is_synthesized_once_per_mood_and_length_and_shared(refined, work, tmp_path):
    pid, sid = refined
    sc = scene_row(sid)
    seconds = -(-(int(sc.duration_s) + 1) // pipeline.MUSIC_BUCKET_S) * pipeline.MUSIC_BUCKET_S
    key = f"audio/music/{sc.emotion}_{seconds}s.wav"
    if storage.exists(key):                      # içerik-adresli: başka bir test üretmiş olabilir; temiz başla
        storage.delete(key)

    finish(sid)
    music, = [t for t in tracks(sid) if t.kind == "music"]
    assert work["music"] == [(sc.emotion, seconds)] and music.file_key == key and music.duration_s == seconds
    assert probe(fetch(key, tmp_path, "m.wav"))["duration"] == pytest.approx(seconds, abs=0.1)
    assert seconds >= sc.duration_s + 1

    with session_scope() as s:                   # aynı duygu ve uzunluk: dosya paylaşılır, yeniden üretilmez
        s.get(Scene, sid).final_video_key = None
        s.get(Scene, sid).lipsync_video_key = None
    finish(sid)
    assert len(work["music"]) == 1


def test_loudness_comes_from_the_project_settings(refined, work, tmp_path):
    pid, sid = refined
    with session_scope() as s:
        s.execute(text("UPDATE projects SET settings = settings || '{\"loudness_lufs\": -16}'::jsonb WHERE id = :p"), {"p": pid})
    finish(sid)
    assert loudness_lufs(fetch(scene_row(sid).final_video_key, tmp_path)) == pytest.approx(-16, abs=2.0)


def test_mux_hash_depends_on_loudness_video_dialogue_and_bed(refined, work):
    pid, sid = refined
    finish(sid)
    first = scene_row(sid).mux_hash
    with session_scope() as s:
        s.execute(text("UPDATE projects SET settings = settings || '{\"loudness_lufs\": -20}'::jsonb WHERE id = :p"), {"p": pid})
    mux_scene(sid)
    assert scene_row(sid).mux_hash != first


def test_mux_refuses_to_silently_drop_dialogue_that_was_never_voiced(refined, work):
    pid, sid = refined
    with session_scope() as s:
        s.get(Scene, sid).lipsync_video_key = rank_one(sid).video_key       # LIPSYNC aşaması atlanmış gibi
    with pytest.raises(RuntimeError, match="seslendirilmemiş"):
        mux_scene(sid)


def test_uploaded_dialogue_is_used_as_is_and_must_have_a_file(refined, work, tmp_path):
    pid, sid = refined
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET source = 'upload' WHERE scene_id = :s AND kind = 'dialogue'"), {"s": sid})
    with pytest.raises(RuntimeError, match="ses dosyası yok"):
        voice_and_lipsync(sid)
    from tests.media_utils import make_tone
    key = f"uploads/{sid}/line.wav"
    storage.put_file(make_tone(tmp_path / "l.wav", 500, 1.0), key, "audio/wav")
    with session_scope() as s:
        s.execute(text("UPDATE audio_tracks SET file_key = :k WHERE scene_id = :s AND kind = 'dialogue'"), {"k": key, "s": sid})
    finish(sid)
    assert work["speech"] == [] and [t.file_key for t in tracks(sid) if t.kind == "dialogue"] == [key]


# ---------- stateless re-render ----------

def test_after_both_stages_the_planner_finds_nothing_left_to_do(refined, work):
    pid, sid = refined
    assert plan(sid) == Stage.LIPSYNC | Stage.MUX                  # görsel aşama bitti, post aşamaları bekliyor
    finish(sid)
    assert plan(sid) == Stage.NONE


def test_rerunning_the_whole_chain_does_no_work(refined, work):
    pid, sid = refined
    finish(sid)
    keys = (scene_row(sid).lipsync_video_key, scene_row(sid).final_video_key)
    before = {k: len(v) for k, v in work.items()}
    finish(sid)
    assert {k: len(v) for k, v in work.items()} == before                 # ne ses, ne müzik, ne montaj
    assert (scene_row(sid).lipsync_video_key, scene_row(sid).final_video_key) == keys


def test_changing_only_the_music_replans_and_runs_only_the_mux(refined, work):
    pid, sid = refined
    finish(sid)
    old_lipsync = scene_row(sid).lipsync_video_key
    old_final = scene_row(sid).final_video_key
    speech_before, mux_before = len(work["speech"]), len(work["mux"])
    with session_scope() as s:
        old = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sid, AudioTrack.kind == "music",
                                                 AudioTrack.is_active)).one()
        old.is_active = False
        s.add(AudioTrack(scene_id=sid, kind="music", source="procedural", params={"mood": "joy"}, emotion="joy",
                         content_hash=music_hash("joy"), gain_db=-12.0, version=old.version + 1))
    assert plan(sid) == Stage.MUX                                          # planlayıcı: yalnızca montaj

    mux_scene(sid)

    sc = scene_row(sid)
    assert sc.lipsync_video_key == old_lipsync and sc.final_video_key != old_final and sc.status == "done"
    assert len(work["speech"]) == speech_before and len(work["mux"]) == mux_before + 1
    assert plan(sid) == Stage.NONE


def test_changing_a_line_voices_only_that_line_and_replans_lipsync_and_mux(sample_scene, work):
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=2)
    sid = codes["S001_SH01"]
    assert run_tiered_generation(pid, sid) == "refined"
    finish(sid)
    old_lipsync, old_final = scene_row(sid).lipsync_video_key, scene_row(sid).final_video_key
    speech_before = len(work["speech"])

    ingest(pid, rules.SAMPLE_SCRIPT.replace("Burada kimse yok mu?", "Orada kimse yok mu?"))
    assert plan(sid) == Stage.LIPSYNC | Stage.MUX                          # görüntü motoru çağrılmaz

    finish(sid)

    sc = scene_row(sid)
    assert sc.lipsync_video_key != old_lipsync and sc.final_video_key != old_final
    assert [c[0] for c in work["speech"][speech_before:]] == ["Orada kimse yok mu?"]
    assert plan(sid) == Stage.NONE


def test_the_same_line_in_two_scenes_is_voiced_once(work, mk_project, fast):
    script = ("İÇ. ODA\n\nKerem bekler.\n\nKEREM\nAynı replik.\n\nİÇ. SOKAK\n\nKerem yürür.\n\nKEREM\nAynı replik.\n")
    pid = mk_project(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_spend_per_10min=10_000.0)
    ingest(pid, script)
    with session_scope() as s:
        ids = [str(x) for x in s.scalars(select(Scene.id).where(Scene.project_id == pid).order_by(Scene.seq_order))]
    for sid in ids:
        run_tiered_generation(pid, sid)
        voice_and_lipsync(sid)
    assert len(work["speech"]) == 1
    keys = {t.file_key for sid in ids for t in tracks(sid) if t.kind == "dialogue"}
    assert len(keys) == 1


def test_the_tone_engine_writes_under_its_own_keys(refined, work, monkeypatch):
    pid, sid = refined
    monkeypatch.setattr(settings, "tts_engine", "tone")
    voice_and_lipsync(sid)
    dialogue, = [t for t in tracks(sid) if t.kind == "dialogue"]
    assert dialogue.file_key == f"audio/tone/{dialogue.content_hash}.wav"


# ---------- atlama ve durma ----------

@pytest.mark.parametrize("status", ["needs_review", "frozen"])
def test_stages_skip_scenes_that_cannot_proceed(refined, work, status):
    pid, sid = refined
    with session_scope() as s:
        s.get(Scene, sid).status = status
    assert voice_and_lipsync(sid) == f"skip ({status})" and mux_scene(sid) == f"skip ({status})"
    assert work["speech"] == [] and scene_row(sid).status == status and scene_row(sid).lipsync_video_key is None


def test_stages_do_nothing_when_there_is_no_refined_video(mk_project, mk_scene, fast, work):
    pid = mk_project()
    sid = mk_scene(pid, "pending")
    assert voice_and_lipsync(sid) == "skip (rafine video yok)"
    assert mux_scene(sid) == "skip (dudak senkronlu video yok)"


def test_a_frozen_project_stops_the_stage_without_overwriting_scene_status(refined, work):
    pid, sid = refined
    ks.r().set(ks.FROZEN.format(pid), "test")
    with pytest.raises(ks.ProjectFrozen):
        voice_and_lipsync(sid)
    assert scene_row(sid).status == "refined"


def test_paid_mode_without_a_key_stops_before_any_reservation(refined, work, monkeypatch):
    """Ücretli lipsync yolu bağlı; ama anahtar yoksa guard, rezervasyondan ÖNCE durdurur: deftere iz bile bırakmaz."""
    from sqlalchemy import text

    from app.live import PaidCallBlocked
    pid, sid = refined
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "fal_key", "")
    before = ledger_rows(pid)
    with pytest.raises(PaidCallBlocked, match="FAL_KEY"):
        voice_and_lipsync(sid)
    assert scene_row(sid).lipsync_video_key is None and ledger_rows(pid) == before


def test_stages_leave_no_temporary_files_behind(refined, work):
    pid, sid = refined
    leftovers_before = set(os.listdir(settings.work_dir))
    finish(sid)
    assert set(os.listdir(settings.work_dir)) == leftovers_before


def test_a_failing_stage_cleans_up_and_leaves_the_scene_resumable(refined, work, monkeypatch):
    pid, sid = refined
    leftovers_before = set(os.listdir(settings.work_dir))
    monkeypatch.setattr(pipeline, "synthesize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts çöktü")))
    with pytest.raises(RuntimeError, match="tts çöktü"):
        voice_and_lipsync(sid)
    assert set(os.listdir(settings.work_dir)) == leftovers_before
    assert scene_row(sid).lipsync_video_key is None
