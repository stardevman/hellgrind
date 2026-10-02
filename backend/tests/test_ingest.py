"""Senaryonun veritabanına yazılması. PostgreSQL gerektirir (yoksa atlanır)."""
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.core.rerender import Stage, plan_rerender
from app.db import session_scope
from app.hermes import ingest as hermes_ingest
from app.hermes import rules
from app.hermes.ingest import ingest
from app.models import AudioTrack, Character, Project, Scene, SceneCharacter

TWO_LINES = """İÇ. ODA

Kerem pencereye bakar.

KEREM
Birinci replik burada.

KEREM
İkinci replik şurada.
"""


def load_scenes(pid):
    """{scene_code: (scene, karakterler, kanallar)} — hepsi veritabanından okunur (oturumdan koparılmış)."""
    with session_scope() as s:
        out = {}
        for sc in s.scalars(select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order)):
            chars = list(s.scalars(select(Character).join(SceneCharacter, SceneCharacter.character_id == Character.id)
                                   .where(SceneCharacter.scene_id == sc.id)))
            tracks = list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id)
                                    .order_by(AudioTrack.start_offset_s, AudioTrack.kind)))
            out[sc.scene_code] = (sc, chars, tracks)
        s.expunge_all()
        return out


def plans(pid):
    return {code: plan_rerender(sc, chars, tracks)[0] for code, (sc, chars, tracks) in load_scenes(pid).items()}


def mark_all_done(pid):
    """Her sahneyi, şimdiki girdilerle başarıyla render edilmiş gibi işaretler."""
    for code, (sc, chars, tracks) in load_scenes(pid).items():
        _, fp = plan_rerender(sc, chars, tracks)
        with session_scope() as s:
            s.execute(text("""UPDATE scenes SET visual_hash = :v, dialogue_hash = :d, bed_hash = :b,
                              lipsync_video_key = 'l.mp4', final_video_key = 'f.mp4' WHERE id = :i"""),
                      {"v": fp.visual, "d": fp.dialogue, "b": fp.bed, "i": sc.id})


def active(tracks, kind):
    return [t for t in tracks if t.kind == kind and t.is_active]


# ---------- ilk yükleme ----------

def test_sample_script_creates_cast_scenes_links_and_tracks(mk_project):
    pid = mk_project()
    result = ingest(pid, rules.SAMPLE_SCRIPT)

    assert result["cast"] == ["Kerem", "Ayşe"] and result["shots"] == 4
    assert result["created"] == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"] and result["updated"] == []
    scenes = load_scenes(pid)
    assert list(scenes) == result["created"]
    assert [sc.seq_order for sc, _, _ in scenes.values()] == [1, 2, 3, 4]
    assert all(sc.status == "pending" for sc, _, _ in scenes.values())
    assert [sc.engine for sc, _, _ in scenes.values()] == ["fal_wan", "fal_wan", "seedance", "seedance"]
    assert [{c.name for c in chars} for _, chars, _ in scenes.values()] == [
        {"Kerem"}, {"Ayşe"}, {"Kerem", "Ayşe"}, {"Kerem", "Ayşe"}]


