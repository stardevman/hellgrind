"""Panel asistanı: sohbet döngüsü, araçlar, onay kartı ve ücretsiz mod.

Claude hiçbir testte çağrılmaz: `core.ASK` sahte istemciyle değiştirilir. Ücretli eylemler (yeniden üretim, yeni aday,
aday seçimi) yönetmen onaylamadan çalışmaz; onaydan sonra panelin kendi uç noktalarıyla başlar (testte kaydedilir)."""
import json
import uuid

import pytest
from sqlalchemy import select

from app.assistant import actions, core, tools
from app.config import settings
from app.db import session_scope
from app.models import AssistantAction, AssistantSession, CurationEvent, Scene


# ---------- veritabanısız ----------

def test_paid_call_is_blocked_in_free_mode_without_loading_the_sdk(monkeypatch):
    import sys

    from app.live import PaidCallBlocked, assistant_llm
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-not-used")
    sys.modules.pop("anthropic", None)
    with pytest.raises(PaidCallBlocked):
        assistant_llm.ask(str(uuid.uuid4()), "sys", [], [{"role": "user", "content": "merhaba"}])
    assert "anthropic" not in sys.modules


def test_cost_uses_cache_rates_and_images_count_in_the_estimate():
    from app.live import assistant_llm

    class Usage:
        input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens = 1000, 0, 10000, 500
    tokens_in, tokens_out, usd = assistant_llm.usage_usd(Usage, "claude-opus-5-5")
    assert (tokens_in, tokens_out) == (11000, 500)
    assert usd == pytest.approx((2000 * 4.0 + 500 * 20.0) / 1e6)          # 10k okuma = 1k girdi fiyatı
    msgs = [{"role": "user", "content": [{"type": "image", "source": {"data": "x" * 400000}}]}]
    assert assistant_llm.IMAGE_TOKENS <= assistant_llm.estimate_tokens("", [], msgs) < assistant_llm.IMAGE_TOKENS + 10


def test_tool_definitions_are_valid_and_paid_tools_need_approval():
    names = [t["name"] for t in tools.TOOLS]
    assert len(names) == len(set(names))
    for t in tools.TOOLS:
        assert t["input_schema"]["type"] == "object" and t["description"]
    assert set(tools.PAID) <= set(names)
    assert {"rerender_shot", "more_candidates", "use_candidate"} == set(tools.PAID)
    assert set(names) == set(tools.PAID) | set(tools.IMPL)


# ---------- yardımcılar ----------

def text_answer(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usd": 0.01, "model": "fake"}


def tool_answer(name, inp, tid="t1"):
    return {"content": [{"type": "text", "text": "Bakıyorum."},
                        {"type": "tool_use", "id": tid, "name": name, "input": inp}],
            "stop_reason": "tool_use", "usd": 0.02, "model": "fake"}


class FakeClaude:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, project_id, system, tool_defs, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.answers.pop(0)


@pytest.fixture
def chat(mk_project, mk_scene, monkeypatch):
    pid = mk_project(limit=10.0)
    sid = mk_scene(pid, status="done")
    with session_scope() as s:
        s.get(Scene, sid).description = "Nine çocukların saçlarını inceliyor"
        row = AssistantSession(project_id=pid)
        s.add(row)
        s.flush()
        session_id = str(row.id)

    def say(text, *answers, context=None):
        fake = FakeClaude(*answers)
        monkeypatch.setattr(core, "ASK", fake)
        assert core.start(session_id, text, context)
        core.run(session_id)
        return fake
    return pid, sid, session_id, say


def code_of(sid):
    with session_scope() as s:
        return s.get(Scene, sid).scene_code


def view(session_id):
    with session_scope() as s:
        return core.view(s, session_id)


# ---------- sohbet döngüsü ----------

def test_free_mode_answers_locally_with_a_status_summary(chat, monkeypatch):
    pid, sid, session_id, _ = chat
    with session_scope() as s:
        s.get(Scene, sid).last_error = "kalite kapısı: siyah kare"
        s.get(Scene, sid).status = "needs_review"
    assert core.start(session_id, "Proje ne durumda?")
    core.run(session_id)                                  # gerçek ASK: ücretsiz modda kilit, Claude çağrılmaz
    v = view(session_id)
    reply = v["items"][-1]
    assert reply["role"] == "assistant" and reply["offline"]
    assert "Ücretsiz modda" in reply["text"] and code_of(sid) in reply["text"] and "siyah kare" in reply["text"]
    assert not v["busy"] and v["error"] is None


def test_tools_run_and_the_history_alternates_with_tool_results(chat):
    pid, sid, session_id, say = chat
    code = code_of(sid)
    fake = say("Bu çekimde sorun var mı?", tool_answer("shot_details", {"shot": code}),
               text_answer(f"{code} tarifinde nine saçları inceliyor."), context={"page": "edit", "scene_id": sid})
    first, second = fake.calls
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    user_text = [b["text"] for b in first[0]["content"]]
    assert f"[Panel notu: açık sayfa: Kurgu, seçili çekim: {code}]" in user_text and user_text[-1].startswith("Bu çekim")
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1" and not result.get("is_error")
    assert "Nine çocukların saçlarını inceliyor" in result["content"][0]["text"]
    items = view(session_id)["items"]
    assert [i["role"] for i in items] == ["user", "assistant", "tool", "assistant"]
    assert items[2]["text"] == f"{code} ayrıntılarını okudu"
    with session_scope() as s:
        assert float(s.get(AssistantSession, session_id).spent_usd) == pytest.approx(0.03)


def test_unknown_shot_is_a_tool_error_not_a_crash(chat):
    _, _, session_id, say = chat
    fake = say("S999'a bak", tool_answer("shot_details", {"shot": "S999"}), text_answer("Böyle bir çekim yok."))
    result = fake.calls[1][2]["content"][0]
    assert result["is_error"] and "S999 adlı çekim yok" in result["content"][0]["text"]
    assert view(session_id)["items"][-2]["error"]


def test_update_shot_edits_text_and_is_recorded(chat):
    pid, sid, session_id, say = chat
    say("Uçan nesneyi kaldır", tool_answer("update_shot", {"shot": code_of(sid), "description": "Büyüteç saçlarda geziniyor",
                                                             "director_note": "havada uçan hiçbir şey yok"}),
        text_answer("Tarifi değiştirdim."))
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.description == "Büyüteç saçlarda geziniyor" and sc.director_note == "havada uçan hiçbir şey yok"
        ev = s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid,
                                                   CurationEvent.action == "assistant_edit")).one()
        assert ev.payload["before"]["description"] == "Nine çocukların saçlarını inceliyor"


