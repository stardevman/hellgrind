"""Müşteri onay döngüsü: kapılar, sürümler (v001…), süreli inceleme bağlantısı, zaman kodlu notlar, revizyon turları.

Kabul:
  - Müşteriye gönderilen her birleştirilmiş film v001, v002… diye etiketlenir; hangi sürümün onaylandığı görünür.
  - İnceleme bağlantısı belirteçli, süreli ve salt okunurdur; bütçe, motor, maliyet ve iç ayrıntılar asla dönmez.
  - Müşteri notu oynatıcının zamanıyla gelir, çekime bağlanır; panelde çözüldü / çözülmedi olarak izlenir.
  - Değişiklik isteği kapının revizyon turunu artırır; turun maliyeti önceki gönderimden bu yana yapılan harcamadır.
  - Her karar kürasyon kaydına düşer (client_note, client_approve, client_reject).
"""
import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.db import session_scope
from app.engine.procedural import render_clip
from app.hermes import rules
from app.models import CurationEvent, Project, ReviewLink, Scene

FORBIDDEN = ("usd", "budget", "bütçe", "engine", "motor", "cost", "spent", "seed", "prompt", "h3", "seedance", "fal",
             "variation", "reserve")


@pytest.fixture
def filmed(api, api_project, tmp_path, monkeypatch):
    """Senaryosu işlenmiş proje + iki çekimlik sahte final film, kaba kurgu ve animatik (kesme listeleriyle)."""
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    pid = api_project(100.0)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        codes = [sc.scene_code for sc in s.scalars(select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order))]
    path = render_clip(str(tmp_path / "film.mp4"), 5, 320, 180, 4.0, 12, emotion="joy", camera={"shot": "wide"}, characters=["Kerem"])
    cut = {"duration_s": 4.0, "clips": [{"scene_code": codes[0], "in_s": 0.0, "out_s": 2.0}, {"scene_code": codes[1], "in_s": 2.0, "out_s": 4.0}]}
    keys = {}
    for name in ("timeline", "draft", "animatic"):
        key = f"{'animatic' if name == 'animatic' else 'timeline'}/{pid}/{name}-{uuid.uuid4().hex[:6]}.mp4"
        storage.put_file(path, key)
        storage.client().put_object(Bucket=settings.s3_bucket, Key=key[:-4] + ".json", Body=json.dumps(cut).encode())
        keys[name] = key
    with session_scope() as s:
        p = s.get(Project, pid)
        p.timeline_key, p.draft_timeline_key, p.animatic_key = keys["timeline"], keys["draft"], keys["animatic"]
    return pid, codes


def _actions(pid):
    with session_scope() as s:
        return [e.action for e in s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid).order_by(CurationEvent.id))]


def _link(api, pid, gate="final", days=7):
    v = api.post(f"/api/projects/{pid}/versions", json={"gate": gate})
    assert v.status_code == 201, v.text
    link = api.post(f"/api/projects/{pid}/review-links", json={"version_id": v.json()["id"], "days": days})
    assert link.status_code == 201, link.text
    return v.json(), link.json()


def test_versions_are_numbered_and_need_a_film(api, api_project):
    pid = api_project(10.0)
    r = api.post(f"/api/projects/{pid}/versions", json={"gate": "final"})
    assert r.status_code == 409 and "film" in r.json()["detail"]
    assert api.post(f"/api/projects/{pid}/versions", json={"gate": "script"}).status_code == 422     # video kapısı değil


def test_the_review_page_shows_only_what_the_client_may_see(api, filmed):
    pid, codes = filmed
    v1, link = _link(api, pid, "draft")
    v2, _ = _link(api, pid, "final")
    assert (v1["label"], v2["label"]) == ("v001", "v002") and v1["kind"] == "draft"
    assert link["url"] == f"/panel/review/?token={link['token']}" and len(link["token"]) >= 24
    page = api.get(f"/api/review/{link['token']}")
    assert page.status_code == 200
    body = page.json()
    assert set(body) == {"title", "label", "gate", "gate_tr", "status", "video_url", "expires_at", "sent_at", "clips", "notes"}
    assert body["label"] == "v001" and body["gate_tr"] == "Taslak kurgu" and body["status"] == "sent"
    raw = page.text.lower().replace(link["token"].lower(), "")
    assert pid not in raw and not any(word in raw for word in FORBIDDEN), raw                 # iç ayrıntı yok
    video = api.get(body["video_url"], headers={"Range": "bytes=0-99"})
    assert video.status_code == 206 and len(video.content) == 100
    assert api.get(f"/api/review/{'x' * 32}").status_code == 404


