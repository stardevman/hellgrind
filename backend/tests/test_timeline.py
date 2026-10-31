"""Film birleştirme: gerçek renkli ve tonlu klipler, nesne deposu ve veritabanı ile."""
import json
import subprocess
import uuid

import pytest
from sqlalchemy import text

from app import storage
from app.config import settings
from app.budget import killswitch as ks
from app.db import session_scope
from app.post import timeline
from app.post.timeline import assemble, concat, normalize
from tests.media_utils import band_db, frame, make_av, pcm, probe

RATE = 16000


def dominant(rgb):
    return "RGB"[int(rgb.reshape(-1, 3).mean(axis=0).argmax())]


def film_info(path):
    info = probe(path)
    return info, info["video"][0], info["audio"][0]


# ---------- normalize ----------

def test_clips_are_normalised_to_1280x720_24fps_yuv420p_with_stereo_aac(tmp_path):
    out = normalize(make_av(tmp_path / "a.mp4", "red", 440, 2.0, size="160x90", fps=12), str(tmp_path / "n.mp4"))
    _, v, a = film_info(out)
    assert (v["codec_name"], v["pix_fmt"], int(v["width"]), int(v["height"]), v["r_frame_rate"]) == ("h264", "yuv420p", 1280, 720, "24/1")
    assert (a["codec_name"], int(a["channels"]), int(a["sample_rate"])) == ("aac", 2, 48000)


def test_other_aspect_ratios_are_letterboxed_not_stretched(tmp_path):
    out = normalize(make_av(tmp_path / "a.mp4", "red", 440, 2.0, size="160x120"), str(tmp_path / "n.mp4"))   # 4:3
    img = frame(out, 1.0)
    assert dominant(img[:, 640 - 50:640 + 50]) == "R"                        # ortada görüntü
    assert img[:, :100].mean() < 30 and img[:, -100:].mean() < 30            # yanlarda siyah şerit


def test_a_clip_without_sound_gets_a_silent_audio_track(tmp_path):
    out = normalize(make_av(tmp_path / "a.mp4", "red", None, 2.0), str(tmp_path / "n.mp4"))
    info, _, a = film_info(out)
    assert float(info["duration"]) == pytest.approx(2.0, abs=0.1)
    assert abs(pcm(out, RATE)).max() < 0.01


@pytest.mark.parametrize("audio_seconds", [1.0, 3.5])
def test_audio_is_made_exactly_as_long_as_the_picture(tmp_path, audio_seconds):
    """Kısa ses sessizlikle tamamlanır, uzun ses kesilir: yoksa -c copy birleştirmede ses kayması birikir."""
    out = normalize(make_av(tmp_path / "a.mp4", "red", 440, 2.0, audio_seconds=audio_seconds), str(tmp_path / "n.mp4"))
    info, v, a = film_info(out)
    assert float(a["duration"]) == pytest.approx(float(v["duration"]), abs=0.06)
    assert info["duration"] == pytest.approx(2.0, abs=0.1)


def test_frame_rate_is_converted_to_24(tmp_path):
    out = normalize(make_av(tmp_path / "a.mp4", "red", 440, 2.0, fps=30), str(tmp_path / "n.mp4"))
    assert film_info(out)[1]["r_frame_rate"] == "24/1"


# ---------- concat ----------

def test_concat_joins_without_re_encoding_and_sums_durations(tmp_path):
    parts = [normalize(make_av(tmp_path / f"{c}.mp4", c, f, 1.5), str(tmp_path / f"n{c}.mp4"))
             for c, f in (("red", 400), ("blue", 800))]
    out = concat(parts, str(tmp_path / "f.mp4"))
    assert probe(out)["duration"] == pytest.approx(3.0, abs=0.1)
    assert not (tmp_path / "f.mp4.txt").exists()


def test_concat_survives_quotes_in_paths(tmp_path):
    odd = tmp_path / "it's here"
    odd.mkdir()
    part = normalize(make_av(tmp_path / "a.mp4", "red", 400, 1.0), str(odd / "n.mp4"))
    assert probe(concat([part], str(tmp_path / "f.mp4")))["duration"] == pytest.approx(1.0, abs=0.1)