# ---------- onay kartı ----------

@pytest.fixture
def renders(monkeypatch):
    from app.api import scenes
    calls = []

    def fake_render(scene_id, force_visual=False, thaw=False, dry_run=False, s=None):
        calls.append((scene_id, force_visual))
        return {"stages": ["VISUAL", "LIPSYNC", "MUX"]}
    monkeypatch.setattr(scenes, "render", fake_render)
    monkeypatch.setattr(tools, "estimate", lambda s, sc, tool, inp: 0.42)
    return calls


def test_paid_action_waits_for_approval_then_runs_and_is_reported_next_turn(chat, renders, api):
    pid, sid, session_id, say = chat
    code = code_of(sid)
    fake = say("Yeniden üret", tool_answer("rerender_shot", {"shot": code, "reason": "uçan nesne"}),
               text_answer("Onay kartını gösterdim, ~0,42 $."))
    assert renders == []                                   # onay yok: hiçbir şey üretilmedi
    tool_text = fake.calls[1][2]["content"][0]["content"][0]["text"]
    assert '"awaiting_approval": true' in tool_text and "0.42" in tool_text
    card = next(i for i in view(session_id)["items"] if i["role"] == "action")["action"]
    assert card["status"] == "pending" and card["estimate_usd"] == 0.42 and card["virtual"]

    r = api.post(f"/api/assistant/actions/{card['id']}/approve", json={"scope": "once"})
    assert r.status_code == 200 and r.json()["result"]["ok"], r.text
    assert renders == [(sid, True)]
    assert api.post(f"/api/assistant/actions/{card['id']}/approve").status_code == 409     # ikinci karar yok
    with session_scope() as s:
        assert not s.get(AssistantSession, session_id).allow_all
        assert s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid,
                                                     CurationEvent.action == "assistant_approve")).one()

    fake = say("Bitti mi?", text_answer("Başlatıldı."))
    notes = [b["text"] for b in fake.calls[0][-1]["content"]]
    assert any("onaylandı ve başlatıldı" in n and code in n for n in notes)
    fake = say("Peki?", text_answer("Tamam."))             # not yalnızca bir kez eklenir
    assert not any("onaylandı" in b["text"] for b in fake.calls[0][-1]["content"])