def test_notes_follow_the_player_time_to_the_shot_and_are_resolved_in_the_panel(api, filmed):
    pid, codes = filmed
    _, link = _link(api, pid, "final")
    r = api.post(f"/api/review/{link['token']}/notes", json={"at_s": 2.75, "text": "Burada ışık daha sıcak olsun"})
    assert r.status_code == 201, r.text
    note = r.json()["notes"][0]
    assert note["scene_code"] == codes[1] and note["at_s"] == 2.75 and "scene_id" not in note
    ov = api.get(f"/api/projects/{pid}/overview").json()
    mine = ov["client"]["notes"][0]
    with session_scope() as s:
        sid = str(s.scalar(select(Scene.id).where(Scene.project_id == pid, Scene.scene_code == codes[1])))
    assert mine["scene_id"] == sid and mine["shot_offset_s"] == pytest.approx(0.75) and not mine["resolved"]
    assert mine["label"] == "v001"
    r = api.patch(f"/api/client-notes/{mine['id']}", json={"resolved": True})
    assert r.status_code == 200 and r.json()["resolved"] is True
    assert api.get(f"/api/projects/{pid}/client").json()["notes"][0]["resolved"] is True
    assert api.post(f"/api/review/{link['token']}/notes", json={"text": "  "}).status_code == 422
    assert "client_note" in _actions(pid) and "client_note_resolve" in _actions(pid)


def test_a_change_request_is_a_revision_round_with_its_cost_and_approval_is_final(api, filmed):
    pid, _ = filmed
    v1, link = _link(api, pid, "final")
    r = api.post(f"/api/review/{link['token']}/decision", json={"decision": "changes", "note": "Sonu kısalt"})
    assert r.status_code == 200 and r.json()["status"] == "changes_requested"
    assert api.post(f"/api/review/{link['token']}/decision", json={"decision": "approve"}).status_code == 409
    assert api.post(f"/api/review/{link['token']}/notes", json={"at_s": 1, "text": "geç not"}).status_code == 409
    with session_scope() as s:
        s.get(Project, pid).spent_usd = 1.25                       # turlar arası harcama
    v2, link2 = _link(api, pid, "final")
    st = api.get(f"/api/projects/{pid}/client").json()
    gate = next(g for g in st["gates"] if g["gate"] == "final")
    assert gate["status"] == "pending" and gate["rounds"] == 1 and gate["limit"] == 2 and not gate["over_limit"]
    assert [v["cost_usd"] for v in gate["versions"]] == [0.0, 1.25]
    r = api.post(f"/api/review/{link2['token']}/decision", json={"decision": "approve"}).json()
    assert r["status"] == "approved"
    st = api.get(f"/api/projects/{pid}/client").json()
    assert [(v["label"], v["status"]) for v in st["versions"]] == [("v001", "changes_requested"), ("v002", "approved")]
    assert next(g for g in st["gates"] if g["gate"] == "final")["status"] == "approved"
    actions = _actions(pid)
    assert "client_reject" in actions and "client_approve" in actions and actions.count("client_send") == 2
    assert any(n["text"] == "Sonu kısalt" for n in st["notes"])          # karar açıklaması da not olarak kalır


def test_expired_and_revoked_links_stop_working(api, filmed):
    pid, _ = filmed
    _, link = _link(api, pid, "animatic", days=1)
    with session_scope() as s:
        s.execute(text("UPDATE review_links SET expires_at = now() - interval '1 minute' WHERE token = :t"), {"t": link["token"]})
    r = api.get(f"/api/review/{link['token']}")
    assert r.status_code == 410 and "süresi doldu" in r.json()["detail"]
    assert api.get(f"/api/review/{link['token']}/video").status_code == 410
    _, link = _link(api, pid, "animatic")
    assert api.delete(f"/api/review-links/{link['id']}").status_code == 200
    assert api.get(f"/api/review/{link['token']}").status_code == 410
    assert api.post(f"/api/projects/{pid}/review-links", json={"version_id": str(uuid.uuid4())}).status_code == 404
    assert api.post(f"/api/projects/{pid}/review-links", json={"version_id": link["id"], "days": 90}).status_code == 422


