"""Hermes ingest: senaryo -> karakterler, sahneler, diyalog ve müzik kanalları.

Gerçek PostgreSQL gerektirir (`hellgrind_test` veritabanı); erişilemiyorsa atlanır.
"""
from sqlalchemy import text

from app.db import session_scope
from app.hermes.ingest import ingest
from app.hermes.rules import SAMPLE_SCRIPT, parse


def rows(sql, **params):
    with session_scope() as s:
        return s.execute(text(sql), params).all()


def scenes(pid):
    return rows("SELECT id::text, scene_code, seq_order, status::text, emotion::text, prompt_spec, description "
                "FROM scenes WHERE project_id = :p ORDER BY seq_order", p=pid)


def tracks(pid, active=True):
    return rows("""SELECT sc.scene_code, t.id::text AS id, t.kind::text AS kind, t.source, t.text, t.emotion::text AS emotion,
                          t.gain_db, t.start_offset_s, t.content_hash, t.is_active, t.character_id::text AS character_id,
                          t.params
                   FROM audio_tracks t JOIN scenes sc ON sc.id = t.scene_id
                   WHERE sc.project_id = :p AND (t.is_active OR NOT :a)
                   ORDER BY sc.seq_order, t.kind, t.start_offset_s, t.created_at""", p=pid, a=active)


def test_ingest_creates_cast_scenes_and_links(mk_project):
    pid = mk_project()
    result = ingest(pid, SAMPLE_SCRIPT)
    parsed = parse(SAMPLE_SCRIPT)

    assert result["cast"] == ["Kerem", "Ayşe"] and result["shots"] == 4
    assert result["created"] == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"] and result["updated"] == []

    assert {r[0] for r in rows("SELECT name FROM characters WHERE project_id = :p", p=pid)} == {"Kerem", "Ayşe"}

    got = scenes(pid)
    assert [(r.scene_code, r.seq_order, r.status) for r in got] == \
        [(f"S00{n // 2 + 1}_SH0{n % 2 + 1}", n + 1, "pending") for n in range(4)]
    assert got[2].prompt_spec["prompt"] == parsed["shots"][2]["visual_prompt"]

    links = rows("""SELECT sc.scene_code, c.name, sch.dialogue FROM scene_characters sch
                    JOIN scenes sc ON sc.id = sch.scene_id JOIN characters c ON c.id = sch.character_id
                    WHERE sc.project_id = :p""", p=pid)
    assert len(links) == sum(len(s["characters"]) for s in parsed["shots"])
    assert any(l.name == "Kerem" and l.dialogue and l.dialogue[0]["line"] == "Burada kimse yok mu?" for l in links)

    assert rows("SELECT script_source FROM projects WHERE id = :p", p=pid)[0][0] == SAMPLE_SCRIPT


def test_dialogue_channels_have_consecutive_offsets(mk_project):
    pid = mk_project()
    ingest(pid, "İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\nBir iki üç dört beş.\n\nKEREM\nTamam.\n")
    dialogue = [t for t in tracks(pid) if t.kind == "dialogue"]

    assert [t.text for t in dialogue] == ["Bir iki üç dört beş.", "Tamam."]
    assert dialogue[0].start_offset_s == 0.4
    assert dialogue[1].start_offset_s == round(0.4 + 5 / 2.5 + 0.35, 2)      # önceki replik + boşluk
    assert all(t.source == "local_tts" and t.character_id for t in dialogue)


def test_each_scene_gets_one_procedural_music_track_at_minus_8_db(mk_project):
    pid = mk_project()
    ingest(pid, SAMPLE_SCRIPT)
    music = [t for t in tracks(pid) if t.kind == "music"]
    emotions = {r.scene_code: r.emotion for r in scenes(pid)}

    assert sorted(t.scene_code for t in music) == sorted(emotions)
    assert all(t.source == "procedural" and t.gain_db == -8.0 for t in music)
    assert all(t.params == {"mood": emotions[t.scene_code]} for t in music)


def test_auto_music_can_be_disabled(mk_project):
    pid = mk_project()
    ingest(pid, SAMPLE_SCRIPT, auto_music=False)
    assert not [t for t in tracks(pid) if t.kind == "music"]


