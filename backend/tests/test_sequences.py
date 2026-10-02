"""Faz 11: sekans hiyerarşisi, sekans bütçe tavanı ve film düzeyinde ses.

Kabul:
  - İki sekanslı bir senaryo doğru gruplanır.
  - Sekans müziği değişince hiçbir sahne yeniden render edilmez, yalnızca film yeniden birleştirilir.
  - Sekans tavanı dolunca diğer sekanslar üretmeye devam eder.
"""
import json
import uuid

import pytest
from sqlalchemy import select, text

from app import storage
from app.budget import killswitch as ks
from app.core.rerender import Stage
from app.db import session_scope
from app.hermes import rules
from app.hermes.ingest import dominant_mood, ingest
from app.models import AudioTrack, Project, Scene, Sequence, TimelineTrack
from app.post import timeline
from app.post.mux import AudioInput, _channel
from tests.media_utils import make_tone, pcm, probe, rms
from tests.scene_utils import mark_finished, plans

CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
NO_MARKERS = rules.SAMPLE_SCRIPT.replace("# SEKANS: Dökümhane\n", "").replace("# SEKANS: Köprü\n", "")


# ---------- ayrıştırıcı (veritabanı gerekmez) ----------

def test_the_sample_script_has_two_sequences_and_each_shot_knows_its_own():
    r = rules.parse(rules.SAMPLE_SCRIPT)
    assert r["sequences"] == [{"code": "SEQ01", "title": "Dökümhane"}, {"code": "SEQ02", "title": "Köprü"}]
    assert [s["sequence"] for s in r["shots"]] == ["SEQ01", "SEQ01", "SEQ02", "SEQ02"]
    assert all("SEKANS" not in s["description"] for s in r["shots"])


def test_without_markers_every_scene_is_in_one_sequence_and_the_shots_are_unchanged():
    with_markers, without = rules.parse(rules.SAMPLE_SCRIPT), rules.parse(NO_MARKERS)
    assert without["sequences"] == [{"code": "SEQ01", "title": "Sekans 1"}]
    strip = lambda shots: [{k: v for k, v in s.items() if k != "sequence"} for s in shots]   # noqa: E731
    assert strip(with_markers["shots"]) == strip(without["shots"])


def test_scenes_before_the_first_marker_get_their_own_sequence_and_empty_sequences_are_dropped():
    script = ("İÇ. ODA - GECE\n\nKerem oturur.\n\n# SEKANS: Boş\n\n# sekans: Yol\n\nDIŞ. YOL - GÜN\n\nYol boş.\n\n"
              "# SEKANS:\n\nDIŞ. KÖPRÜ - GECE\n\nKöprü sallanır.\n")
    r = rules.parse(script)
    assert r["sequences"] == [{"code": "SEQ01", "title": "Sekans 1"}, {"code": "SEQ02", "title": "Yol"},
                              {"code": "SEQ03", "title": "Sekans 3"}]
    assert [s["sequence"] for s in r["shots"]] == ["SEQ01", "SEQ02", "SEQ03"]


def test_a_marker_ends_a_paragraph_and_a_speech():
    r = rules.parse("İÇ. ODA - GECE\n\nKEREM\nGidelim.\n# SEKANS: Dış\nDIŞ. YOL - GÜN\n\nYol boş.\n")
    assert r["shots"][0]["dialogue"][0]["line"] == "Gidelim." and [s["sequence"] for s in r["shots"]] == ["SEQ01", "SEQ02"]


def test_the_llm_mode_assigns_sequences_with_the_same_rule():
    pairs = rules.split_scenes_with_sequences(rules.SAMPLE_SCRIPT)
    assert len(pairs) == 2 and not any("SEKANS" in chunk for chunk, _ in pairs)            # LLM sekans satırını görmez
    assert rules.number_sequences([key for _, key in pairs]) == (["SEQ01", "SEQ02"],
                                                                 rules.parse(rules.SAMPLE_SCRIPT)["sequences"])


def test_sequence_music_follows_the_heaviest_emotion():
    assert dominant_mood([{"emotion": "fear", "duration_s": 3}, {"emotion": "anger", "duration_s": 4},
                          {"emotion": "fear", "duration_s": 2}]) == "fear"
    assert dominant_mood([]) == "neutral"


