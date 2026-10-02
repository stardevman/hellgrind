"""Film birleştirme: normalleştirme ve concat gerçek FFmpeg ile; assemble gerçek PostgreSQL/MinIO üzerinde.

Normalleştirme/concat testleri servis gerektirmez; assemble testleri servisler yoksa atlanır.
"""
import json
import os
import uuid

import numpy as np
import pytest
from sqlalchemy import text

from app import storage
from app.budget import killswitch as ks
from app.config import settings
from app.db import session_scope
from app.post import timeline
from app.post.mux import AudioInput, mux
from app.post.timeline import concat, normalize
from tests.media_utils import frame, make_sine, make_video, pcm, probe, tone_amp


def video_stream(path):
    return [s for s in probe(path)[0] if s["codec_type"] == "video"][0]


def audio_stream(path):
    return [s for s in probe(path)[0] if s["codec_type"] == "audio"][0]


# ---------- normalleştirme ----------

def test_clip_is_normalised_to_1280x720_24fps_yuv420p_with_aac_48k_stereo(tmp_path):
    out = normalize(make_video(tmp_path / "a.mp4", 2, size="320x240", fps=30), str(tmp_path / "n.mp4"))
    v, a = video_stream(out), audio_stream(out)
    assert (v["width"], v["height"], v["r_frame_rate"], v["pix_fmt"], v["codec_name"]) == (1280, 720, "24/1", "yuv420p", "h264")
    assert (a["codec_name"], a["sample_rate"], a["channels"]) == ("aac", "48000", 2)


def test_other_aspect_ratios_are_letterboxed_not_stretched(tmp_path):
    out = normalize(make_video(tmp_path / "a.mp4", 2, size="320x240", fps=24), str(tmp_path / "n.mp4"))   # 4:3
    img = frame(out)
    assert img[:, :100].mean() < 8 and img[:, -100:].mean() < 8          # sol/sağ siyah bant
    assert img[:, 400:880].mean() > 20                                   # görüntü ortada, 960x720 olarak


def test_clip_without_audio_gets_silent_stereo_audio_of_the_same_length(tmp_path):
    out = normalize(make_video(tmp_path / "a.mp4", 2.5), str(tmp_path / "n.mp4"))
    assert audio_stream(out)["channels"] == 2
    assert np.abs(pcm(out)).max() < 1e-4
    assert float(audio_stream(out)["duration"]) == pytest.approx(float(video_stream(out)["duration"]), abs=0.05)


def test_existing_audio_is_kept(tmp_path):
    final = mux(make_video(tmp_path / "v.mp4", 3), [AudioInput(make_sine(tmp_path / "t.wav", 1000, 3))], [],
                str(tmp_path / "f.mp4"))
    out = normalize(final, str(tmp_path / "n.mp4"))
    assert tone_amp(pcm(out, 0.5, 2.5), 1000) == pytest.approx(tone_amp(pcm(final, 0.5, 2.5), 1000), rel=0.15)


# ---------- birleştirme ----------

def test_concat_copies_streams_and_adds_up_the_durations(tmp_path):
    parts = [normalize(make_video(tmp_path / f"v{i}.mp4", d, size="160x90", fps=24), str(tmp_path / f"n{i}.mp4"))
             for i, d in enumerate([2.0, 3.0, 2.5])]
    film = concat(parts, str(tmp_path / "film.mp4"))
    v = video_stream(film)
    assert int(v["nb_frames"]) == 48 + 72 + 60                           # kare kaybı/çoğalması yok (-c copy)
    assert probe(film)[1] == pytest.approx(7.5, abs=0.15)
    assert (v["width"], v["height"], v["codec_name"]) == (1280, 720, "h264")
    assert not os.path.exists(str(tmp_path / "film.mp4.txt"))            # liste dosyası temizlenir


