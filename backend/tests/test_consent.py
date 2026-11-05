"""Ücretli ek işlem izni (app/budget/consent.py): video üretimi dışındaki her ücretli çağrı (dudak senkronu, görsel,
seslendirme, LLM ile metin, upscale) yönetmenin izni olmadan yapılmaz ve bütçeden rezervasyon almaz.

Kabul:
  - Ücretsiz modda hiçbir şey sorulmaz; video üretimi ve makinenin boşta bedeli izin istemez.
  - İzin yoksa çağrı yapılmaz, rezervasyon alınmaz, tek bir bekleyen istek açılır.
  - "İzin ver" yalnızca o işlemi (proje, hedef, tür) açar; "Bu oturum için tümüne izin ver" panel oturumu sürdükçe
    hepsini açar ve yalnızca izni veren oturum tazeler.
  - Sahne aşaması izin beklerken çekim `awaiting_consent` olur, zincirin sonraki aşamaları çalışmaz; izin verilince
    sahne yeniden planlanır, tek başına görev yeniden kuyruğa verilir.
  - Reddedilen dudak senkronu çekimi senkronsuz bitirir; diğer türlerde çekim hata olarak işaretlenir.
  - İstek içindeki çağrı 428 döner; kararlar kürasyon kaydına yazılır.
"""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.budget import consent, killswitch
from app.config import settings
from app.db import session_scope
from app.models import Scene


@pytest.fixture
def asks(consent_asks, paid, pg):
    r = killswitch.r()

    def wipe():
        for key in r.scan_iter("hg:consent:*"):
            r.delete(key)
    wipe()
    yield
    wipe()


def fake_task(name: str, args=None, kwargs=None):
    return SimpleNamespace(name=name, request=SimpleNamespace(id=uuid.uuid4().hex, args=args or [], kwargs=kwargs or {}))


def ask(pid, label="fal:lipsync", usd=0.32, scene_id=None) -> dict:
    with pytest.raises(consent.ConsentRequired) as e:
        consent.require(pid, label, usd, scene_id=scene_id)
    return e.value.request