# ---------- assemble ----------

@pytest.fixture
def film(mk_project, tmp_path, s3):
    """Sahne ekleyen yardımcı: gerçek bir 'final' videosu yükler ve sahneyi done yapar."""
    pid = mk_project()
    counter = iter(range(1, 100))

    def add(color="red", freq: float | None = 440.0, seconds=2.0, *, status="done", order=None, desc=None,
            emotion="neutral", with_final=True, project=None, size="160x90", audio_seconds=None):
        n = next(counter)
        sid, code = str(uuid.uuid4()), f"S{n:03d}_SH01"
        key = f"final/{sid}/{uuid.uuid4().hex}.mp4" if with_final else None
        if with_final:
            storage.put_file(make_av(tmp_path / f"{sid}.mp4", color, freq, seconds, size=size,
                                     audio_seconds=audio_seconds), key)
        with session_scope() as s:
            s.execute(text("""INSERT INTO scenes (id, project_id, scene_code, seq_order, status, description, emotion, final_video_key)
                              VALUES (:i, :p, :c, :o, CAST(:st AS scene_status), :d, CAST(:e AS emotion_tag), :k)"""),
                      {"i": sid, "p": project or pid, "c": code, "o": order if order is not None else n,
                       "st": status, "d": desc or f"{color} sahnesi", "e": emotion, "k": key})
        return sid, code, key

    add.pid = pid
    return add


def cutlist(result):
    return json.loads(storage.read_bytes(result["cutlist_key"]))


def test_film_follows_scene_order_and_the_cut_list_matches_what_is_on_screen(film, tmp_path):
    film("red", 400, 2.0, order=3, desc="üçüncü", emotion="anger")
    film("green", 800, 1.5, order=1, desc="birinci", emotion="joy")
    film("blue", 1200, 2.5, order=2, desc="ikinci", emotion="fear")

    result = assemble(film.pid)

    assert result["scenes"] == 3 and result["key"].startswith(f"timeline/{film.pid}/") and result["key"].endswith(".mp4")
    cuts = cutlist(result)
    assert [c["description"] for c in cuts["clips"]] == ["birinci", "ikinci", "üçüncü"]
    assert [c["emotion"] for c in cuts["clips"]] == ["joy", "fear", "anger"]
    assert all(set(c) >= {"scene_code", "in_s", "out_s", "description"} for c in cuts["clips"])
    path = storage.download(result["key"], str(tmp_path / "film.mp4"))
    expected = [("G", 800), ("B", 1200), ("R", 400)]
    for clip, (color, freq) in zip(cuts["clips"], expected):                       # kesme listesi gerçek görüntü ve sesle uyuşuyor
        mid = (clip["in_s"] + clip["out_s"]) / 2
        assert dominant(frame(path, mid)) == color
        x = pcm(path, RATE)
        assert band_db(x, RATE, clip["in_s"] + 0.3, clip["out_s"] - 0.3, freq - 30, freq + 30) \
            > band_db(x, RATE, clip["in_s"] + 0.3, clip["out_s"] - 0.3, 100, 300) + 30


def test_cut_list_is_contiguous_and_totals_the_film_duration(film, tmp_path):
    for color, secs in (("red", 2.0), ("green", 1.5), ("blue", 2.5)):
        film(color, 440, secs)
    result = assemble(film.pid)
    cuts = cutlist(result)
    clips = cuts["clips"]
    assert clips[0]["in_s"] == 0
    assert all(b["in_s"] == a["out_s"] for a, b in zip(clips, clips[1:]))
    path = storage.download(result["key"], str(tmp_path / "film.mp4"))
    assert clips[-1]["out_s"] == pytest.approx(probe(path)["duration"], abs=0.15) and cuts["duration_s"] == clips[-1]["out_s"]
    assert [round(c["out_s"] - c["in_s"], 1) for c in clips] == [2.0, 1.5, 2.5]


