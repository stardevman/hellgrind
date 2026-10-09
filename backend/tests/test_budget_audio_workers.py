"""Bütçe düzenleme, H3 sesinin kapatılması (audio=off) ve uç noktanın en çok worker ayarı. Ağ yok."""
import copy
import json
import pathlib

import pytest
from sqlalchemy import text

from app.budget import killswitch
from app.config import settings
from app.db import session_scope
from app.engine.providers import GenRequest
from app.live import PaidCallBlocked, runpod_h3, vast_h3
from tests.test_runpod import runpod  # noqa: F401

FLOWS = pathlib.Path(__file__).resolve().parents[2] / "comfy-workflows"
if not FLOWS.exists():
    FLOWS = pathlib.Path("/comfy-workflows")


# ---------- bütçe ----------

def test_the_budget_can_be_changed_and_the_change_is_recorded(api, api_project):
    pid = api_project(4.0)
    with killswitch.spend(pid, 1.0, "h3:480p") as ch:
        ch.actual = 1.0
    r = api.patch(f"/api/projects/{pid}", json={"budget_limit_usd": 5})
    assert r.status_code == 200 and api.get(f"/api/projects/{pid}/budget").json()["limit"] == 5.0
    assert api.patch(f"/api/projects/{pid}", json={"budget_limit_usd": 0.5}).status_code == 422     # harcananın altı
    kinds = [json.loads(x) for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    assert any(e["kind"] == "budget_changed" and e["old"] == 4.0 and e["new"] == 5.0 for e in kinds)
    with session_scope() as s:
        actions = s.execute(text("SELECT action, payload FROM curation_events WHERE project_id = :p"), {"p": pid}).all()
    assert any(a == "budget_change" and json.dumps(p).find("5") >= 0 for a, p in actions)


# ---------- ses kapalı ----------

def flow(kind: str) -> dict:
    return json.loads((FLOWS / f"{kind}.json").read_text())


@pytest.mark.parametrize("kind", ["i2v", "r2v", "t2v"])
def test_audio_off_skips_the_audio_vae_and_decoder_but_keeps_the_sampler(kind):
    f = flow(kind)
    before = {k: v["class_type"] for k, v in f.items()}
    out = vast_h3.strip_audio(copy.deepcopy(f))
    classes = [v["class_type"] for v in out.values()]
    assert "VAEDecodeAudio" not in classes
    create = next(v for v in out.values() if v["class_type"] == "CreateVideo")["inputs"]
    assert "audio" not in create and "images" in create                         # sessiz video
    audio_vaes = [k for k, v in out.items() if v["class_type"] == "VAELoader" and "audio" in v["inputs"]["vae_name"]]
    users = {(n["class_type"], key) for n in out.values() for key, v in n["inputs"].items()
             if isinstance(v, list) and len(v) == 2 and v[0] in audio_vaes}
    # Ses VAE'si yalnızca H3 düğümü onu ortak ses+video üretimi için istiyorsa kalır (r2v: audio_vae); çözücü yok.
    assert all(cls.startswith("MiniMaxH3") for cls, _ in users) and (users or not audio_vaes)
    samplers = [k for k, c in before.items() if c in ("SamplerCustomAdvanced", "KSamplerSelect", "BasicGuider")]
    assert samplers and all(k in out for k in samplers)                          # ortak gürültü giderme aynı
    links = [v[0] for n in out.values() for v in n["inputs"].values() if isinstance(v, list) and len(v) == 2
             and isinstance(v[0], str)]
    assert all(target in out for target in links)


def test_the_audio_setting_reaches_the_runpod_request(runpod):  # noqa: F811
    (pathlib.Path(settings.h3_comfy_workflow_dir) / "t2v.json").write_text((FLOWS / "t2v.json").read_text())
    provider = runpod_h3.get_live_provider()
    provider.generate(GenRequest(prompt="p", seed=1, resolution="480p", duration_s=4.0, extra={"audio": False}))
    quiet = runpod.runs[-1]["input"]["workflow"]
    provider.generate(GenRequest(prompt="p", seed=2, resolution="480p", duration_s=4.0))
    loud = runpod.runs[-1]["input"]["workflow"]
    assert "VAEDecodeAudio" not in {n["class_type"] for n in quiet.values()}
    assert "VAEDecodeAudio" in {n["class_type"] for n in loud.values()}


def test_the_final_mux_never_uses_the_clip_audio(tmp_path):
    from app.post.mux import AudioInput, build_command
    from tests.media_utils import make_video
    video = make_video(tmp_path / "v.mp4", seconds=2.0, size="160x90", fps=24)
    tone = tmp_path / "t.wav"
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", str(tone)], check=True)
    cmd = build_command(str(video), [AudioInput(path=str(tone))], [], str(tmp_path / "o.mp4"))
    maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map"]
    assert maps == ["0:v", "[aout]"]                                              # H3'ün kendi sesi (0:a) alınmaz


def test_audio_off_is_a_project_setting(api, api_project):
    pid = api_project(1.0)
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"audio": "off"}}).status_code == 200
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"audio": "mute"}}).status_code == 422


# ---------- en çok worker ----------

def test_workers_max_is_only_written_when_chosen(runpod, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "runpod_workers_max", "keep")
    assert "max" not in runpod_h3.desired_config()["workers"]                      # varsayılan değişmez
    monkeypatch.setattr(settings, "runpod_workers_max", "2")
    assert runpod_h3.desired_config()["workers"]["max"] == 2
    monkeypatch.setattr(settings, "runpod_max_pending_jobs", 1)
    with pytest.raises(PaidCallBlocked, match="bekleyen iş sınırından"):
        runpod_h3.desired_config()