# ---------- mikser ve yerleşim (veritabanı gerekmez) ----------

def test_a_channel_without_a_length_is_mixed_exactly_as_before():
    assert _channel(1, AudioInput("a.wav", 0.4, -8), "b0") == (
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay=delays=400:all=1,volume=-8dB[b0]")


def test_a_timed_channel_is_trimmed_and_faded():
    f = _channel(2, AudioInput("m.wav", 3.0, -6, duration_s=10.0, fade_in_s=1.0, fade_out_s=1.5), "b1")
    assert "atrim=0:10.000,asetpts=PTS-STARTPTS,afade=t=in:st=0:d=1.000,afade=t=out:st=8.500:d=1.500," in f
    assert f.endswith("adelay=delays=3000:all=1,volume=-6dB[b1]")


def _track(seq, start=0.0, end=None, **kw):
    return {"id": str(uuid.uuid4()), "sequence": seq, "start_s": start, "end_s": end, **kw}


def test_tracks_are_placed_relative_to_their_sequence_and_never_spill_over():
    spans = {"SEQ01": (0.0, 8.0), "SEQ02": (8.0, 15.0)}
    placed = timeline.place_tracks([_track("SEQ02"), _track("SEQ02", 2.0, 30.0), _track("SEQ01", 1.0, 3.0),
                                    _track(None, 10.0), _track("SEQ09"), _track("SEQ01", 7.99)], spans, 15.0)
    assert [(p["sequence"], p["at"], p["length"]) for p in placed] == [
        ("SEQ01", 1.0, 2.0), ("SEQ02", 8.0, 7.0), (None, 10.0, 5.0), ("SEQ02", 10.0, 5.0)]


# ---------- ingest ----------

def test_ingest_groups_scenes_into_sequences_and_gives_each_sequence_its_music(mk_project):
    pid = mk_project()
    summary = ingest(pid, rules.SAMPLE_SCRIPT)
    assert summary["sequences"] == [{"code": "SEQ01", "title": "Dökümhane", "shots": 2},
                                    {"code": "SEQ02", "title": "Köprü", "shots": 2}]
    assert summary["music_created"] == ["SEQ01", "SEQ02"]
    with session_scope() as s:
        seqs = {q.code: q for q in s.scalars(select(Sequence).where(Sequence.project_id == pid))}
        scenes = {sc.scene_code: sc.sequence_id for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        tracks = s.scalars(select(TimelineTrack).where(TimelineTrack.project_id == pid)).all()
        scene_music = s.scalar(text("SELECT count(*) FROM audio_tracks a JOIN scenes sc ON sc.id = a.scene_id "
                                    "WHERE sc.project_id = :p AND a.kind = 'music'"), {"p": pid})
    assert scenes == {"S001_SH01": seqs["SEQ01"].id, "S001_SH02": seqs["SEQ01"].id,
                      "S002_SH01": seqs["SEQ02"].id, "S002_SH02": seqs["SEQ02"].id}
    assert scene_music == 0                                                   # müzik artık çekimde değil
    moods = {next(c for c, q in seqs.items() if q.id == t.sequence_id): t.params["mood"] for t in tracks}
    assert moods == {"SEQ01": "tension", "SEQ02": "fear"}                     # köprü: korku 3,5 sn > öfke 3 sn
    assert all((t.kind, t.source, t.gain_db, t.end_s) == ("music", "procedural", -8.0, None) for t in tracks)


def test_reingesting_keeps_sequences_music_and_plans(mk_project):
    pid = mk_project()
    ingest(pid, rules.SAMPLE_SCRIPT)
    mark_finished(pid)
    with session_scope() as s:
        s.execute(text("UPDATE sequences SET budget_cap_usd = 5 WHERE project_id = :p AND code = 'SEQ01'"), {"p": pid})
    again = ingest(pid, rules.SAMPLE_SCRIPT.replace("# SEKANS: Köprü", "# SEKANS: Çöken Köprü"))
    assert again["music_created"] == [] and all(st == Stage.NONE for st in plans(pid).values())
    with session_scope() as s:
        rows = {q.code: (q.title, q.budget_cap_usd) for q in s.scalars(select(Sequence).where(Sequence.project_id == pid))}
        assert s.scalar(select(text("count(*)")).select_from(TimelineTrack).where(TimelineTrack.project_id == pid)) == 2
    assert rows == {"SEQ01": ("Dökümhane", 5), "SEQ02": ("Çöken Köprü", None)}


def test_a_sequence_whose_scenes_already_have_music_gets_no_second_music(mk_project):
    pid = mk_project()
    ingest(pid, NO_MARKERS, auto_music=False)
    with session_scope() as s:
        sid = s.scalar(select(Scene.id).where(Scene.project_id == pid, Scene.scene_code == "S001_SH01"))
        s.add(AudioTrack(scene_id=sid, kind="music", source="procedural", params={"mood": "joy"}, content_hash="x",
                         gain_db=-8, is_active=True, version=1))
    assert ingest(pid, NO_MARKERS)["music_created"] == []


def test_auto_music_can_be_turned_off(mk_project):
    pid = mk_project()
    assert ingest(pid, rules.SAMPLE_SCRIPT, auto_music=False)["music_created"] == []


def test_migration_puts_existing_scenes_into_one_sequence(pg):
    with session_scope() as s:
        assert s.execute(text("SELECT 1 FROM schema_migrations WHERE name = '005_sequences.sql'")).scalar() == 1
        assert s.execute(text("SELECT count(*) FROM scenes WHERE sequence_id IS NULL AND project_id IN "
                              "(SELECT id FROM projects WHERE title NOT LIKE 'pytest-%')")).scalar() == 0


# ---------- sekans tavanı ----------

@pytest.fixture
def two_seq(mk_project):
    pid = mk_project(100.0)
    ingest(pid, rules.SAMPLE_SCRIPT, auto_music=False)
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        seqs = {q.code: str(q.id) for q in s.scalars(select(Sequence).where(Sequence.project_id == pid))}
    return pid, ids, seqs


def set_cap(seq_id, cap):
    with session_scope() as s:
        s.execute(text("UPDATE sequences SET budget_cap_usd = :c WHERE id = :q"), {"c": cap, "q": seq_id})


def seq_row(seq_id):
    with session_scope() as s:
        q = s.get(Sequence, seq_id)
        s.expunge(q)
        return q


def events(pid):
    return [json.loads(e) for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]


def test_a_sequence_cap_stops_only_that_sequence(two_seq):
    pid, ids, seqs = two_seq
    set_cap(seqs["SEQ01"], 0.5)
    rid = ks.reserve(pid, 0.4, "t", scene_id=ids["S001_SH01"])
    with pytest.raises(ks.SequenceCapReached):
        ks.reserve(pid, 0.2, "t", scene_id=ids["S001_SH02"])
    q = seq_row(seqs["SEQ01"])
    assert (q.status, float(q.reserved_usd)) == ("capped", 0.4)
    assert not ks.is_frozen(pid)                                           # proje durmadı
    ks.reserve(pid, 3.0, "t", scene_id=ids["S002_SH01"])                   # diğer sekans üretmeye devam eder
    with pytest.raises(ks.SequenceCapReached):                             # duran sekans küçük isteği de almaz
        ks.reserve(pid, 0.01, "t", scene_id=ids["S001_SH01"])
    ks.commit(pid, rid, 0.4, 0.4, "t", scene_id=ids["S001_SH01"])
    capped = [e for e in events(pid) if e["kind"] == "sequence_capped"]
    assert len(capped) == 1 and capped[0]["code"] == "SEQ01" and capped[0]["cap"] == 0.5
    with session_scope() as s:
        assert s.execute(text("SELECT status FROM projects WHERE id = :p"), {"p": pid}).scalar() == "producing"
        rows = s.execute(text("SELECT kind, sequence_id FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}).all()
    assert {str(q) for _, q in rows} == {seqs["SEQ01"], seqs["SEQ02"]}


def test_a_refused_project_reservation_does_not_leave_a_sequence_reservation(two_seq):
    pid, ids, seqs = two_seq
    with pytest.raises(ks.BudgetExceeded):
        ks.reserve(pid, 150.0, "t", scene_id=ids["S001_SH01"])
    assert float(seq_row(seqs["SEQ01"]).reserved_usd) == 0


def test_spending_up_to_the_cap_closes_the_sequence_and_release_returns_its_reservation(two_seq):
    pid, ids, seqs = two_seq
    set_cap(seqs["SEQ02"], 1.0)
    rid = ks.reserve(pid, 0.6, "t", scene_id=ids["S002_SH01"])
    ks.release(pid, rid, 0.6, "t", scene_id=ids["S002_SH01"])
    assert float(seq_row(seqs["SEQ02"]).reserved_usd) == 0
    rid = ks.reserve(pid, 0.6, "t", scene_id=ids["S002_SH01"])
    ks.commit(pid, rid, 0.6, 1.0, "t", scene_id=ids["S002_SH01"])           # gerçek maliyet tahmini aştı
    q = seq_row(seqs["SEQ02"])
    assert (q.status, float(q.spent_usd), float(q.reserved_usd)) == ("capped", 1.0, 0.0)


def test_project_resume_does_not_reopen_a_capped_sequence(two_seq):
    pid, ids, seqs = two_seq
    set_cap(seqs["SEQ01"], 0.1)
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET status = 'pending' WHERE project_id = :p"), {"p": pid})
    with pytest.raises(ks.SequenceCapReached):
        ks.reserve(pid, 0.2, "t", scene_id=ids["S001_SH01"])
    ks.freeze(pid, "test")
    ks.resume(pid)
    with session_scope() as s:
        status = dict(s.execute(text("SELECT scene_code, status FROM scenes WHERE project_id = :p"), {"p": pid}).all())
    assert status == {"S001_SH01": "frozen", "S001_SH02": "frozen", "S002_SH01": "pending", "S002_SH02": "pending"}


# ---------- API ----------

@pytest.fixture
def sample(api, api_project, dispatched):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    assert r.status_code == 200, r.text
    seqs = {q["code"]: q for q in api.get(f"/api/projects/{pid}/sequences").json()}
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return pid, ids, seqs


def test_the_overview_groups_scenes_under_their_sequences(api, sample):
    pid, ids, seqs = sample
    ov = api.get(f"/api/projects/{pid}/overview").json()
    assert [(q["code"], q["title"], q["shots"], q["status"], q["budget_cap_usd"]) for q in ov["sequences"]] == [
        ("SEQ01", "Dökümhane", 2, "active", None), ("SEQ02", "Köprü", 2, "active", None)]
    by_seq = {q["id"]: q["code"] for q in ov["sequences"]}
    assert [by_seq[sc["sequence_id"]] for sc in ov["scenes"]] == ["SEQ01", "SEQ01", "SEQ02", "SEQ02"]
    assert all(len(q["tracks"]) == 1 and q["tracks"][0]["source"] == "procedural" for q in ov["sequences"])


def test_a_capped_sequence_refuses_paid_stages_and_render_all_reports_it(api, sample, dispatched):
    pid, ids, seqs = sample
    with session_scope() as s:
        s.execute(text("UPDATE sequences SET status = 'capped', budget_cap_usd = 1 WHERE id = :q"), {"q": seqs["SEQ01"]["id"]})
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render")
    assert r.status_code == 423 and "SEQ01 sekansı bütçe tavanına ulaştı" in r.json()["detail"]
    body = api.post(f"/api/projects/{pid}/render-all").json()
    assert body["capped"] == ["S001_SH01", "S001_SH02"]
    assert sorted(sid for _, sid, _ in dispatched) == sorted([ids["S002_SH01"], ids["S002_SH02"]])


def test_raising_the_cap_reopens_the_sequence(api, sample):
    pid, ids, seqs = sample
    seq = seqs["SEQ01"]["id"]
    with session_scope() as s:
        s.execute(text("UPDATE sequences SET status = 'capped', budget_cap_usd = 1, spent_usd = 1 WHERE id = :q"), {"q": seq})
        s.execute(text("UPDATE scenes SET status = 'frozen' WHERE sequence_id = :q"), {"q": seq})
    still = api.patch(f"/api/sequences/{seq}", json={"budget_cap_usd": 1.0}).json()
    assert still["status"] == "capped"                                     # harcama tavanın altında değil
    r = api.patch(f"/api/sequences/{seq}", json={"budget_cap_usd": 3.0, "title": "  Döküm   hanesi "})
    assert (r.json()["status"], r.json()["budget_cap_usd"], r.json()["title"]) == ("active", 3.0, "Döküm hanesi")
    with session_scope() as s:
        assert set(s.scalars(select(Scene.status).where(Scene.sequence_id == seq))) == {"pending"}
    assert api.patch(f"/api/sequences/{seq}", json={"budget_cap_usd": 0.5}).json()["status"] == "capped"
    assert api.patch(f"/api/sequences/{seq}", json={"budget_cap_usd": None}).json()["status"] == "active"
    assert api.patch(f"/api/sequences/{seq}", json={"budget_cap_usd": -1}).status_code == 422


def test_changing_sequence_music_replaces_only_procedural_music_and_renders_nothing(api, sample, dispatched, tmp_path):
    pid, ids, seqs = sample
    seq = seqs["SEQ02"]["id"]
    stem = make_tone(tmp_path / "tema.wav", 330, seconds=2.0)
    up = api.post(f"/api/sequences/{seq}/tracks?filename=tema.wav&start_s=1&gain_db=-6&fade_in_s=0.5",
                  content=open(stem, "rb").read(), headers={"content-type": "audio/wav"})
    assert up.status_code == 201, up.text
    assert (up.json()["track"]["source"], up.json()["track"]["start_s"], up.json()["track"]["duration_s"]) == ("upload", 1.0, 2.0)
    r = api.post(f"/api/sequences/{seq}/tracks", json={"mood": "joy", "gain_db": -12})
    assert r.status_code == 201 and r.json()["stages"] == []                # henüz birleştirilecek sahne yok
    tracks = api.get(f"/api/sequences/{seq}/tracks").json()
    assert [(t["source"], t["mood"], t["is_active"]) for t in tracks if t["is_active"]] == [
        ("procedural", "joy", True), ("upload", None, True)]
    assert dispatched == []                                                 # hiçbir çekim kuyruğa girmedi


@pytest.mark.parametrize("query,body,needle", [
    ("filename=tema.txt", b"x", "uzantı"),
    ("filename=tema.wav&kind=dialogue", b"x", "music, sfx, ambience"),
    ("filename=tema.wav&start_s=4&end_s=2", b"x", "end_s"),
    ("filename=tema.wav", b"not audio", "ses akışı"),
    ("", b"x", "filename"),
])
def test_bad_stems_are_refused_in_turkish(api, sample, query, body, needle):
    _, _, seqs = sample
    r = api.post(f"/api/sequences/{seqs['SEQ01']['id']}/tracks?{query}", content=body,
                 headers={"content-type": "application/octet-stream"})
    assert r.status_code == 422 and needle in r.text


def test_film_level_tracks_and_track_edits(api, sample, tmp_path):
    pid, _, _ = sample
    r = api.post(f"/api/projects/{pid}/tracks", json={"mood": "sadness", "start_s": 2, "end_s": 6, "replace": False})
    assert r.status_code == 201 and r.json()["track"]["sequence_id"] is None
    tid = r.json()["track"]["id"]
    edited = api.patch(f"/api/timeline-tracks/{tid}", json={"gain_db": -20, "end_s": None}).json()["track"]
    assert (edited["gain_db"], edited["end_s"], edited["start_s"]) == (-20, None, 2)
    assert api.patch(f"/api/timeline-tracks/{tid}", json={"start_s": 9, "end_s": 3}).status_code == 422
    assert api.delete(f"/api/timeline-tracks/{tid}").json()["ok"] is True
    assert api.delete(f"/api/timeline-tracks/{tid}").status_code == 404


def test_a_manually_added_scene_joins_the_last_sequence(api, sample):
    pid, _, seqs = sample
    r = api.post(f"/api/projects/{pid}/scenes", json={"scene_code": "S009_SH01", "seq_order": 9})
    assert r.status_code == 201 and r.json()["sequence_id"] == seqs["SEQ02"]["id"]
    other = api.post(f"/api/projects/{pid}/scenes", json={"scene_code": "S010_SH01", "seq_order": 10,
                                                          "sequence_id": str(uuid.uuid4())})
    assert other.status_code == 422


# ---------- uçtan uca ----------

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=1, max_spend_per_10min=10_000)


@pytest.fixture
def film(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 200
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 200
    return pid


def cut(api, pid):
    return api.get(f"/api/projects/{pid}/overview").json()["timeline"]["cut"]


def film_file(pid, tmp_path, name):
    with session_scope() as s:
        key = s.get(Project, pid).timeline_key
    return key, storage.download(key, str(tmp_path / name))


def test_the_film_carries_sequence_music_across_the_cuts(api, film, tmp_path):
    pid = film
    c = cut(api, pid)
    assert [(q["code"], q["title"]) for q in c["sequences"]] == [("SEQ01", "Dökümhane"), ("SEQ02", "Köprü")]
    assert [x["sequence"] for x in c["clips"]] == ["SEQ01", "SEQ01", "SEQ02", "SEQ02"]
    seq_spans = {q["code"]: (q["in_s"], q["out_s"]) for q in c["sequences"]}
    assert [(t["sequence"], t["in_s"], t["out_s"]) for t in c["tracks"]] == [
        (code, *seq_spans[code]) for code in ("SEQ01", "SEQ02")]
    key, path = film_file(pid, tmp_path, "with.mp4")
    info = probe(path)
    assert len(info["video"]) == 1 and len(info["audio"]) == 1 and abs(info["duration"] - c["duration_s"]) < 0.2
    boundary = c["clips"][1]["in_s"]                                        # SEQ01 içindeki kesme
    loud = rms(pcm(path), 16000, boundary - 0.3, boundary + 0.3)

    for q in api.get(f"/api/projects/{pid}/sequences").json():               # müziği kaldır: kesme sessizleşir
        for t in q["tracks"]:
            api.delete(f"/api/timeline-tracks/{t['id']}")
    _, silent = film_file(pid, tmp_path, "without.mp4")
    assert loud > 3 * rms(pcm(silent), 16000, boundary - 0.3, boundary + 0.3) and loud > 0.01


def test_changing_sequence_music_reassembles_only_the_film(api, film, tmp_path):
    pid = film
    with session_scope() as s:
        finals = dict(s.execute(text("SELECT scene_code, final_video_key FROM scenes WHERE project_id = :p"), {"p": pid}).all())
        before_key, spent = s.get(Project, pid).timeline_key, s.get(Project, pid).spent_usd
    seq = api.get(f"/api/projects/{pid}/sequences").json()[0]["id"]
    r = api.post(f"/api/sequences/{seq}/tracks", json={"mood": "joy", "gain_db": -10})
    assert r.json()["stages"] == ["ASSEMBLE"]
    assert all(st == Stage.NONE for st in plans(pid).values())               # hiçbir sahne yeniden üretilmez
    with session_scope() as s:
        after = dict(s.execute(text("SELECT scene_code, final_video_key FROM scenes WHERE project_id = :p"), {"p": pid}).all())
        p = s.get(Project, pid)
        assert (after, p.spent_usd) == (finals, spent) and p.timeline_key != before_key
    assert next(t for t in cut(api, pid)["tracks"] if t["sequence"] == "SEQ01")["mood"] == "joy"


def test_a_capped_sequence_stops_while_the_other_finishes(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    seq1 = next(q for q in api.get(f"/api/projects/{pid}/sequences").json() if q["code"] == "SEQ01")
    api.patch(f"/api/sequences/{seq1['id']}", json={"budget_cap_usd": 0.05})
    r = api.post(f"/api/projects/{pid}/render-all").json()
    assert r["frozen"] is False
    ov = api.get(f"/api/projects/{pid}/overview").json()
    status = {sc["scene_code"]: sc["status"] for sc in ov["scenes"]}
    assert status == {"S001_SH01": "frozen", "S001_SH02": "frozen", "S002_SH01": "done", "S002_SH02": "done"}
    seqs = {q["code"]: q for q in ov["sequences"]}
    assert seqs["SEQ01"]["status"] == "capped" and seqs["SEQ01"]["spent_usd"] <= 0.05
    assert seqs["SEQ02"]["status"] == "active" and seqs["SEQ02"]["spent_usd"] > 0
    assert ov["project"]["status"] == "producing" and not ov["project"]["frozen"]
    assert any(e["kind"] == "sequence_capped" for e in ov["events"])
