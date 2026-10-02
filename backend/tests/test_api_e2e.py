"""Örnek filmin API üzerinden uçtan uca üretimi (scripts/smoke.py'nin süreç içi karşılığı).

Gerçek PostgreSQL, Redis, MinIO, FFmpeg ve espeak-ng kullanılır; Celery görevleri istek içinde eager çalışır.
Servisler yoksa atlanır.
"""
import httpx
import pytest
from sqlalchemy import text
from urllib.parse import urlsplit, urlunsplit

from app.hermes.rules import SAMPLE_SCRIPT
from app.db import session_scope
from tests.media_utils import probe


def rows(query, **params):
    with session_scope() as s:
        return s.execute(text(query), params).all()


def overview(api, pid):
    return api.get(f"/api/projects/{pid}/overview").json()


def fetch_video(api, url, tmp_path, name):
    """/api/media yönlendirmesini izler: imzalı adres localhost:9000'dir; konteynerden MinIO'ya Host başlığı korunarak gidilir."""
    r = api.get(url, follow_redirects=False)
    assert r.status_code == 307
    parts = urlsplit(r.headers["location"])
    got = httpx.get(urlunsplit(("http", "minio:9000", parts.path, parts.query, "")), headers={"Host": parts.netloc})
    assert got.status_code == 200
    path = tmp_path / name
    path.write_bytes(got.content)
    return str(path)


def spent(pid):
    return float(rows("SELECT spent_usd FROM projects WHERE id = :p", p=pid)[0][0])


def test_sample_script_becomes_a_film_through_the_api(fast, eager, api, api_project, tmp_path):
    preview = api.get("/api/meta").json()["presets"]["preview"]
    pid = api_project(60, **preview)

    # 1) Örnek senaryo 4 sahneye ayrılır ve hepsi tamamlanır
    body = api.post(f"/api/projects/{pid}/script", json={"text": SAMPLE_SCRIPT}).json()
    assert body["cast"] == ["Kerem", "Ayşe"] and body["shots"] == 4
    data = overview(api, pid)
    assert [s["status"] for s in data["scenes"]] == ["done"] * 4, [(s["scene_code"], s["status"], s["last_error"]) for s in data["scenes"]]

    # 2) Her sahnede Rafine Havuz 1-3 arası; elenenler havuzda değil; havuzdakiler anomalisiz
    for sc in data["scenes"]:
        pool = [v for v in sc["variations"] if v["rank"]]
        assert 1 <= len(pool) <= 3 and sorted(v["rank"] for v in pool) == list(range(1, len(pool) + 1)), sc["scene_code"]
        assert all(v["status"] == "refined" and not v["anomalies"] and v["video_url"] for v in pool)
        assert all(v["thumb_url"] for v in sc["variations"])                                   # küçük resim her varyasyonda
        for v in (v for v in sc["variations"] if v["anomalies"]):
            assert v["status"] == "purged" and v["reason"] == "quality_gate" and v["rank"] is None
        assert sc["final_url"] and sc["tracks"] and sc["last_error"] is None

    # 3) Sonuç videosu oynatılabilir bir mp4 (görüntü + ses)
    final = fetch_video(api, data["scenes"][0]["final_url"], tmp_path, "s1.mp4")
    assert [s["codec_type"] for s in probe(final)[0]] == ["video", "audio"]

    # 4) Harcama kayıtlı ve defterle tutarlı
    total = spent(pid)
    assert total > 0 and total == pytest.approx(sum(float(r[0]) for r in rows(
        "SELECT v.cost_usd FROM variations v JOIN scenes s ON s.id = v.scene_id WHERE s.project_id = :p", p=pid)))
    assert float(rows("SELECT reserved_usd FROM projects WHERE id = :p", p=pid)[0][0]) == 0

    # 5) Yalnızca müzik değişince yalnızca MUX çalışır, harcama değişmez
    sc = data["scenes"][1]
    before = rows("SELECT final_video_key FROM scenes WHERE id = :s", s=sc["id"])[0][0]
    r = api.put(f"/api/scenes/{sc['id']}/audio", json={"kind": "music", "source": "procedural", "params": {"mood": "joy"}, "gain_db": -6})
    assert r.json()["stages"] == ["MUX"]
    assert spent(pid) == pytest.approx(total)
    status, after = rows("SELECT status::text, final_video_key FROM scenes WHERE id = :s", s=sc["id"])[0]
    assert status == "done" and after != before and after.startswith(f"final/{sc['id']}/")      # yeniden montajlandı

    # 6) Küratör seçimi yalnızca LIPSYNC ve MUX çalıştırır
    pool = sorted((v for v in overview(api, pid)["scenes"][0]["variations"] if v["rank"]), key=lambda v: v["rank"])
    assert len(pool) >= 2
    r = api.post(f"/api/scenes/{data['scenes'][0]['id']}/promote/{pool[1]['id']}")
    assert r.json()["stages"] == ["LIPSYNC", "MUX"]
    assert spent(pid) == pytest.approx(total)
    now = {v["id"]: v["rank"] for v in overview(api, pid)["scenes"][0]["variations"] if v["rank"]}
    assert now[pool[1]["id"]] == 1 and now[pool[0]["id"]] == 2
    promoted = rows("SELECT lipsync_video_key FROM scenes WHERE id = :s", s=data["scenes"][0]["id"])[0][0]
    assert pool[1]["id"] in promoted                                   # dudak senkronu anahtarı yeni 1. varyasyonu gösterir

    # 7) Değişiklik yoksa plan boştur
    assert api.post(f"/api/projects/{pid}/render-all").json() == {s["scene_code"]: [] for s in data["scenes"]}

    # 8) Film 4 sahneyle birleşir; kesme listesi ve oynatılabilir mp4 hazır
    assert api.post(f"/api/projects/{pid}/assemble").json() == {"queued": True}
    tl = overview(api, pid)["timeline"]
    assert [c["scene_code"] for c in tl["cut"]["clips"]] == ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
    film = fetch_video(api, tl["url"], tmp_path, "film.mp4")
    assert probe(film)[1] == pytest.approx(tl["cut"]["duration_s"], abs=0.2)
    assert any(e["kind"] == "timeline" and e["scenes"] == 4 for e in overview(api, pid)["events"])


def test_one_dollar_budget_freezes_production_and_refuses_new_renders(fast, eager, api, api_project):
    preview = api.get("/api/meta").json()["presets"]["preview"]
    pid = api_project(1, **preview)

    api.post(f"/api/projects/{pid}/script", json={"text": SAMPLE_SCRIPT})

    project = api.get(f"/api/projects/{pid}").json()
    statuses = {s["status"] for s in overview(api, pid)["scenes"]}
    assert project["status"] == "paused" and project["frozen"] is True
    assert "frozen" in statuses and "done" not in statuses
    assert project["spent_usd"] <= 1.0 and project["spent_usd"] + project["reserved_usd"] <= 1.0
    first = overview(api, pid)["scenes"][0]
    assert api.post(f"/api/scenes/{first['id']}/render").status_code == 423
    assert any(e["kind"] == "frozen" for e in overview(api, pid)["events"])

    api.post(f"/api/projects/{pid}/killswitch", json={"action": "resume"})
    assert api.get(f"/api/projects/{pid}").json()["frozen"] is False