def test_the_film_is_one_normalised_stream_pair(film, tmp_path):
    film("red", 440, 1.5, size="160x90")
    film("green", None, 1.5, size="160x120")            # sessiz ve 4:3
    path = storage.download(assemble(film.pid)["key"], str(tmp_path / "f.mp4"))
    info, v, a = film_info(path)
    assert (int(v["width"]), int(v["height"]), v["r_frame_rate"], a["codec_name"]) == (1280, 720, "24/1", "aac")
    assert len(info["video"]) == 1 and len(info["audio"]) == 1


def test_picture_and_sound_stay_in_sync_when_the_scene_audio_is_shorter_or_longer_than_its_video(film, tmp_path):
    """-shortest ile ses görüntüden yarım saniye uzun çıkardı; concat her klipte biraz daha kayardı."""
    film("red", 400, 2.0, audio_seconds=1.0)
    film("green", 800, 2.0, audio_seconds=3.5)
    film("blue", 1200, 2.0)
    film("red", 400, 2.0, audio_seconds=0.5)
    result = assemble(film.pid)
    path = storage.download(result["key"], str(tmp_path / "f.mp4"))
    info = probe(path)
    assert float(info["audio"][0]["duration"]) == pytest.approx(float(info["video"][0]["duration"]), abs=0.1)
    clips = cutlist(result)["clips"]
    assert [round(c["out_s"] - c["in_s"], 1) for c in clips] == [2.0] * 4
    for a, b, (ca, cb) in zip(clips, clips[1:], (("R", "G"), ("G", "B"), ("B", "R"))):      # sınırda doğru kare
        assert dominant(frame(path, a["out_s"] - 0.15)) == ca and dominant(frame(path, b["in_s"] + 0.15)) == cb


def test_a_silent_scene_between_scenes_with_sound_keeps_audio_aligned(film, tmp_path):
    film("red", 400, 2.0)
    film("green", None, 2.0)
    film("blue", 1200, 2.0)
    result = assemble(film.pid)
    path = storage.download(result["key"], str(tmp_path / "f.mp4"))
    x, clips = pcm(path, RATE), cutlist(result)["clips"]
    assert band_db(x, RATE, 0.4, 1.6, 370, 430) > band_db(x, RATE, 2.4, 3.6, 370, 430) + 20      # ortada sessizlik
    assert band_db(x, RATE, clips[2]["in_s"] + 0.4, clips[2]["out_s"] - 0.4, 1170, 1230) \
        > band_db(x, RATE, 2.4, 3.6, 1170, 1230) + 20                                           # üçüncü sahne kendi yerinde


def test_only_finished_scenes_enter_the_film(film):
    film("red", status="done", desc="girer")
    film("green", status="refined", desc="yeniden üretiliyor: eski videosu girmemeli")
    film("blue", status="failed", desc="başarısız")
    film("red", status="frozen", desc="donmuş")
    film("red", status="done", with_final=False, desc="videosu yok")
    assert [c["description"] for c in cutlist(assemble(film.pid))["clips"]] == ["girer"]


def test_a_shot_taken_out_of_the_cut_is_skipped_and_comes_back_unchanged(film, api, monkeypatch):
    film("red", desc="birinci")
    out, _, _ = film("green", desc="çıkarılan")
    film("blue", desc="üçüncü")
    whole = assemble(film.pid)
    queued = []
    from app.worker import tasks
    monkeypatch.setattr(tasks.assemble_timeline, "delay", lambda *a: queued.append(a))

    assert api.post(f"/api/scenes/{out}/cut", json={"included": False}).json()["in_cut"] is False
    cut = assemble(film.pid)
    assert [c["description"] for c in cutlist(cut)["clips"]] == ["birinci", "üçüncü"] and cut["key"] != whole["key"]
    assert api.post(f"/api/projects/{film.pid}/assemble").json()["scenes"] == 2      # sayım da çıkarılanı atlar
    with session_scope() as s:
        assert s.execute(text("SELECT final_video_key IS NOT NULL FROM scenes WHERE id = :i"), {"i": out}).scalar()
        actions = s.execute(text("SELECT action FROM curation_events WHERE scene_id = :i"), {"i": out}).scalars().all()
    assert actions == ["cut_exclude"]

    assert api.post(f"/api/scenes/{out}/cut", json={"included": True}).json()["in_cut"] is True
    assert assemble(film.pid)["key"] == whole["key"]                                   # aynı film, yeniden kodlanmadan