def test_allow_all_for_the_session_runs_later_actions_without_asking(chat, renders, api):
    pid, sid, session_id, say = chat
    code = code_of(sid)
    say("Yeniden üret", tool_answer("rerender_shot", {"shot": code, "reason": "a"}), text_answer("Kart."))
    card = next(i for i in view(session_id)["items"] if i["role"] == "action")["action"]
    assert api.post(f"/api/assistant/actions/{card['id']}/approve", json={"scope": "session"}).status_code == 200
    say("Bir daha", tool_answer("rerender_shot", {"shot": code, "reason": "b"}), text_answer("Başlattım."))
    assert len(renders) == 2
    with session_scope() as s:
        statuses = [a.status for a in s.scalars(select(AssistantAction).where(AssistantAction.session_id == session_id)
                                                .order_by(AssistantAction.created_at))]
    assert statuses == ["done", "done"]
    assert api.post(f"/api/assistant/sessions/{session_id}/allow-all?on=false").json() == {"allow_all": False}
    say("Üçüncü", tool_answer("rerender_shot", {"shot": code, "reason": "c"}), text_answer("Kart."))
    assert len(renders) == 2                               # kapatınca yeniden onay ister


def test_declined_action_never_runs(chat, renders, api):
    pid, sid, session_id, say = chat
    say("Yeniden üret", tool_answer("rerender_shot", {"shot": code_of(sid), "reason": "a"}), text_answer("Kart."))
    card = next(i for i in view(session_id)["items"] if i["role"] == "action")["action"]
    assert api.post(f"/api/assistant/actions/{card['id']}/decline").json()["action"]["status"] == "declined"
    assert renders == []
    fake = say("Tamam", text_answer("Peki."))
    assert any("yönetmen reddetti" in b["text"] for b in fake.calls[0][-1]["content"])


def test_consent_flow_is_told_about_the_approval_when_present(monkeypatch, chat, renders, api):
    import sys
    import types
    granted = []
    fake_module = types.SimpleNamespace(allow=lambda *a, **k: granted.append((a, k)),
                                        target_of=lambda scene_id=None, asset_id=None: f"scene:{scene_id}")
    import app.budget as budget_pkg
    monkeypatch.setitem(sys.modules, "app.budget.consent", fake_module)
    monkeypatch.setattr(budget_pkg, "consent", fake_module, raising=False)
    pid, sid, session_id, say = chat
    say("Aday değiştir", tool_answer("rerender_shot", {"shot": code_of(sid), "reason": "a"}), text_answer("Kart."))
    card = next(i for i in view(session_id)["items"] if i["role"] == "action")["action"]
    api.post(f"/api/assistant/actions/{card['id']}/approve", headers={"X-HG-Session": "panel-1"})
    kinds = [a[1] for a, _ in granted]
    assert set(kinds) == set(actions.CONSENT_KINDS["rerender_shot"])
    assert all(a[0] == pid and a[2] == f"scene:{sid}" and a[3] == "once" and a[4] == "panel-1" for a, _ in granted)


# ---------- uç noktalar ----------

def test_endpoints_create_send_and_refuse_while_busy(mk_project, api, monkeypatch):
    pid = mk_project()
    state = api.get(f"/api/projects/{pid}/assistant").json()
    assert state["available"] is False and "Ücretsiz mod" in state["reason"] and state["sessions"] == []
    session = api.post(f"/api/projects/{pid}/assistant/sessions").json()
    monkeypatch.setattr(core, "ASK", FakeClaude(text_answer("Merhaba Enes.")))
    r = api.post(f"/api/assistant/sessions/{session['id']}/messages", json={"text": "selam", "page": "edit"})
    assert r.status_code == 200 and r.json()["items"][0]["text"] == "selam"
    v = api.get(f"/api/assistant/sessions/{session['id']}").json()        # arka plan görevi TestClient'ta bitti
    assert v["items"][-1]["text"] == "Merhaba Enes." and not v["busy"] and v["title"] == "selam"
    with session_scope() as s:
        s.get(AssistantSession, session["id"]).busy = True
    busy = api.post(f"/api/assistant/sessions/{session['id']}/messages", json={"text": "tekrar"})
    assert busy.status_code == 409
    assert api.get(f"/api/projects/{pid}/assistant").json()["sessions"][0]["title"] == "selam"