def status_of(sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        return sc.status, sc.last_error


# ---------- tür (veritabanı gerekmez) ----------

@pytest.mark.parametrize("label, kind", [
    ("fal:lipsync", "lipsync"), ("elevenlabs", "voice"), ("anthropic", "llm"), ("upscale:1080p", "upscale"),
    ("h3:storyboard:qwen_edit", "image"), ("flux_lora:first_frame:480p", "image"), ("fal_qwen:studio", "image"),
    ("flux_kontext:storyboard", "image"), ("h3:reference:qwen", "image"), ("bilinmeyen", "other"),
    ("h3max:480P", None), ("seedance25:1080p:complete", None), ("h3:720p", None), ("v2v:720p:previs", None),
    ("h3max:768P:motion_test", None), ("h3:idle", None),
])
def test_video_generation_and_idle_billing_never_ask_everything_else_does(label, kind):
    assert consent.kind_of(label) == kind


# ---------- izin ----------

def test_free_mode_never_asks(asks, mk_project, monkeypatch):
    monkeypatch.setattr(settings, "dry_run", True)
    pid = mk_project()
    consent.require(pid, "fal:lipsync", 0.32)
    assert consent.pending(pid) == []


def test_a_paid_extra_call_waits_for_consent_and_reserves_nothing(asks, mk_project):
    pid = mk_project()
    for _ in range(2):                                     # yeniden deneme aynı isteği bekler: tek istek
        with pytest.raises(consent.ConsentRequired) as e:
            with killswitch.spend(pid, 0.32, "fal:lipsync"):
                pytest.fail("izin yokken ücretli çağrı yapıldı")
    assert "izin" in str(e.value) and "0.32" in str(e.value)
    [req] = consent.pending(pid)
    assert (req["kind"], req["usd"], req["target"]) == ("lipsync", 0.32, "project")
    with session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM cost_ledger WHERE project_id = :p"), {"p": pid}).scalar() == 0
    events = [json.loads(x)["kind"] for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    assert events.count("consent_requested") == 1


def test_video_generation_goes_straight_to_the_budget(asks, mk_project):
    pid = mk_project()
    with killswitch.spend(pid, 0.2, "h3max:480P") as charge:
        charge.actual = 0.2
    assert consent.pending(pid) == []


def test_allow_once_opens_only_that_operation(asks, mk_project, mk_scene):
    pid = mk_project()
    a, b = mk_scene(pid), mk_scene(pid)
    req = ask(pid, scene_id=a)
    out = consent.grant([req["id"]], "once")
    assert out["granted"] == [req["id"]] and out["retry"][0]["resumed"] is None     # istek içi: panel yineler
    consent.require(pid, "fal:lipsync", 0.32, scene_id=a)                            # aynı işlem: geçer
    ask(pid, scene_id=b)                                                              # başka çekim: sorulur
    ask(pid, "flux_lora:storyboard", 0.04, scene_id=a)                               # başka tür: sorulur


def test_session_grant_allows_everything_and_only_its_own_tab_keeps_it_alive(asks, mk_project):
    pid = mk_project()
    req = ask(pid, "elevenlabs", 0.02)
    ask(pid, "fal_qwen:studio", 0.03)
    out = consent.grant([req["id"]], "session", session_id="sekme-1")
    assert len(out["granted"]) == 2 and consent.pending() == []                      # bekleyenlerin hepsi
    for label in ("fal:lipsync", "flux_lora:storyboard", "anthropic", "upscale:1080p"):
        consent.require(pid, label, 0.1)
    r = killswitch.r()
    r.expire(consent.SESSION, 5)
    consent.touch("sekme-2")
    assert r.ttl(consent.SESSION) <= 5
    consent.touch("sekme-1")
    assert r.ttl(consent.SESSION) > consent.SESSION_TTL_S - 5
    assert consent.revoke_session() and not consent.session_active()
    ask(pid)


def test_a_waiting_shot_stops_its_chain_and_resumes_when_allowed(asks, mk_project, mk_scene, monkeypatch):
    from app.api import plan
    from app.worker import tasks
    pid = mk_project()
    sid = mk_scene(pid, status="lipsync")
    calls = []

    def work():
        calls.append(1)
        consent.require(pid, "fal:lipsync", 0.32, scene_id=sid)
        return "synced"
    assert tasks._guard(fake_task("hg.lipsync_scene"), pid, sid, work) == "awaiting_consent"
    status, error = status_of(sid)
    assert status == "awaiting_consent" and "Dudak senkronu" in error
    assert tasks._guard(fake_task("hg.mux_scene"), pid, sid, work) == "skip (izin bekleniyor)"
    assert calls == [1]
    [req] = consent.pending(pid)
    assert req["resume"] == {"type": "scene", "scene_id": sid} and req["scene_code"]

    dispatched = []
    monkeypatch.setattr(plan, "dispatch_result", lambda s, sc, **kw: dispatched.append(str(sc.id)) or
                        {"stages": ["LIPSYNC", "MUX"]})
    out = consent.grant([req["id"]], "once")
    assert out["resumed"][0]["resumed"] == "scene" and dispatched == [sid]
    consent.require(pid, "fal:lipsync", 0.32, scene_id=sid)                           # devam eden aşama: izinli
    with session_scope() as s:
        actions = [a for (a,) in s.execute(text("SELECT action FROM curation_events WHERE project_id = :p"), {"p": pid})]
    assert "consent_grant" in actions


def test_a_standalone_task_is_sent_again_with_the_same_arguments(asks, mk_project, monkeypatch):
    from app.worker.celery_app import celery
    pid = mk_project()
    req = ask(pid, "fal_qwen:studio", 0.03)
    consent.set_resume(req["id"], consent.resume_for_task(fake_task("hg.studio_image", [pid, "gorsel-1"])))
    sent = []
    monkeypatch.setattr(celery, "send_task", lambda name, args, kwargs: sent.append((name, args, kwargs)))
    consent.grant([req["id"]], "once")
    assert sent == [("hg.studio_image", [pid, "gorsel-1"], {})]
    # ödenmiş videoyu yeniden üretebilecek görevler (yeni aday, tamamlama) yeniden gönderilmez: sahne planlanır
    assert consent.resume_for_task(fake_task("hg.more_candidates", [pid, "s", 1]), "s") == {"type": "scene",
                                                                                              "scene_id": "s"}


def test_denied_lipsync_finishes_unsynced_and_other_kinds_fail_the_shot(asks, mk_project, mk_scene, monkeypatch):
    from app.api import plan
    from app.worker import tasks
    pid = mk_project()
    lip, img = mk_scene(pid, status="lipsync"), mk_scene(pid, status="storyboard")

    def needs(label, sid):
        def work():
            consent.require(pid, label, 0.3, scene_id=sid)
        return work
    tasks._guard(fake_task("hg.lipsync_scene"), pid, lip, needs("fal:lipsync", lip))
    tasks._guard(fake_task("hg.storyboard_scene"), pid, img, needs("flux_lora:storyboard", img))
    dispatched = []
    monkeypatch.setattr(plan, "dispatch_result", lambda s, sc, **kw: dispatched.append(str(sc.id)) or {"stages": ["MUX"]})
    by_scene = {r["scene_id"]: r["id"] for r in consent.pending(pid)}
    consent.deny([by_scene[lip], by_scene[img]])
    assert consent.denied(lip, "lipsync") and dispatched == [lip]                    # senkronsuz biter
    status, error = status_of(img)
    assert status == "failed" and error.startswith("İzin verilmedi: Görsel üretimi")
    consent.clear_denials(lip)                                                       # açık üretim isteği: yeniden sorulur
    assert not consent.denied(lip, "lipsync")


# ---------- API ----------

def test_the_panel_lists_grants_and_denies_requests(asks, api, mk_project):
    pid = mk_project()
    req = ask(pid)
    state = api.get("/api/consent", params={"project_id": pid}).json()
    assert state["paid_mode"] and [r["id"] for r in state["pending"]] == [req["id"]]
    assert state["kinds"]["lipsync"] == "Dudak senkronu" and state["session"]["active"] is False
    assert api.post(f"/api/consent/{req['id']}/grant", json={"scope": "session"}).status_code == 422
    assert api.post(f"/api/consent/{req['id']}/grant", json={"scope": "session", "session_id": "s1"}).status_code == 200
    assert api.get("/api/consent", headers={"X-HG-Session": "s1"}).json()["session"]["active"]
    assert api.post("/api/consent/yok/deny", json={}).status_code == 404
    assert api.post("/api/consent/session/revoke").json()["revoked"] is True


def test_assistant_can_allow_before_starting_an_action(asks, api, mk_project, mk_scene):
    pid = mk_project()
    sid = mk_scene(pid)
    res = api.post("/api/consent/allow", json={"project_id": pid, "kind": "lipsync", "scene_id": sid, "by": "assistant"})
    assert res.status_code == 200 and res.json()["target"] == f"scene:{sid}"
    consent.require(pid, "fal:lipsync", 0.32, scene_id=sid)
    with session_scope() as s:
        payload = s.execute(text("SELECT payload FROM curation_events WHERE project_id = :p AND action = 'consent_grant'"),
                            {"p": pid}).scalar()
    assert payload["by"] == "assistant"


def test_an_in_request_paid_call_answers_428_with_the_request(asks, mk_project):
    from app.main import consent_required
    pid = mk_project()
    with pytest.raises(consent.ConsentRequired) as e:
        consent.require(pid, "fal_qwen:studio", 0.03)
    res = asyncio.run(consent_required(None, e.value))
    body = json.loads(res.body)
    assert res.status_code == 428 and body["consent"]["kind"] == "image" and "izin" in body["detail"]
