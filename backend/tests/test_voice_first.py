"""Ses-önce akışı: diyalog animasyondan önce kaydedilir; çekim süresi ve tempo sesten çıkar.

Kabul:
  - Ses sayfası replikleri sekans ve çekim altında listeler; geçici ses (espeak) ölçülür, final kayıt yüklenir.
  - Dosya adı S001_SH01_2.wav biçimindeyse repliğe eşlenir; senaryo yeniden işlenince final kayıt korunur.
  - Sekansın sesi kilitlenince görüntüsü üretilmemiş çekimlerin süresi sesten hesaplanır ("voice"); üretilmiş çekimde
    süre değişmez, yalnızca uyarı verilir (planlayıcı değişmez). Kilitli sekansta replik değişmez. Kilit ve kilidin
    açılması kürasyon kaydına düşer.
  - voice_first açıkken sesi kilitlenmemiş sekansta görüntü üretilmez.
  - Animatik onaylı kareleri (yoksa kartı) sesle dizer; ücretsiz modda yereldir.
  - Ses referansı açıkken parçanın istek gövdesine diyalog karışımı girer (Seedance audio_urls, H3 Max
    reference_audio_urls); yalnızca plan ve kuru çalıştırmada görünür, ağ çağrısı yapılmaz.
"""
import subprocess
import uuid

import pytest
from sqlalchemy import select

from app import storage
from app.db import session_scope
from app.hermes import rules
from app.models import AudioTrack, CurationEvent, Scene, Variation
from app.post import voice
from tests.fal_queue_fake import validate
from tests.media_utils import probe
from tests.test_reference_mode import nora, plan_for, ref_defaults  # noqa: F401 (fikstür)

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


# ---------- saf hesaplar ----------

def test_retiming_lays_lines_after_the_lead_in_with_gaps_and_a_tail():
    offsets, total = voice.retime([1.0, 2.0])
    assert offsets == [0.4, 1.75] and total == round(0.4 + 1.0 + 0.35 + 2.0 + 0.6, 2)
    assert voice.retime([]) == ([], 1.0)
    assert voice.clamp_shot(0.9) == 2.0 and voice.clamp_shot(14) == 10.0 and voice.clamp_shot(4.35) == 4.35


def test_a_lines_room_ends_at_the_next_line_or_the_tail_and_drift_is_over_half_a_second():
    assert voice.slots([0.4, 1.75], 4.35) == [1.0, 2.0]
    assert voice.slots([0.4], 3.0) == [2.0]
    assert not voice.drifts(2.4, 2.0) and voice.drifts(2.6, 2.0) and not voice.drifts(None, 2.0)


@pytest.mark.parametrize("name,expected", [("S001_SH01_2.wav", ("S001_SH01", 2)), ("s002_sh03-1.MP3", ("S002_SH03", 1)),
                                           ("S001_SH01_0.wav", None), ("S001_SH01.wav", None), ("notlar.txt", None)])
def test_line_files_are_matched_by_name(name, expected):
    assert voice.parse_line_file(name) == expected


# ---------- API ----------

@pytest.fixture
def voiced(api, api_project, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    return pid


def _lines(api, pid):
    st = api.get(f"/api/projects/{pid}/voice").json()
    return st, [(g, sh, ln) for g in st["sequences"] for sh in g["shots"] for ln in sh["lines"]]


def _wav(path, seconds):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    "-ar", "48000", str(path)], check=True)
    return path


def test_the_voice_page_lists_lines_and_temp_voices_are_measured(api, voiced):
    st, lines = _lines(api, voiced)
    assert lines and st["counts"]["lines"] == len(lines) and st["counts"]["final"] == 0
    assert all(not ln["final"] and ln["duration_s"] is None and ln["slot_s"] > 0 for _, _, ln in lines)
    assert [g["code"] for g in st["sequences"]] and all(not g["locked"] for g in st["sequences"])
    assert api.post(f"/api/projects/{voiced}/voice/temp").json() == {"queued": True}         # eager: hemen biter
    st, lines = _lines(api, voiced)
    assert all(ln["duration_s"] and ln["engine"] == "espeak" and ln["url"] for _, _, ln in lines)
    assert st["counts"]["unmeasured"] == 0
    tid = lines[0][2]["track_id"]
    peaks = api.get(f"/api/audio-tracks/{tid}/peaks").json()["peaks"]
    assert len(peaks) == voice.PEAKS and max(peaks) == 1.0
    for _, sh, ln in lines:
        assert ln["drift"] == (abs(ln["duration_s"] - ln["slot_s"]) > voice.DRIFT_S)