def test_audio_stays_aligned_with_its_clip_across_silent_and_voiced_clips(tmp_path):
    silent = normalize(make_video(tmp_path / "s.mp4", 3), str(tmp_path / "n0.mp4"))
    voiced_src = mux(make_video(tmp_path / "v.mp4", 3), [AudioInput(make_sine(tmp_path / "t.wav", 700, 3))], [], str(tmp_path / "f.mp4"))
    voiced = normalize(voiced_src, str(tmp_path / "n1.mp4"))
    film = concat([silent, voiced, silent], str(tmp_path / "film.mp4"))

    assert tone_amp(pcm(film, 0.5, 2.5), 700) < 0.005                    # 1. klip sessiz
    assert tone_amp(pcm(film, 3.3, 5.7), 700) > 0.1                      # 2. klip sesli, sınırda başlıyor
    assert tone_amp(pcm(film, 6.5, 8.5), 700) < 0.005                    # 3. klip yine sessiz


# ---------- assemble (veritabanı + nesne deposu) ----------

CLIPS = [("S001_SH01", 2.0, 400, "Döküm hanesi"), ("S001_SH02", 3.0, 800, "Kızıl ışık"), ("S002_SH01", 2.5, 1200, "Çöken köprü")]


@pytest.fixture
def film(s3, mk_project, tmp_path, monkeypatch):
    """3 tamamlanmış sahne (her biri farklı frekansta ton) ve ilgisiz 2 sahne; seq_order ters sırada eklenir."""
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    pid = mk_project()
    keys = {}

    def add(code, order, status, final_key, description):
        with session_scope() as s:
            s.execute(text("""INSERT INTO scenes (project_id, scene_code, seq_order, status, final_video_key, description)
                              VALUES (:p, :c, :o, CAST(:st AS scene_status), :k, :d)"""),
                      {"p": pid, "c": code, "o": order, "st": status, "k": final_key, "d": description})

    for order, (code, dur, freq, desc) in reversed(list(enumerate(CLIPS, start=1))):
        path = mux(make_video(tmp_path / f"{code}.mp4", dur), [AudioInput(make_sine(tmp_path / f"{code}.wav", freq, dur))], [],
                   str(tmp_path / f"{code}_final.mp4"))
        keys[code] = storage.put_file(path, f"final/{uuid.uuid4()}/{code}.mp4")
        add(code, order, "done", keys[code], desc)

    stale = storage.put_file(str(tmp_path / "S001_SH01_final.mp4"), f"final/{uuid.uuid4()}/stale.mp4")
    add("S009_SH01", 4, "failed", stale, "Başarısız ama eski videosu var")
    add("S009_SH02", 5, "refined", None, "Henüz montajsız")
    return pid, keys


def project_timeline(pid):
    with session_scope() as s:
        return s.execute(text("SELECT timeline_key FROM projects WHERE id = :p"), {"p": pid}).scalar()


def download_cut(key, tmp_path):
    return json.loads(storage.read_bytes(key.rsplit(".", 1)[0] + ".json"))


def test_assemble_joins_only_done_scenes_in_seq_order(film, tmp_path):
    pid, _ = film
    result = timeline.assemble(pid)

    assert result["scenes"] == 3 and result["key"].startswith(f"timeline/{pid}/") and result["key"].endswith(".mp4")
    assert project_timeline(pid) == result["key"]
    cut = download_cut(result["key"], tmp_path)
    assert [c["scene_code"] for c in cut["clips"]] == [c[0] for c in CLIPS]        # failed/refined sahneler dışarıda
    assert [c["description"] for c in cut["clips"]] == [c[3] for c in CLIPS]


