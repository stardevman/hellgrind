"""Teslim şartnamesi, QC raporu, hak zinciri ve teslim paketi.

Kabul:
  - Şartname (çözünürlük, kare hızı, kodek, loudness, SRT, M&E, yazısız) final filmi besler; şartname yoksa eski sabitler.
  - QC: siyah kare, donma, loudness, fotosensitif ve çizgi titremesi geçti/kaldı ve zaman koduyla; PDF indirilir.
  - Hak zinciri: madde başına belge; yapay zekâ kullanım beyanı servis geçmişinden otomatik; eksik madde paketi engellemez.
  - Paket: master, izleme kopyası, stem'ler, M&E, SRT, EDL/OTIO, QC raporu ve lisans dosyaları.
"""
import io
import json
import subprocess
import zipfile

import numpy as np
import pytest
from sqlalchemy import select

from app import storage
from app.db import session_scope
from app.hermes import rules
from app.models import CurationEvent, Project, Scene
from app.post import delivery
from tests.media_utils import probe

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


def _clip(path, vf, seconds=2, audio=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=gray:s=320x180:d={seconds}:r=24"]
    if audio:
        cmd += ["-f", "lavfi", "-i", audio, "-shortest"]
    subprocess.run(cmd + ["-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


# ---------- QC (ffmpeg, veritabanı gerekmez) ----------

def test_a_strobe_fails_the_photosensitive_check_and_smooth_light_passes():
    strobe = [30.0, 30.0, 220.0, 220.0] * 12                       # 24 fps'de saniyede 6 parlama
    issues = delivery.flashes(strobe, 24.0)
    assert issues and issues[0]["count"] > delivery.FLASHES_PER_S
    assert delivery.flashes(list(np.linspace(30, 220, 96)), 24.0) == []          # yavaş ışık değişimi parlama değil
    assert delivery.flashes([215.0, 255.0] * 24, 24.0) == []                      # parlak alanda titreşim sayılmaz


def test_qc_names_black_strobe_and_flicker_with_timecodes(tmp_path):
    black = _clip(tmp_path / "b.mp4", "geq=lum='if(between(T\\,0.5\\,1.5)\\,0\\,128)':cb=128:cr=128")
    r = delivery.run_qc(black, None, {"clips": [{"scene_code": "S001_SH01", "in_s": 0, "out_s": 2}]})
    by = {c["id"]: c for c in r["checks"]}
    assert not by["black"]["passed"] and by["black"]["issues"][0]["tc"].startswith("00:00:00:1")
    assert by["black"]["issues"][0]["scene_code"] == "S001_SH01"
    assert not by["loudness"]["passed"] and by["loudness"]["issues"][0]["detail"] == "ses yok"
    strobe = _clip(tmp_path / "s.mp4", "geq=lum='if(lt(mod(N\\,4)\\,2)\\,30\\,220)':cb=128:cr=128")
    assert not {c["id"]: c for c in delivery.run_qc(strobe, None, None)["checks"]}["photosensitive"]["passed"]
    flicker = _clip(tmp_path / "f.mp4", "geq=lum='if(eq(mod(N\\,2)\\,0)*lt(mod(X\\,20)\\,2)\\,20\\,128)':cb=128:cr=128")
    checks = {c["id"]: c for c in delivery.run_qc(flicker, None, None)["checks"]}
    assert not checks["line_flicker"]["passed"] and checks["photosensitive"]["passed"]


def test_loudness_is_measured_against_the_spec_target(tmp_path):
    clip = _clip(tmp_path / "a.mp4", "null", 3, "sine=frequency=440:duration=3:sample_rate=48000")
    loud = delivery.measure_loudness(clip)
    assert -30 < loud["integrated"] < -10
    spec = {**delivery.DEFAULT_SPEC, "loudness_lufs": round(loud["integrated"])}
    ok = {c["id"]: c for c in delivery.run_qc(clip, spec, None)["checks"]}["loudness"]
    assert ok["passed"] and "LUFS" in ok["summary"]
    far = {c["id"]: c for c in delivery.run_qc(clip, {**spec, "loudness_lufs": -40.0}, None)["checks"]}["loudness"]
    assert not far["passed"] and "hedef -40.0" in far["issues"][0]["detail"]


def test_the_qc_report_prints_as_a_pdf():
    report = {"duration_s": 2.0, "fps": 25.0, "target_lufs": -23.0, "passed": False, "film_key": "timeline/x.mp4",
              "checked_at": "2026-10-14", "checks": [{"id": "black", "name": "Siyah kare", "passed": False, "summary": "1 sorun",
                                                     "issues": [{"tc": "00:00:00:12 – 00:00:01:12", "scene_code": "S001_SH01",
                                                                 "detail": "1.00 sn siyah"}]}]}
    data = delivery.qc_pdf(report, "Deneme")
    assert data.startswith(b"%PDF")


def test_srt_timecodes():
    assert delivery._srt_tc(3723.456) == "01:02:03,456"
    assert delivery.tc(1.52, 25.0) == "00:00:01:13" and delivery.tc(1.5, 24000 / 1001) == "00:00:01:12"


# ---------- API ve uçtan uca ----------

@pytest.fixture
def made(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        sid = str(s.scalar(select(Scene.id).where(Scene.project_id == pid, Scene.scene_code == "S001_SH01")))
    assert "VISUAL" in api.post(f"/api/scenes/{sid}/render").json()["stages"]
    return pid


def _actions(pid):
    with session_scope() as s:
        return [e.action for e in s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid))]


def test_the_spec_shapes_the_final_film_and_the_edl_timebase(api, made):
    pid = made
    st = api.get(f"/api/projects/{pid}/delivery").json()
    assert st["custom"] is False and set(st["presets"]) == {"tv_tr", "web", "platform"}
    assert st["presets"]["tv_tr"]["label"] == "TV (Türkiye, 25 fps)" and st["options"]["fps"] == ["23.976", "24", "25"]
    assert api.put(f"/api/projects/{pid}/delivery", json={"fps": "30"}).status_code == 422
    tv = {k: v for k, v in delivery.PRESETS["tv_tr"].items() if k != "label"}
    r = api.put(f"/api/projects/{pid}/delivery", json={**tv, "codec": "h264", "preset": "tv_tr"})
    assert r.status_code == 200 and r.json()["custom"] is True
    assert api.post(f"/api/projects/{pid}/assemble").status_code == 200
    with session_scope() as s:
        key = s.get(Project, pid).timeline_key
    film = storage.download(key, "/tmp/delivery_film.mp4")
    video = probe(film)["video"][0]
    assert (int(video["width"]), int(video["height"]), video["r_frame_rate"]) == (1920, 1080, "25/1")
    loud = delivery.measure_loudness(film)
    assert abs(loud["integrated"] - (-23.0)) <= 1.5
    cut = json.loads(storage.read_bytes(key[:-4] + ".json"))
    assert cut["format"]["timebase"] == 25
    edl = api.get(f"/api/projects/{pid}/export?format=edl").text
    from app.post import export
    out = cut["clips"][0]["out_s"]
    assert export.timecode(export.frames(out, 25), 25) in edl                       # 25 fps zaman kodu
    assert "delivery_spec" in _actions(pid)


def test_qc_runs_on_the_final_film_and_marks_issues_for_the_timeline(api, made):
    pid = made
    assert api.post(f"/api/projects/{pid}/qc").status_code == 409                   # film yok
    api.post(f"/api/projects/{pid}/assemble")
    assert api.post(f"/api/projects/{pid}/qc").json() == {"queued": True}
    st = api.get(f"/api/projects/{pid}/delivery").json()
    assert st["qc"]["stale"] is False and {c["id"] for c in st["qc"]["checks"]} == set(delivery.QC_NAMES)
    ov = api.get(f"/api/projects/{pid}/overview").json()["delivery"]
    assert ov["qc"]["passed"] == st["qc"]["passed"] and isinstance(ov["qc"]["issues"], list)
    pdf = api.get(f"/api/projects/{pid}/qc.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert "qc_run" in _actions(pid)


def test_rights_documents_and_the_automatic_ai_declaration(api, made):
    pid = made
    st = api.get(f"/api/projects/{pid}/delivery").json()["rights"]
    assert len(st["missing"]) == 4 and st["ai_rows"] >= 1                          # sanal üretim çağrıları
    r = api.post(f"/api/projects/{pid}/rights/script_rights?filename=hak_devri.pdf", content=b"%PDF-1.4 hak")
    assert r.status_code == 201, r.text
    assert api.post(f"/api/projects/{pid}/rights/nope?filename=a.pdf", content=b"x").status_code == 404
    assert api.post(f"/api/projects/{pid}/rights/music_license?filename=a.exe", content=b"x").status_code == 422
    st = api.get(f"/api/projects/{pid}/delivery").json()["rights"]
    assert "Senaryo hak devri" not in st["missing"] and len(st["missing"]) == 3
    assert api.get(f"/api/projects/{pid}/overview").json()["delivery"]["rights_missing"] == st["missing"]
    rows = api.get(f"/api/projects/{pid}/rights/ai-declaration").json()["rows"]
    assert rows[0]["shot"] == "S001_SH01" and rows[0]["mode"] == "sanal (ücretsiz mod)" and rows[0]["engine"]
    csv_text = api.get(f"/api/projects/{pid}/rights/ai-declaration?format=csv").text
    assert csv_text.splitlines()[0] == "Çekim,Motor,İşlem,Tarih,Mod"
    assert api.delete(f"/api/rights/{r.json()['id']}").status_code == 200
    assert {"rights_upload", "rights_remove"} <= set(_actions(pid))


def test_the_package_holds_every_deliverable_and_missing_rights_only_warn(api, made):
    pid = made
    spec = {k: v for k, v in delivery.PRESETS["tv_tr"].items() if k != "label"}
    api.put(f"/api/projects/{pid}/delivery", json={**spec, "resolution": "1280x720", "preset": "tv_tr"})
    api.post(f"/api/projects/{pid}/assemble")
    api.post(f"/api/projects/{pid}/rights/voice_consent?filename=riza.txt", content=b"ses oyuncusu rizasi")
    plan = {r["path"]: r for r in api.get(f"/api/projects/{pid}/delivery").json()["package"]["files"]}
    assert {"master.mov", "izleme_kopyasi.mp4", "me_mix.wav", "altyazi.srt", "yazisiz_surum.mp4", "qc_raporu.pdf",
            "hak_zinciri/yapay_zeka_beyani.csv", "hak_zinciri/voice_consent/riza.txt"} <= set(plan)
    assert plan["hak_zinciri/script_rights/"]["ready"] is False
    r = api.post(f"/api/projects/{pid}/delivery/package").json()
    assert r["queued"] and "Senaryo hak devri" in r["missing"]                       # eksik madde engellemez
    with session_scope() as s:
        p = s.get(Project, pid)
        key, film_key = p.delivery_key, p.timeline_key
        assert p.qc and p.qc["film_key"] == film_key                                 # QC paketten önce çalıştı
    z = zipfile.ZipFile(io.BytesIO(storage.read_bytes(key)))
    names = set(z.namelist())
    assert {"master.mov", "izleme_kopyasi.mp4", "stems/diyalog.wav", "stems/muzik.wav", "stems/efekt.wav", "me_mix.wav",
            "altyazi.srt", "film.edl", "film.otio", "qc_raporu.pdf", "hak_zinciri/yapay_zeka_beyani.csv",
            "hak_zinciri/voice_consent/riza.txt", "BENIOKU.txt"} <= names
    with open("/tmp/master.mov", "wb") as f:
        f.write(z.read("master.mov"))
    assert probe("/tmp/master.mov")["video"][0]["codec_name"] == "prores"
    film_s = probe(storage.download(film_key, "/tmp/pkg_film.mp4"))["duration"]
    with open("/tmp/stem.wav", "wb") as f:
        f.write(z.read("stems/diyalog.wav"))
    assert abs(probe("/tmp/stem.wav")["duration"] - film_s) < 0.1
    assert z.read("altyazi.srt").decode().startswith("1\n00:00:")
    assert "[eksik]" in z.read("BENIOKU.txt").decode()
    assert api.get(f"/api/projects/{pid}/delivery").json()["package"]["url"].endswith(".zip")