def test_a_final_recording_replaces_its_line_and_survives_a_new_script(api, voiced, tmp_path):
    _, lines = _lines(api, voiced)
    _, shot, line = lines[0]
    name = f"{shot['scene_code']}_{line['n']}.wav"
    up = api.post(f"/api/projects/{voiced}/voice/upload?filename={name}", content=_wav(tmp_path / "a.wav", 1.5).read_bytes())
    assert up.status_code == 201, up.text
    up = up.json()
    assert up["match"] == {"scene_code": shot["scene_code"], "n": line["n"]} and abs(up["duration_s"] - 1.5) < 0.05
    r = api.put(f"/api/scenes/{shot['scene_id']}/audio", json={"kind": "dialogue", "source": "upload", "file_key": up["key"],
                                                              "replaces": line["track_id"], "params": {"filename": name}})
    assert r.status_code == 200, r.text
    _, after = _lines(api, voiced)
    final = after[0][2]
    assert final["final"] and final["text"] == line["text"] and final["character"] == line["character"]
    assert abs(final["duration_s"] - 1.5) < 0.05 and final["filename"] == name
    assert api.post(f"/api/projects/{voiced}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    _, again = _lines(api, voiced)
    assert again[0][2]["track_id"] == final["track_id"] and again[0][2]["final"]          # senaryo kaydı silmedi
    assert api.post(f"/api/projects/{voiced}/voice/upload?filename=notlar.txt", content=b"x").status_code == 422


def test_locking_retimes_unrendered_shots_from_the_voice_and_freezes_editing(api, voiced):
    st, lines = _lines(api, voiced)
    seq = st["sequences"][0]
    r = api.post(f"/api/sequences/{seq['id']}/voice-lock", json={"locked": True})
    assert r.status_code == 409 and "seslendirilmedi" in r.json()["detail"]               # önce ses gerekir
    api.post(f"/api/projects/{voiced}/voice/temp")
    r = api.post(f"/api/sequences/{seq['id']}/voice-lock", json={"locked": True})
    assert r.status_code == 200 and r.json()["locked"] is True, r.text
    st, _ = _lines(api, voiced)
    locked = st["sequences"][0]
    assert locked["locked"] and locked["locked_at"]
    for sh in locked["shots"]:
        if sh["lines"]:
            measured = [ln["duration_s"] for ln in sh["lines"]]
            offsets, total = voice.retime(measured)
            assert sh["duration_source"] == "voice" and sh["duration_s"] == voice.clamp_shot(total)
            assert [ln["start_offset_s"] for ln in sh["lines"]] == offsets and not sh["drift"]
            assert sh["scene_code"] in r.json()["retimed"]
    talker = next(sh for sh in locked["shots"] if sh["lines"])
    ln = talker["lines"][0]
    r = api.put(f"/api/scenes/{talker['scene_id']}/audio", json={"kind": "dialogue", "text": "Yeni replik.",
                                                                "replaces": ln["track_id"]})
    assert r.status_code == 409 and "kilitli" in r.json()["detail"]
    assert api.post(f"/api/sequences/{seq['id']}/voice-lock", json={"locked": False}).json()["locked"] is False
    with session_scope() as s:
        actions = [e.action for e in s.scalars(select(CurationEvent).where(CurationEvent.project_id == voiced))]
    assert "voice_lock" in actions and "voice_unlock" in actions and "voice_temp" in actions


def test_a_rendered_shot_keeps_its_duration_and_only_warns(api, voiced, fast):
    st, _ = _lines(api, voiced)
    talker = next(sh for g in st["sequences"] for sh in g["shots"] if sh["lines"])
    assert "VISUAL" in api.post(f"/api/scenes/{talker['scene_id']}/render").json()["stages"]
    with session_scope() as s:
        sc = s.get(Scene, talker["scene_id"])
        before, sc.duration_s = sc.duration_s, 9.5                   # sesten çok farklı bir çekim süresi
    seq_id = next(g["id"] for g in st["sequences"] if any(sh["scene_id"] == talker["scene_id"] for sh in g["shots"]))
    api.post(f"/api/projects/{voiced}/voice/temp")
    r = api.post(f"/api/sequences/{seq_id}/voice-lock", json={"locked": True}).json()
    assert talker["scene_code"] in r["drift"] and talker["scene_code"] not in r["retimed"]
    st, _ = _lines(api, voiced)
    shot = next(sh for g in st["sequences"] for sh in g["shots"] if sh["scene_id"] == talker["scene_id"])
    assert shot["duration_s"] == 9.5 and shot["drift"] and shot["drift_text"] == voice.DRIFT_TEXT
    assert shot["duration_source"] == "script" and before != 9.5


def test_voice_first_holds_the_picture_until_the_sequence_voice_is_locked(api, voiced, fast):
    assert api.patch(f"/api/projects/{voiced}", json={"settings": {"voice_first": True}}).status_code == 200
    r = api.post(f"/api/projects/{voiced}/render-all").json()
    assert sorted(r["awaiting_voice"]) == sorted(r["planned"]) and all(v == [] for v in r["planned"].values())
    with session_scope() as s:
        assert s.scalar(select(Variation.id).join(Scene).where(Scene.project_id == voiced).limit(1)) is None
    st, _ = _lines(api, voiced)
    api.post(f"/api/projects/{voiced}/voice/temp")
    first = st["sequences"][0]
    assert api.post(f"/api/sequences/{first['id']}/voice-lock", json={"locked": True}).status_code == 200
    sid = first["shots"][0]["scene_id"]
    assert "VISUAL" in api.post(f"/api/scenes/{sid}/render").json()["stages"]           # kilitten sonra üretilir
    other = st["sequences"][-1]["shots"][0]["scene_id"]
    if len(st["sequences"]) > 1:
        assert api.post(f"/api/scenes/{other}/render").json() == {"stages": [], "awaiting_voice": True,
                                                                  "sequence": st["sequences"][-1]["code"]}


def test_the_animatic_lays_cards_and_voices_by_the_voice_length(api, voiced):
    api.post(f"/api/projects/{voiced}/voice/temp")
    st, lines = _lines(api, voiced)
    assert api.post(f"/api/projects/{voiced}/animatic").json() == {"queued": True}
    film = api.get(f"/api/projects/{voiced}/overview").json()["animatic"]
    assert film and film["url"] and film["cut"]
    cut = film["cut"]
    shots = [sh for g in st["sequences"] for sh in g["shots"]]
    assert [c["scene_code"] for c in cut["clips"]] == [sh["scene_code"] for sh in shots]
    for c, sh in zip(cut["clips"], shots):
        expected = voice.retime([ln["duration_s"] for ln in sh["lines"]])[1] if sh["lines"] else sh["duration_s"]
        assert abs((c["out_s"] - c["in_s"]) - expected) < 0.01 and c["still"] == "card"
    assert [q["code"] for q in cut["sequences"]] == [g["code"] for g in st["sequences"]]
    with session_scope() as s:
        from app.models import Project
        key = s.get(Project, voiced).animatic_key
    path = storage.download(key, f"/tmp/{uuid.uuid4().hex}.mp4")
    info = probe(path)
    assert info["audio"] and abs(info["duration"] - cut["duration_s"]) < 0.2
    served = api.get(film["url"], headers={"Range": "bytes=0-99"})                       # panelin oynatıcısı bu adresi açar
    assert served.status_code in (200, 206) and served.headers["content-type"].startswith("video/")


# ---------- ses referansı (referans modu, kuru çalıştırma) ----------

def _voice_nora(mk_project, tmp_path, engine="h3max"):
    pid, ids = nora(mk_project)
    with session_scope() as s:
        for i, code in enumerate(("S001_SH01", "S001_SH03")):
            key = f"uploads/voice/{pid}/{code}.wav"
            storage.put_file(str(_wav(tmp_path / f"{code}.wav", 1.2 + i)), key, "audio/wav")
            s.add(AudioTrack(scene_id=ids[code], kind="dialogue", source="upload", text=f"line {i}", file_key=key,
                             content_hash=f"h{i}", start_offset_s=0.4, gain_db=0.0, is_active=True, version=1,
                             duration_s=1.2 + i, params={}))
        for sid in ids.values():
            s.get(Scene, sid).engine = engine
    return pid, ids


@pytest.mark.parametrize("engine,field,token", [("h3max", "reference_audio_urls", "Audio 1"), ("seedance25", "audio_urls", "@Audio1")])
def test_the_audio_reference_joins_the_request_body_only_when_switched_on(ref_defaults, mk_project, s3, api, tmp_path,
                                                                          engine, field, token):
    pid, ids = _voice_nora(mk_project, tmp_path, engine)
    off = plan_for(ids["S001_SH02"])
    assert field not in off["body"] and off["audio_ref"] is None
    r = api.patch(f"/api/scenes/{ids['S001_SH01']}", json={"video_audio_ref": "on"})        # parçanın ilk çekimi
    assert r.status_code == 200, r.text
    plan = api.post(f"/api/scenes/{ids['S001_SH02']}/render?dry_run=true").json()
    assert plan["network"] is False and plan["dry_run"] is True
    body = plan["body"]
    assert len(body[field]) == 1 and body[field][0].startswith("local:") and token in body["prompt"]
    assert validate(plan["endpoint"], body) == []
    row = next(u for u in plan["uploads"] if u["field"] == f"{field}[0]")
    assert row["key"].startswith("voice/refs/") and "2 replik" in row["note"]
    assert plan["audio_ref"]["duration_s"] == 10.0 and plan["plan_hash"] != off["plan_hash"]
    assert not storage.exists(row["key"])                                                  # kuru çalıştırma: dosya yok
    assert api.patch(f"/api/scenes/{ids['S001_SH01']}", json={"video_audio_ref": "off"}).status_code == 200
    assert field not in plan_for(ids["S001_SH02"])["body"]


def test_the_paid_path_builds_uploads_and_sends_the_dialogue_mix(ref_defaults, paid, s3, mk_project, monkeypatch, tmp_path,
                                                                api, fast):
    """Ücretli yol sahte kuyrukla (ağ yok): karışım yerel olarak üretilir, yüklenir ve gövdede tek ses referansıdır."""
    import sys
    from app.config import settings
    from app.engine.iteration import run_tiered_generation
    from app.engine.procedural import render_clip
    from app.live import fal_queue, fal_video
    from app.quality import gate
    from tests.fal_queue_fake import FakeQueue
    fake = FakeQueue()
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(fal_queue, "POLL_S", 0)
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    monkeypatch.setattr(gate, "ensure_scorer", lambda: None)
    fal_queue._local_uploads.clear()
    for key in fal_queue._r().scan_iter("hg:fal:*"):
        fal_queue._r().delete(key)

    def download(url, suffix=".mp4"):
        path = str(tmp_path / f"{uuid.uuid4().hex}{suffix}")
        render_clip(path, 1, 160, 90, 10.5, 8, emotion="neutral", camera={"shot": "wide"}, characters=["Nora"])
        return path
    monkeypatch.setattr(fal_video, "_download", download)
    pid, ids = _voice_nora(mk_project, tmp_path, "seedance25")
    api.patch(f"/api/scenes/{ids['S001_SH01']}", json={"video_audio_ref": "on"})
    plan = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    key = plan["audio_ref"]["key"]
    assert api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve", json={"plan_hash": plan["plan_hash"]}).status_code == 200
    for sid in ids.values():
        run_tiered_generation(pid, sid)
    body = fake.submits[0][1]
    assert validate(fake.endpoints()[0], body) == [] and len(body["audio_urls"]) == 1
    assert body["audio_urls"][0].startswith("https://") and "@Audio1" in body["prompt"]
    assert storage.exists(key) and abs(probe(storage.download(key, str(tmp_path / "m.wav")))["duration"] - 10.0) < 0.1
