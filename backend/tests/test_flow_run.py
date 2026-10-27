"""Google Flow ile doğrudan üretim: köprü (tools/flow_bridge) + app/live/flow_bridge + app/engine/flow_run.

Kabul:
  - Ücretsiz modda Flow çağrılmaz: yerel taslak üretilir, tutar bütçeye sanal yazılır, çekim dış çekim olarak biter.
  - Ücretli modda her çekim kredi x kredi başına $ ile rezervasyondan geçer; köprü yanıtı dış çekim olarak bağlanır.
  - Köprü modülü ücretli kilitten geçer; köprü aracı gflow-cli komutunu doğru kurar.
"""
import importlib.util
import json
import pathlib

import httpx
import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db import session_scope
from app.engine import flow_pack, flow_run
from app.hermes import rules
from app.live import PaidCallBlocked
from app.models import Scene, Variation
from tests.media_utils import make_video
from tests.scene_utils import scene_row

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)
BRIDGE = pathlib.Path(__file__).resolve().parents[2] / "tools" / "flow_bridge" / "flow_bridge.py"


def _bridge_module():
    if not BRIDGE.exists():
        pytest.skip("tools/flow_bridge yok (konteyner imajında repo kökü bağlı değil)")
    spec = importlib.util.spec_from_file_location("flow_bridge_tool", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- köprü aracı (ağ yok, gflow çalıştırılmaz) ----------

def test_the_bridge_builds_each_gflow_command():
    b = _bridge_module()
    t2v = b.build_command("gflow", "t2v", "a cat", "veo-fast", "8", "16:9", {}, "/o")
    assert t2v[:3] == ["gflow", "video", "t2v"] and "a cat" in t2v and t2v[-2:] == ["-o", "/o/clip.mp4"]
    assert t2v[t2v.index("--count") + 1] == "1" and "--json" in t2v
    i2v = b.build_command("gflow", "i2v", "p", "veo-lite", "6", "16:9", {"start": "/s.jpg", "end": "/e.jpg"}, "/o")
    assert i2v[3:7] == ["--initial-frame", "/s.jpg", "--end-frame", "/e.jpg"]
    r2v = b.build_command("gflow", "r2v", "p", "veo-fast", "4", "16:9",
                          {"ref2": "/b.jpg", "ref1": "/a.jpg", "ref3": "/c.jpg"}, "/o")
    assert [r2v[i + 1] for i, x in enumerate(r2v) if x == "--ref"] == ["/a.jpg", "/b.jpg", "/c.jpg"]
    assert r2v[-2:] == ["--out-dir", "/o"]


@pytest.mark.parametrize("args, msg", [
    (("x2v", "p", "veo-fast", "8", "16:9", {}), "mode"),
    (("t2v", "p", "veo-ultra", "8", "16:9", {}), "model"),
    (("t2v", "p", "veo-fast", "10", "16:9", {}), "duration"),
    (("i2v", "p", "veo-fast", "8", "16:9", {}), "start"),
    (("r2v", "p", "veo-fast", "8", "16:9", {}), "ref1"),
    (("t2v", "  ", "veo-fast", "8", "16:9", {}), "prompt"),
])
def test_the_bridge_refuses_bad_requests_before_touching_flow(args, msg):
    b = _bridge_module()
    with pytest.raises(b.BadRequest, match=msg):
        b.build_command("gflow", *args, "/o")


def test_the_bridge_reads_multipart_and_gflow_json():
    b = _bridge_module()
    req = httpx.Request("POST", "http://x/generate", data={"mode": "i2v", "prompt": "çay bardağı"},
                        files={"start": ("kare.jpg", b"\xff\xd8jpeg")})
    fields, files = b.parse_multipart(req.headers["content-type"], req.read())
    assert fields == {"mode": "i2v", "prompt": "çay bardağı"} and files == {"start": ("kare.jpg", b"\xff\xd8jpeg")}
    out = "log line\n" + json.dumps({"media_id": "m-1", "credits_spent": 20}) + "\n"
    assert b.parse_result(out) == {"credits": 20, "media_id": "m-1"}
    assert b.parse_result("no json") == {}


# ---------- ücretli modül (sahte köprü) ----------

def test_the_live_bridge_is_locked_in_free_mode(tmp_path):
    from app.live import flow_bridge
    with pytest.raises(PaidCallBlocked):
        flow_bridge.generate({"mode": "text", "prompt": "p", "seconds": 4, "code": "S001_SH01"}, {}, str(tmp_path / "x.mp4"))


def test_the_live_bridge_sends_one_shot_and_keeps_the_clip(paid, tmp_path, monkeypatch):
    from app.live import flow_bridge
    monkeypatch.setattr(settings, "flow_bridge_url", "http://bridge.test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"], seen["url"] = request.headers["authorization"], str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, content=b"MP4DATA", headers={"content-type": "video/mp4", "x-flow-credits": "20",
                                                                 "x-flow-media-id": "m-9"})
    start = tmp_path / "start.jpg"
    start.write_bytes(b"jpg")
    out = tmp_path / "clip.mp4"
    meta = flow_bridge.generate({"mode": "frames", "prompt": "Ponpon jumps", "seconds": 6, "code": "S001_SH01"},
                                {"start": str(start)}, str(out), model="veo-fast", transport=httpx.MockTransport(handler))
    assert out.read_bytes() == b"MP4DATA" and meta == {"credits": 20, "media_id": "m-9", "model": "veo-fast"}
    assert seen["auth"] == "Bearer test-flow_bridge_token" and seen["url"] == "http://bridge.test/generate"
    assert b'name="mode"' in seen["body"] and b"i2v" in seen["body"] and b'name="start"' in seen["body"]


