"""Faz 17: kürasyon kayıtları, verimlilik metrikleri ve profesyonel dışa aktarma.

Kabul:
  - Her panel eylemi bir kürasyon olayı üretir.
  - Metrikler tutarlı sayılar verir.
  - EDL dosyası tüm çekimleri doğru in/out değerleriyle içerir.
  - Paket zip'i açılabilir ve stem'ler doğru sürededir.
"""
import io
import json
import re
import zipfile

import pytest
from sqlalchemy import select, text

from app.api import assets, budget, grade, methods, panel, projects, scenes, sequences, storyboard, style
from app.curation import ACTIONS_TR
from app.db import session_scope
from app.hermes import rules
from app.models import Scene, Variation
from app.post import export
from tests.media_utils import pcm, probe, rms

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)
MODULES = (projects, scenes, methods, grade, sequences, storyboard, assets, style, budget, panel)
READ_ONLY_POSTS = {"preview_style",          # POST ama hiçbir şey yazmaz: karar değildir
                   "estimate",               # tahmini maliyet: yalnızca hesap
                   "delete_project"}         # olayın bağlı olduğu proje silinir; kayıt da CASCADE ile gider
SPEC_ACTIONS = {"promote", "upgrade_request", "board_approve", "board_reject", "asset_lock", "revision_upload",
                "style_activate", "force_rerender"}


# ---------- kayıt kapsamı (veritabanı gerekmez) ----------

def mutating_routes():
    for module in MODULES:
        for r in module.router.routes:
            if r.methods & {"POST", "PUT", "PATCH", "DELETE"}:
                yield module.__name__.split(".")[-1], r


def test_every_state_changing_route_records_a_curation_event():
    missing = [f"{m}:{r.path}" for m, r in mutating_routes()
               if r.endpoint.__name__ not in READ_ONLY_POSTS and not hasattr(r.endpoint, "__curation_action__")]
    assert missing == []


def test_every_named_action_has_a_turkish_label_and_the_spec_actions_exist():
    named = {r.endpoint.__curation_action__ for _, r in mutating_routes() if hasattr(r.endpoint, "__curation_action__")
             and isinstance(r.endpoint.__curation_action__, str)}
    assert named <= set(ACTIONS_TR)
    assert SPEC_ACTIONS <= set(ACTIONS_TR)


# ---------- EDL ve OTIO (veritabanı gerekmez) ----------

CUT = {"duration_s": 13.0, "sequences": [{"code": "SEQ01", "title": "A", "in_s": 0.0, "out_s": 6.5}],
       "clips": [{"scene_code": "S001_SH01", "sequence": "SEQ01", "in_s": 0.0, "out_s": 3.0, "description": "x"},
                 {"scene_code": "S001_SH02", "sequence": "SEQ01", "in_s": 3.0, "out_s": 6.5, "description": "y"},
                 {"scene_code": "S002_SH01", "sequence": "SEQ02", "in_s": 6.5, "out_s": 13.0, "description": "z"}]}
EVENT = re.compile(r"^(\d{3})  (\S{1,8}) +AA/V  C +(\S+) (\S+) (\S+) (\S+)$")


def tc_seconds(tc: str, fps: int = 24) -> float:
    hh, mm, ss, ff = map(int, tc.split(":"))
    return ((hh * 60 + mm) * 60 + ss) + ff / fps


def edl_events(text_: str) -> list[tuple]:
    return [m.groups() for m in map(EVENT.match, text_.splitlines()) if m]


def test_timecodes_are_frame_accurate_at_24_fps():
    assert export.timecode(0) == "00:00:00:00" and export.timecode(24 * 3661 + 5) == "01:01:01:05"
    assert export.frames(6.5) == 156 and export.reel("S001_SH01") == "S001SH01"


def test_the_edl_lists_every_shot_with_its_in_and_out():
    events = edl_events(export.edl(CUT, "Film"))
    assert [e[0] for e in events] == ["001", "002", "003"] and [e[1] for e in events] == ["S001SH01", "S001SH02", "S002SH01"]
    for (_, _, src_in, src_out, rec_in, rec_out), clip in zip(events, CUT["clips"]):
        assert (tc_seconds(rec_in), tc_seconds(rec_out)) == (clip["in_s"], clip["out_s"])
        assert tc_seconds(src_in) == 0 and tc_seconds(src_out) == pytest.approx(clip["out_s"] - clip["in_s"])
    assert "* FROM CLIP NAME: 002_S001_SH02/final.mp4" in export.edl(CUT, "Film")