def test_script_and_design_gates_are_marked_from_the_panel_and_rounds_have_a_limit(api, filmed):
    pid, _ = filmed
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"revision_rounds": 1}}).status_code == 200
    for _ in range(2):
        r = api.post(f"/api/projects/{pid}/gates/script", json={"status": "changes_requested", "note": "Sahne 2 uzun"})
        assert r.status_code == 200
    assert r.json() == {"gate": "script", "status": "changes_requested", "rounds": 2, "locked": False}   # yalnızca Kabul onayı bölümü kilitler
    api.post(f"/api/projects/{pid}/gates/design", json={"status": "approved"})
    st = api.get(f"/api/projects/{pid}/client").json()
    script = next(g for g in st["gates"] if g["gate"] == "script")
    assert script["over_limit"] and script["limit"] == 1 and not script["video"]
    summary = api.get(f"/api/projects/{pid}/overview").json()["client"]["gates"]
    assert {g["gate"]: g["status"] for g in summary}["design"] == "approved"
    assert api.post(f"/api/projects/{pid}/gates/nope", json={"status": "approved"}).status_code == 404
    assert _actions(pid).count("client_reject") == 2 and "client_approve" in _actions(pid)


def test_a_newer_version_supersedes_the_old_link_which_cannot_overwrite_the_gate(api, filmed):
    pid, _ = filmed
    _, old = _link(api, pid, "final")
    _, new = _link(api, pid, "final")
    page = api.get(f"/api/review/{old['token']}")
    assert page.status_code == 200 and page.json()["status"] == "superseded"     # eski bağlantı izlemeye açık
    r = api.post(f"/api/review/{old['token']}/decision", json={"decision": "approve"})
    assert r.status_code == 409 and "yeni" in r.json()["detail"]
    assert api.post(f"/api/review/{old['token']}/notes", json={"at_s": 1, "text": "eski"}).status_code == 409
    st = api.get(f"/api/projects/{pid}/client").json()
    assert next(g for g in st["gates"] if g["gate"] == "final")["status"] == "pending"
    assert [(v["label"], v["status"]) for v in st["versions"]] == [("v001", "superseded"), ("v002", "sent")]
    assert api.post(f"/api/review/{new['token']}/decision", json={"decision": "approve"}).json()["status"] == "approved"
    assert "client_approve" in _actions(pid)


def test_the_client_app_serves_only_the_review_page_and_its_endpoints(api, filmed, tmp_path, monkeypatch):
    """Dışarıya açılan uygulama (app.review_main) stüdyonun uçlarını ve sayfalarını hiç sunmaz."""
    from fastapi.testclient import TestClient

    from app import review_main
    web = tmp_path / "web"
    for rel in ("index.html", "review/index.html", "_next/static/chunks/a.js", "settings/index.html"):
        (web / rel).parent.mkdir(parents=True, exist_ok=True)
        (web / rel).write_text(rel)
    mount = next(r for r in review_main.app.routes if getattr(r, "path", None) == "/panel")
    monkeypatch.setattr(mount, "app", review_main.ReviewFiles(web))
    pid, _ = filmed
    _, link = _link(api, pid, "final")
    client = TestClient(review_main.app)
    assert client.get(f"/api/review/{link['token']}").status_code == 200
    assert client.get(f"/api/review/{link['token']}/video", headers={"Range": "bytes=0-9"}).status_code == 206
    for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/budget", f"/api/projects/{pid}/client", "/api/meta",
                 "/api/admin", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze"}).status_code == 404
    assert client.post(f"/api/projects/{pid}/gates/final", json={"status": "approved"}).status_code == 404
    assert client.get("/panel/review/").text == "review/index.html"
    assert client.get("/panel/_next/static/chunks/a.js").status_code == 200
    for path in ("/panel/", "/panel/index.html", "/panel/settings/", "/panel/review/../settings/index.html"):
        assert client.get(path).status_code == 404, path
    assert client.get("/", follow_redirects=False).headers["location"] == "/panel/review/"