def test_each_scene_gets_its_dialogue_channel_and_one_music_channel_at_minus_8_db(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    for sc, _, tracks in load_scenes(pid).values():
        dialogue, music = active(tracks, "dialogue"), active(tracks, "music")
        assert len(dialogue) == 1 and dialogue[0].start_offset_s == pytest.approx(0.4)
        assert dialogue[0].character_id is not None and dialogue[0].text and dialogue[0].emotion
        assert len(music) == 1 and music[0].gain_db == -8.0 and music[0].source == "procedural"
        assert music[0].params == {"mood": sc.emotion}


def test_characters_get_their_own_vector_namespace_and_default_identity(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        chars = list(s.scalars(select(Character).where(Character.project_id == pid)))
    assert {c.name for c in chars} == {"Kerem", "Ayşe"}
    assert all(c.pinecone_namespace == str(c.id) and c.embedding_version == 1 and c.lora_url is None for c in chars)


def test_engine_is_routed_from_action_complexity_at_ingest_so_the_visual_fingerprint_is_stable(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    mark_all_done(pid)
    ingest(pid, rules.SAMPLE_SCRIPT)
    assert set(plans(pid).values()) == {Stage.NONE}
    with session_scope() as s:                                     # yönetmen motoru elle değiştirirse yeniden render gerekir
        s.execute(text("UPDATE scenes SET engine = 'higgsfield' WHERE project_id = :p AND scene_code = 'S001_SH01'"),
                  {"p": pid})
    assert plans(pid)["S001_SH01"] == Stage.VISUAL | Stage.LIPSYNC | Stage.MUX


def test_script_source_and_scene_text_are_stored(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        assert s.get(Project, pid).script_source == rules.SAMPLE_SCRIPT
    first = load_scenes(pid)["S001_SH01"][0]
    assert first.script_text.startswith("İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE") and "KEREM: Burada kimse yok mu?" in first.script_text
    assert first.prompt_spec["prompt"] and first.prompt_spec["lang"] == "tr"


def test_two_lines_of_one_shot_get_consecutive_offsets(mk_project):
    pid = mk_project()
    ingest(pid, TWO_LINES)
    (_, _, tracks), = load_scenes(pid).values()
    first, second = active(tracks, "dialogue")
    assert first.start_offset_s == pytest.approx(0.4)
    assert second.start_offset_s == pytest.approx(0.4 + 3 / rules.WPS + rules.LINE_GAP_S)


# ---------- aynı senaryoyu yeniden yükleme ----------

def test_reingesting_the_same_script_changes_nothing_and_plans_no_work(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    before = {code: [t.id for t in tracks] for code, (_, _, tracks) in load_scenes(pid).items()}
    mark_all_done(pid)
    assert set(plans(pid).values()) == {Stage.NONE}

    result = ingest(pid, rules.SAMPLE_SCRIPT)

    assert result["created"] == [] and len(result["updated"]) == 4 and result["orphaned"] == []
    assert {code: [t.id for t in tracks] for code, (_, _, tracks) in load_scenes(pid).items()} == before
    assert set(plans(pid).values()) == {Stage.NONE}               # gereksiz yeniden render yok


def test_editing_one_line_replaces_only_that_channel_and_replans_only_that_scene(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    before = load_scenes(pid)
    mark_all_done(pid)

    ingest(pid, rules.SAMPLE_SCRIPT.replace("Burada kimse yok mu?", "Orada kimse yok mu?"))

    after = load_scenes(pid)
    assert plans(pid) == {"S001_SH01": Stage.LIPSYNC | Stage.MUX, "S001_SH02": Stage.NONE,
                          "S002_SH01": Stage.NONE, "S002_SH02": Stage.NONE}
    old_ids = {t.id for t in before["S001_SH01"][2] if t.kind == "dialogue"}
    kept = [t for t in after["S001_SH01"][2] if t.kind == "dialogue"]
    assert len(kept) == 2 and {t.id for t in kept if not t.is_active} == old_ids          # eski kanal pasif, geçmişte
    assert [t.text for t in kept if t.is_active] == ["Orada kimse yok mu?"]
    assert [t.version for t in kept if t.is_active] == [2]
    assert [t.id for t in after["S001_SH02"][2]] == [t.id for t in before["S001_SH02"][2]]


def test_unchanged_line_keeps_its_channel_when_the_line_before_it_changes_length(mk_project):
    pid = mk_project()
    ingest(pid, TWO_LINES)
    (_, _, tracks), = load_scenes(pid).values()
    second_id = active(tracks, "dialogue")[1].id

    ingest(pid, TWO_LINES.replace("Birinci replik burada.", "Birinci replik burada ve daha uzun."))

    (_, _, tracks), = load_scenes(pid).values()
    dialogue = active(tracks, "dialogue")
    assert [t.text for t in dialogue] == ["Birinci replik burada ve daha uzun.", "İkinci replik şurada."]
    assert dialogue[1].id == second_id                              # yeniden seslendirilmez
    assert dialogue[1].start_offset_s == pytest.approx(0.4 + 6 / rules.WPS + rules.LINE_GAP_S)   # ama yeni ofsete kayar


def test_a_line_removed_from_the_script_is_deactivated_not_deleted(mk_project):
    pid = mk_project()
    ingest(pid, TWO_LINES)
    ingest(pid, TWO_LINES.replace("\nKEREM\nİkinci replik şurada.\n", ""))
    (_, _, tracks), = load_scenes(pid).values()
    assert [t.text for t in active(tracks, "dialogue")] == ["Birinci replik burada."]
    assert len([t for t in tracks if t.kind == "dialogue"]) == 2


def test_the_same_line_spoken_twice_keeps_two_channels(mk_project):
    script = "İÇ. ODA\n\nKerem bekler.\n\nKEREM\nHayır.\n\nKEREM\nHayır.\n"
    pid = mk_project()
    ingest(pid, script)
    (_, _, tracks), = load_scenes(pid).values()
    ids = [t.id for t in active(tracks, "dialogue")]
    assert len(ids) == 2
    ingest(pid, script)
    (_, _, tracks), = load_scenes(pid).values()
    assert [t.id for t in active(tracks, "dialogue")] == ids


# ---------- müzik ----------

def test_existing_music_is_never_duplicated_or_reset(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        s.execute(text("""UPDATE audio_tracks SET gain_db = -14 WHERE kind = 'music'
                          AND scene_id IN (SELECT id FROM scenes WHERE project_id = :p)"""), {"p": pid})
    ingest(pid, rules.SAMPLE_SCRIPT)
    for _, _, tracks in load_scenes(pid).values():
        music = active(tracks, "music")
        assert len(music) == 1 and music[0].gain_db == -14


def test_auto_music_can_be_turned_off(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT, auto_music=False)
    assert all(not active(tracks, "music") for _, _, tracks in load_scenes(pid).values())


# ---------- korunması gerekenler ----------

def test_character_identity_set_by_the_director_survives_reingest(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        s.execute(text("UPDATE characters SET lora_url = 'https://x/kerem.safetensors', lora_scale = 0.8, embedding_version = 3 "
                       "WHERE project_id = :p AND name = 'Kerem'"), {"p": pid})
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        kerem = s.scalars(select(Character).where(Character.project_id == pid, Character.name == "Kerem")).one()
    assert (kerem.lora_url, kerem.embedding_version) == ("https://x/kerem.safetensors", 3)
    assert kerem.lora_scale == pytest.approx(0.8)


def test_extra_prompt_keys_added_later_survive_reingest(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    with session_scope() as s:
        s.execute(text("""UPDATE scenes SET prompt_spec = prompt_spec || '{"negative": "bulanık"}'::jsonb
                          WHERE project_id = :p AND scene_code = 'S001_SH01'"""), {"p": pid})
    ingest(pid, rules.SAMPLE_SCRIPT)
    assert load_scenes(pid)["S001_SH01"][0].prompt_spec["negative"] == "bulanık"


def test_scenes_that_left_the_script_are_reported_but_never_deleted(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    short = rules.SAMPLE_SCRIPT.split("DIŞ. ÇÖKEN KÖPRÜ")[0]
    result = ingest(pid, short)
    assert result["shots"] == 2 and result["orphaned"] == ["S002_SH01", "S002_SH02"]
    assert len(load_scenes(pid)) == 4


def test_ingest_into_a_missing_project_fails_without_writing(pg):
    import uuid
    from sqlalchemy.exc import NoResultFound
    with pytest.raises(NoResultFound):
        ingest(str(uuid.uuid4()), rules.SAMPLE_SCRIPT)


# ---------- ayrıştırıcı seçimi ----------

def test_free_mode_never_uses_the_paid_parser_even_if_llm_mode_is_requested(monkeypatch):
    monkeypatch.setattr(settings, "hermes_mode", "llm")
    monkeypatch.setattr(settings, "dry_run", True)
    assert hermes_ingest.parse_script(rules.SAMPLE_SCRIPT, "p") == rules.parse(rules.SAMPLE_SCRIPT)