def test_the_otio_timeline_has_contiguous_video_and_audio_tracks():
    doc = export.otio(CUT, "Film", jumps={"S001_SH02"})
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    video, audio = doc["tracks"]["children"]
    assert (video["kind"], audio["kind"]) == ("Video", "Audio") and len(video["children"]) == len(audio["children"]) == 3
    durations = [c["source_range"]["duration"]["value"] for c in video["children"]]
    assert durations == [72.0, 84.0, 156.0] and sum(durations) == export.frames(13.0)
    assert video["children"][1]["markers"][0]["name"] == "Renk sıçraması"
    assert video["children"][0]["media_reference"]["target_url"] == "001_S001_SH01/final.mp4"
    try:
        import opentimelineio as otio_lib                              # kuruluysa gerçek okuyucuyla da doğrula
    except ImportError:
        return
    timeline = otio_lib.adapters.read_from_string(json.dumps(doc), "otio_json")
    assert timeline.duration().to_seconds() == pytest.approx(13.0)


# ---------- uçtan uca ----------

@pytest.fixture
def film(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT}).status_code == 200
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 200
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return pid, ids


def curation(api, pid) -> list[dict]:
    body = api.get(f"/api/projects/{pid}/curation.jsonl")
    assert body.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in body.text.splitlines()]


def test_panel_actions_become_curation_events_with_training_context(api, film):
    pid, ids = film
    sid = ids["S002_SH01"]
    with session_scope() as s:
        second = str(s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 2)))
    api.post(f"/api/scenes/{sid}/promote/{second}")
    api.post(f"/api/scenes/{sid}/render?force_visual=true")
    api.post(f"/api/scenes/{sid}/board", json={"status": "changes_requested", "note": "Kerem daha yakın"})
    api.post(f"/api/scenes/{sid}/board", json={"status": "approved"})
    api.post(f"/api/scenes/{sid}/upgrade", json={"requested": True})
    api.put(f"/api/scenes/{ids['S001_SH01']}/audio", json={"kind": "music", "emotion": "joy"})
    api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze"})
    api.post(f"/api/projects/{pid}/killswitch", json={"action": "resume"})
    events = curation(api, pid)
    actions = [e["action"] for e in events]
    for expected in ("script_upload", "assemble", "promote", "force_rerender", "board_reject", "board_approve",
                     "upgrade_request", "music_change", "killswitch_freeze", "killswitch_resume"):
        assert expected in actions, expected
    promote = next(e for e in events if e["action"] == "promote")
    assert promote["scene_code"] == "S002_SH01" and promote["variation_id"] == second
    assert promote["payload"]["variation"]["rank"] == 1 and promote["payload"]["variation"]["score"] is not None
    assert promote["payload"]["result"]["stages"] == ["LIPSYNC", "MUX"]
    reject = next(e for e in events if e["action"] == "board_reject")
    assert reject["payload"]["request"]["body"]["note"] == "Kerem daha yakın" and reject["action_tr"]
    assert [e["action"] for e in api.get(f"/api/projects/{pid}/curation?limit=2").json()] == [
        "killswitch_resume", "killswitch_freeze"]


def test_a_refused_request_records_nothing(api, film):
    pid, ids = film
    before = len(curation(api, pid))
    assert api.post(f"/api/scenes/{ids['S001_SH01']}/board", json={"status": "maybe"}).status_code == 422
    assert api.post(f"/api/projects/{pid}/lut?filename=x.cube", content=b"LUT_3D_SIZE 2\n").status_code == 422
    assert len(curation(api, pid)) == before