def test_a_shot_out_of_the_cut_does_not_hold_back_the_automatic_assembly(film):
    from app.worker.tasks import not_ready
    film("red")
    waiting, _, _ = film("green", status="needs_review")
    assert not_ready(film.pid) == ["S002_SH01"]
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET in_cut = false WHERE id = :i"), {"i": waiting})
    assert not_ready(film.pid) == []


def test_scenes_of_other_projects_never_enter_the_film(film, mk_project):
    other = mk_project()
    film("red", desc="benim")
    film("green", desc="başkasının", project=other)
    assert [c["description"] for c in cutlist(assemble(film.pid))["clips"]] == ["benim"]


def test_project_remembers_its_film(film):
    film("red")
    result = assemble(film.pid)
    with session_scope() as s:
        assert s.execute(text("SELECT timeline_key FROM projects WHERE id = :p"), {"p": film.pid}).scalar() == result["key"]


def test_no_finished_scene_means_no_film_and_an_unchanged_project(film):
    film("red", status="refined")
    assert assemble(film.pid) == {"error": "Henüz tamamlanmış sahne yok"}
    with session_scope() as s:
        assert s.execute(text("SELECT timeline_key FROM projects WHERE id = :p"), {"p": film.pid}).scalar() is None


# ---------- içerik-adresli çıktı ----------

def test_assembling_twice_reuses_the_film(film, monkeypatch):
    film("red")
    first = assemble(film.pid)
    calls = []
    real = timeline.normalize
    monkeypatch.setattr(timeline, "normalize", lambda *a: calls.append(a) or real(*a))
    second = assemble(film.pid)
    assert second == first and calls == []                  # yeniden kodlama yok


def test_a_changed_scene_gives_a_new_film_and_keeps_the_old_one(film):
    sid, _, _ = film("red", desc="a")
    first = assemble(film.pid)
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET description = 'b' WHERE id = :s"), {"s": sid})
    second = assemble(film.pid)
    assert second["key"] != first["key"] and storage.exists(first["key"]) and storage.exists(second["key"])
    with session_scope() as s:
        assert s.execute(text("SELECT timeline_key FROM projects WHERE id = :p"), {"p": film.pid}).scalar() == second["key"]


def test_a_new_final_video_or_scene_order_changes_the_key(film):
    a, _, _ = film("red", order=1)
    film("green", order=2)
    first = assemble(film.pid)
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET seq_order = 5 WHERE id = :s"), {"s": a})
    assert assemble(film.pid)["key"] != first["key"]


def test_an_interrupted_upload_is_completed_on_the_next_run(film):
    film("red")
    first = assemble(film.pid)
    storage.delete(first["cutlist_key"])                    # film var, kesme listesi yarım kalmış
    second = assemble(film.pid)
    assert second == first and storage.exists(first["cutlist_key"])


# ---------- görev ----------

def test_assemble_task_reports_progress_and_failure_as_events(film):
    from app.worker.tasks import assemble_timeline
    events = lambda: [json.loads(e)["kind"] for e in ks.r().lrange(ks.EVENTS.format(film.pid), 0, -1)]  # noqa: E731
    assert assemble_timeline.apply(args=[film.pid]).get() == {"error": "Henüz tamamlanmış sahne yok"}
    assert events() == ["timeline_failed"]
    film("red")
    result = assemble_timeline.apply(args=[film.pid]).get()
    assert result["scenes"] == 1 and events() == ["timeline", "timeline_failed"]


def test_a_broken_source_video_fails_the_task_with_an_event(film):
    from app.worker.tasks import assemble_timeline
    _, _, key = film("red")
    storage.client().put_object(Bucket=settings.s3_bucket, Key=key, Body=b"bozuk")
    with pytest.raises(subprocess.CalledProcessError):
        assemble_timeline.apply(args=[film.pid], throw=True).get()
    assert json.loads(ks.r().lindex(ks.EVENTS.format(film.pid), 0))["kind"] == "timeline_failed"