def test_cut_list_is_contiguous_and_matches_the_film(film, tmp_path):
    pid, _ = film
    key = timeline.assemble(pid)["key"]
    cut = download_cut(key, tmp_path)["clips"]
    path = storage.download(key, str(tmp_path / "film.mp4"))

    assert cut[0]["in_s"] == 0 and all(a["out_s"] == b["in_s"] for a, b in zip(cut, cut[1:]))
    assert cut[-1]["out_s"] == pytest.approx(probe(path)[1], abs=0.1)
    assert [round(c["out_s"] - c["in_s"], 1) for c in cut] == [2.0, 3.0, 2.5]
    for c, (_, _, freq, _) in zip(cut, CLIPS):                                       # kesme noktaları gerçek sınırlarla örtüşür
        inside = pcm(path, c["in_s"] + 0.3, c["out_s"] - 0.3)
        assert tone_amp(inside, freq) > 0.05 and all(tone_amp(inside, f) < 0.02 for _, _, f, _ in CLIPS if f != freq)


def test_assemble_without_completed_scenes_reports_it(s3, mk_project):
    pid = mk_project()
    assert timeline.assemble(pid) == {"error": "Henüz tamamlanmış sahne yok"}
    assert project_timeline(pid) is None


def test_assemble_twice_does_no_new_work(film, monkeypatch):
    pid, _ = film
    first = timeline.assemble(pid)
    monkeypatch.setattr(timeline, "normalize", lambda *a, **k: pytest.fail("yeniden normalleştirilmemeliydi"))
    assert timeline.assemble(pid) == first


def test_a_different_set_or_order_of_scenes_is_a_different_film(film):
    pid, keys = film
    first = timeline.assemble(pid)["key"]
    with session_scope() as s:                                                     # sahnelerin sırasını değiştir
        s.execute(text("UPDATE scenes SET seq_order = 10 WHERE project_id = :p AND scene_code = 'S001_SH01'"), {"p": pid})
    reordered = timeline.assemble(pid)["key"]
    with session_scope() as s:                                                     # ve bir sahneyi 'done' dışına al
        s.execute(text("UPDATE scenes SET status = 'probing' WHERE project_id = :p AND scene_code = 'S002_SH01'"), {"p": pid})
    shorter = timeline.assemble(pid)
    assert len({first, reordered, shorter["key"]}) == 3 and shorter["scenes"] == 2
    assert storage.exists(first) and storage.exists(reordered)                     # içerik-adresli: eskiler silinmez


def test_changed_description_refreshes_the_cut_list(film, tmp_path):
    """Regresyon: anahtar yalnızca video anahtarlarından hesaplanıyordu; açıklama değişince JSON bayat kalırdı."""
    pid, _ = film
    first = timeline.assemble(pid)["key"]
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET description = 'Yeni açıklama' WHERE project_id = :p AND scene_code = 'S001_SH01'"), {"p": pid})
    second = timeline.assemble(pid)["key"]
    assert second != first and download_cut(second, tmp_path)["clips"][0]["description"] == "Yeni açıklama"


def test_missing_cut_list_is_regenerated(film, tmp_path):
    """Regresyon: mp4 yüklenip JSON yüklenemezse sonraki çalıştırma 'dosya var' deyip JSON'u hiç üretmiyordu."""
    pid, _ = film
    key = timeline.assemble(pid)["key"]
    storage.delete(key.rsplit(".", 1)[0] + ".json")
    assert timeline.assemble(pid)["key"] == key
    assert download_cut(key, tmp_path)["clips"]


def test_work_directory_is_cleaned_up_even_when_a_clip_fails(film, monkeypatch):
    pid, _ = film
    monkeypatch.setattr(timeline, "normalize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg çöktü")))
    with pytest.raises(RuntimeError):
        timeline.assemble(pid)
    assert os.listdir(settings.work_dir) == [] and project_timeline(pid) is None


def test_assemble_task_records_a_timeline_event(film):
    from app.worker.tasks import assemble_timeline
    pid, _ = film
    result = assemble_timeline(pid)
    events = [json.loads(e) for e in ks.r().lrange(ks.EVENTS.format(pid), 0, -1)]
    assert events[0]["kind"] == "timeline" and events[0]["scenes"] == 3 and events[0]["key"] == result["key"]