def test_images_are_stored_by_key_and_expanded_for_the_request(chat, monkeypatch):
    from app import storage
    _, _, session_id, _ = chat
    block = {"type": "image", "source": {"type": "hg_key", "key": "assistant/x/abc.jpg", "media_type": "image/jpeg"}}
    with session_scope() as s:
        core._add(s, session_id, "user", [{"type": "text", "text": "a"}])
        core._add(s, session_id, "assistant", [{"type": "tool_use", "id": "t", "name": "view_shot", "input": {}}])
        core._add(s, session_id, "user", [{"type": "tool_result", "tool_use_id": "t", "content": [block]}])
        core._add(s, session_id, "user", [{"type": "text", "text": "b"}])
    monkeypatch.setattr(storage, "read_bytes", lambda key: b"\xff\xd8jpeg")
    with session_scope() as s:
        msgs = core.history(s, session_id)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]           # ardışık kullanıcı mesajları birleşir
    img = msgs[2]["content"][0]["content"][0]
    assert img["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "/9hqcGVn"}
    assert msgs[2]["content"][1] == {"type": "text", "text": "b"}


def test_view_shot_sends_evenly_spaced_frames_of_the_final_clip(chat, monkeypatch, tmp_path):
    import shutil
    import subprocess

    from app import storage
    pid, sid, session_id, say = chat
    clip = tmp_path / "final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=8:duration=4",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    with session_scope() as s:
        s.get(Scene, sid).final_video_key = "final/x.mp4"
    stored = {}
    monkeypatch.setattr(storage, "download", lambda key, path: shutil.copy(clip, path) and path)
    monkeypatch.setattr(storage, "exists", lambda key: key in stored)
    monkeypatch.setattr(storage, "put_file", lambda path, key, content_type="": stored.setdefault(
        key, open(path, "rb").read()) and key)
    monkeypatch.setattr(storage, "read_bytes", lambda key: stored[key])
    fake = say("Bak", tool_answer("view_shot", {"shot": code_of(sid), "frames": 3}), text_answer("Gördüm."))
    content = fake.calls[1][2]["content"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 3 and len(stored) == 3 and all(b["source"]["type"] == "base64" for b in images)
    assert [b["text"] for b in content if b["type"] == "text"][1:] == ["Kare 1 (0.67 sn)", "Kare 2 (2 sn)",
                                                                       "Kare 3 (3.33 sn)"]
    with session_scope() as s:                             # kayıtta yalnızca anahtar durur
        from app.models import AssistantMessage
        raw = json.dumps([m.content for m in s.scalars(select(AssistantMessage)
                                                       .where(AssistantMessage.session_id == session_id))])
    assert "base64" not in raw and raw.count('"hg_key"') == 3


def test_paid_call_reserves_budget_and_commits_the_real_cost(mk_project, paid, consent_asks, monkeypatch):
    """Ücretli modda gerçek SDK, sahte HTTP aktarımıyla (ağa çıkılmaz): istek gövdesi, rezervasyon ve gerçek bedel."""
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from sqlalchemy import text

    from app.live import assistant_llm
    pid = mk_project(limit=5.0)
    seen = {}

    def handler(req):
        seen["body"], seen["beta"] = json.loads(req.content), req.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_sequence": None,
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "Tamam"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 200,
                                                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
    real = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: real(
        **kw, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    out = assistant_llm.ask(pid, "sys", tools.TOOLS, [{"role": "user", "content": [{"type": "text", "text": "selam"}]}])
    assert out["content"] == [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "Tamam"}]
    assert out["usd"] == pytest.approx((1000 * 4.0 + 200 * 20.0) / 1e6)
    body = seen["body"]
    assert body["model"] == settings.assistant_model and body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default" and seen["beta"] == assistant_llm.FALLBACK_BETA
    assert not {"temperature", "top_p", "top_k", "thinking"} & body.keys()
    with session_scope() as s:
        kinds = s.execute(text("SELECT kind, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY created_at"),
                          {"p": pid}).all()
    assert [k for k, _ in kinds] == ["reserve", "commit"] and float(kinds[1][1]) == pytest.approx(out["usd"], abs=1e-4)


def test_too_small_budget_is_refused_before_reserving(mk_project, paid):
    pytest.importorskip("anthropic")
    from app.live import assistant_llm
    pid = mk_project(limit=0.01)
    with pytest.raises(core.BudgetShort):
        assistant_llm.ask(pid, "sys", tools.TOOLS, [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
    from app.budget import killswitch
    assert not killswitch.is_frozen(pid)


def test_approval_is_a_real_consent_grant_for_the_shot(chat, renders, api, paid, consent_asks):
    """Ücretli modda kartın "İzin ver"i izin akışına yazılır: üretim zincirindeki dudak senkronu ikinci kez sormaz."""
    from app.budget import consent
    pid, sid, session_id, say = chat
    say("Yeniden üret", tool_answer("rerender_shot", {"shot": code_of(sid), "reason": "a"}), text_answer("Kart."))
    card = next(i for i in view(session_id)["items"] if i["role"] == "action")["action"]
    target = consent.target_of(scene_id=sid)
    assert not consent.allowed(pid, "lipsync", target)
    assert api.post(f"/api/assistant/actions/{card['id']}/approve", json={"scope": "once"}).status_code == 200
    assert consent.allowed(pid, "lipsync", target) and not consent.session_active()
    consent.require(pid, "fal_lipsync", 0.5, scene_id=sid)          # izin var: istek açılmaz, hata yok