def test_a_bridge_failure_is_reported_in_turkish(paid, tmp_path, monkeypatch):
    from app.live import flow_bridge
    monkeypatch.setattr(settings, "flow_bridge_url", "http://bridge.test")
    transport = httpx.MockTransport(lambda r: httpx.Response(502, json={"detail": "gflow çıkış kodu 23: selector drift"}))
    with pytest.raises(flow_bridge.FlowBridgeError, match="selector drift"):
        flow_bridge.generate({"mode": "text", "prompt": "p", "seconds": 4, "code": "S"}, {}, str(tmp_path / "x.mp4"),
                             transport=transport)


def test_the_probe_reads_only_the_bridge_health(monkeypatch):
    from app.live import probe
    monkeypatch.setattr(settings, "flow_bridge_url", "http://bridge.test")
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "gflow": "gflow 0.90.0"})
                             if r.url.path == "/health" else httpx.Response(500))
    assert probe.check("flow", "tok-123456789", ok)["ok"] is True
    bad = httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "x"}))
    assert probe.check("flow", "tok-123456789", bad)["ok"] is False


# ---------- uçtan uca (veritabanı ve nesne deposu) ----------

@pytest.fixture
def sample(api, api_project, fast, eager):
    def make(**cfg):
        pid = api_project(1000.0, **{**SMALL, **cfg})
        assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
        with session_scope() as s:
            ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        return pid, ids
    return make


def _ledger(pid):
    with session_scope() as s:
        return s.execute(text("SELECT provider, kind, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"),
                         {"p": pid}).all()


def test_free_mode_makes_a_local_draft_and_charges_virtually(api, sample, monkeypatch):
    pid, ids = sample()
    monkeypatch.setattr("app.live.flow_bridge.generate", lambda *a, **k: pytest.fail("ücretsiz modda Flow çağrıldı"))
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/flow/run", json={"quality": "fast"})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == ["S001_SH01"] and r.json()["credits"] == 20 and r.json()["dry_run"] is True
    sc = scene_row(ids["S001_SH01"])
    assert (sc.status, sc.method) == ("done", "external"), sc.last_error
    est = flow_run.estimate_usd("fast")
    assert [(p, k) for p, k, _ in _ledger(pid)] == [("flow:video", "reserve"), ("flow:video", "commit")]
    assert float(_ledger(pid)[-1][2]) == pytest.approx(est)
    with session_scope() as s:
        v = s.scalar(select(Variation).where(Variation.scene_id == ids["S001_SH01"], Variation.refined_rank == 1))
        assert v.params["flow"]["dry_run"] is True and v.params["flow"]["model"] == "veo-fast"


def test_paid_mode_sends_the_pack_to_the_bridge_and_reserves_credits(api, sample, paid, dispatched, monkeypatch, tmp_path):
    pid, ids = sample()
    sent = []
    clip = make_video(tmp_path / "flow.mp4", seconds=4.0, size="320x180")

    def fake(shot, files, out_path, model=None, transport=None):
        sent.append((shot["code"], shot["mode"], sorted(files), model))
        pathlib.Path(out_path).write_bytes(pathlib.Path(clip).read_bytes())
        return {"credits": 10, "media_id": "m-1", "model": model}
    monkeypatch.setattr("app.live.flow_bridge.generate", fake)
    r = api.post(f"/api/projects/{pid}/flow/run", json={"quality": "lite", "scope": "all", "scene_ids": [ids["S002_SH01"]]})
    assert r.status_code == 200, r.text
    assert sent == [("S002_SH01", "text", [], "veo-lite")]
    sc = scene_row(ids["S002_SH01"])
    assert sc.method == "external" and sc.external_key.startswith(f"external/{ids['S002_SH01']}/")
    assert [(p, k, float(a)) for p, k, a in _ledger(pid)] == [("flow:video", "reserve", 0.2), ("flow:video", "commit", 0.2)]
    assert any(sid == ids["S002_SH01"] and "MUX" in stages for _, sid, stages in dispatched)


def test_a_failed_flow_call_releases_the_reservation(api, sample, paid, monkeypatch):
    pid, ids = sample()
    from app.live.flow_bridge import FlowBridgeError

    def boom(*a, **k):
        raise FlowBridgeError("Köprüye ulaşılamadı")
    monkeypatch.setattr("app.live.flow_bridge.generate", boom)
    with pytest.raises(FlowBridgeError):
        flow_run.run_scene(pid, ids["S001_SH02"], "fast")
    assert [(p, k) for p, k, _ in _ledger(pid)] == [("flow:video", "reserve"), ("flow:video", "release")]
    assert scene_row(ids["S001_SH02"]).method != "external"


def test_a_frozen_project_queues_nothing(api, sample):
    pid, ids = sample()
    assert api.post(f"/api/projects/{pid}/killswitch", json={"action": "freeze"}).status_code == 200
    assert api.post(f"/api/projects/{pid}/flow/run", json={"scope": "all"}).status_code == 423
    assert flow_pack.FLOW_CREDITS["quality"] == 100