def test_reingesting_unchanged_script_keeps_every_row_and_track(mk_project):
    pid = mk_project()
    ingest(pid, SAMPLE_SCRIPT)
    before_scenes, before_tracks = scenes(pid), tracks(pid, active=False)

    again = ingest(pid, SAMPLE_SCRIPT)

    assert again["created"] == [] and len(again["updated"]) == 4
    assert [r.id for r in scenes(pid)] == [r.id for r in before_scenes]
    after = tracks(pid, active=False)
    assert [(t.id, t.content_hash, t.is_active) for t in after] == [(t.id, t.content_hash, t.is_active) for t in before_tracks]
    assert len([t for t in after if t.kind == "music"]) == 4                  # müzik çoğalmaz


def test_changed_dialogue_replaces_only_that_scenes_dialogue_tracks(mk_project):
    pid = mk_project()
    ingest(pid, SAMPLE_SCRIPT)
    before = {t.id: t for t in tracks(pid, active=False)}

    ingest(pid, SAMPLE_SCRIPT.replace("Burada kimse yok mu?", "Burada kimse var mı?"))

    after = tracks(pid, active=False)
    changed = [t for t in after if t.id not in before]
    assert [(t.scene_code, t.kind, t.text) for t in changed] == [("S001_SH01", "dialogue", "Burada kimse var mı?")]
    deactivated = [t for t in after if t.id in before and before[t.id].is_active and not t.is_active]
    assert [(t.scene_code, t.text) for t in deactivated] == [("S001_SH01", "Burada kimse yok mu?")]
    untouched = [t for t in after if t.id in before and t.is_active == before[t.id].is_active]
    assert len(untouched) == len(before) - 1


def test_changing_one_of_two_lines_keeps_the_unchanged_line_and_its_voice(mk_project):
    """Regresyon: tek replik değişince sahnedeki tüm kanallar yeniden oluşturuluyor, hepsi yeniden seslendiriliyordu."""
    script = "İÇ. ODA - GECE\n\nKerem oturur.\n\nKEREM\nBirinci replik.\n\nKEREM\nİkinci replik.\n"
    pid = mk_project()
    ingest(pid, script)
    before = {t.text: t for t in tracks(pid) if t.kind == "dialogue"}
    with session_scope() as s:                                      # ilk repliğin sesi zaten üretilmiş olsun
        s.execute(text("UPDATE audio_tracks SET file_key = 'audio/x/ilk.wav' WHERE id = CAST(:i AS uuid)"),
                  {"i": before["Birinci replik."].id})

    ingest(pid, script.replace("İkinci replik.", "Üçüncü replik."))

    after = {t.text: t for t in tracks(pid, active=False) if t.kind == "dialogue"}
    active = {t.text: t for t in tracks(pid) if t.kind == "dialogue"}
    assert set(active) == {"Birinci replik.", "Üçüncü replik."}
    assert active["Birinci replik."].id == before["Birinci replik."].id          # aynı satır, yeniden oluşturulmadı
    assert after["İkinci replik."].is_active is False                           # yalnızca eksilen pasifleşti
    assert len([t for t in tracks(pid, active=False) if t.kind == "dialogue"]) == 3


def test_existing_character_is_kept_with_its_identity_fields(mk_project):
    pid = mk_project()
    with session_scope() as s:
        s.execute(text("""INSERT INTO characters (project_id, name, pinecone_namespace, lora_url, embedding_version)
                          VALUES (:p, 'Kerem', :ns, 'https://lora.example/kerem', 3)"""), {"p": pid, "ns": pid})
    before = rows("SELECT id::text FROM characters WHERE project_id = :p AND name = 'Kerem'", p=pid)[0][0]

    ingest(pid, SAMPLE_SCRIPT)

    kerem = rows("SELECT id::text, lora_url, embedding_version FROM characters WHERE project_id = :p AND name = 'Kerem'", p=pid)
    assert kerem == [(before, "https://lora.example/kerem", 3)]
    assert rows("SELECT count(*) FROM characters WHERE project_id = :p", p=pid)[0][0] == 2