def test_metrics_are_consistent(api, film):
    pid, _ = film
    m = api.get(f"/api/projects/{pid}/metrics").json()
    with session_scope() as s:
        n = s.execute(text("SELECT count(*) FROM variations v JOIN scenes sc ON sc.id = v.scene_id "
                           "WHERE sc.project_id = :p"), {"p": pid}).scalar()
        purged = s.execute(text("SELECT count(*) FROM variations v JOIN scenes sc ON sc.id = v.scene_id "
                                "WHERE sc.project_id = :p AND v.status IN ('purged', 'failed')"), {"p": pid}).scalar()
        spent = float(s.execute(text("SELECT spent_usd FROM projects WHERE id = :p"), {"p": pid}).scalar())
    assert m["variations"] == n and m["accepted_shots"] == 4
    assert m["variations_per_accepted_shot"] == round(n / 4, 2)
    assert sum(q["variations"] for q in m["sequences"]) == n
    assert sum(q["variation_share"] for q in m["sequences"]) == pytest.approx(1, abs=0.01)
    assert sum(q["spend_share"] for q in m["sequences"]) == pytest.approx(1, abs=0.01)
    assert m["film_minutes_source"] == "film" and m["cost_per_film_minute_usd"] == round(spent / m["film_minutes"], 2)
    assert sum(m["purge_reasons"].values()) == purged
    assert 0 < m["probe"]["success_rate"] <= 1 and m["probe"]["succeeded"] == 4
    assert m["draft"] == {"drafted": 0, "upgraded": 0, "upgrade_ratio": None}
    assert sum(m["spent_by_kind"].values()) == pytest.approx(spent, abs=0.01)


def test_export_needs_an_assembled_film(api, api_project):
    pid = api_project()
    assert api.get(f"/api/projects/{pid}/export?format=edl").status_code == 409
    assert api.get(f"/api/projects/{pid}/export?format=zip").status_code == 422


def test_the_exported_edl_matches_the_film(api, film):
    pid, _ = film
    cut = api.get(f"/api/projects/{pid}/overview").json()["timeline"]["cut"]
    events = edl_events(api.get(f"/api/projects/{pid}/export?format=edl").text)
    assert [e[1] for e in events] == [export.reel(c["scene_code"]) for c in cut["clips"]]
    for event, clip in zip(events, cut["clips"]):
        assert tc_seconds(event[4]) == pytest.approx(clip["in_s"], abs=1 / 48)
        assert tc_seconds(event[5]) == pytest.approx(clip["out_s"], abs=1 / 48)
    doc = api.get(f"/api/projects/{pid}/export?format=otio").json()
    assert [c["name"] for c in doc["tracks"]["children"][0]["children"]] == [c["scene_code"] for c in cut["clips"]]


def test_the_package_opens_and_its_stems_last_as_long_as_their_shots(api, film, tmp_path):
    pid, _ = film
    cut = api.get(f"/api/projects/{pid}/overview").json()["timeline"]["cut"]
    r = api.get(f"/api/projects/{pid}/export?format=package")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.testzip() is None
    names = set(z.namelist())
    assert {"film.edl", "film.otio", "kesme_listesi.json", "kurasyon.jsonl", "BENIOKU.txt"} <= names
    for n, clip in enumerate(cut["clips"], start=1):
        base = f"{n:03d}_{clip['scene_code']}"
        assert {f"{base}/final.mp4", f"{base}/cekim.json"} <= names
        assert any(name.startswith(f"{base}/rafine/rank1_") for name in names)
        for stem in ("dialogue", "music", "sfx"):
            path = tmp_path / f"{base}_{stem}.wav"
            path.write_bytes(z.read(f"{base}/stems/{stem}.wav"))
            assert probe(path)["duration"] == pytest.approx(clip["out_s"] - clip["in_s"], abs=0.05), (base, stem)
        music = pcm(tmp_path / f"{base}_music.wav")
        assert rms(music, 16000, 0.5, 1.5) > 0.005                                   # sekans müziği dilimlendi
        dialogue = pcm(tmp_path / f"{base}_dialogue.wav")
        assert rms(dialogue, 16000, 0, len(dialogue) / 16000) > 0.001                  # her çekimde bir replik var
        assert rms(pcm(tmp_path / f"{base}_sfx.wav"), 16000, 0, 1) < 1e-4             # efekt yok: sessiz
