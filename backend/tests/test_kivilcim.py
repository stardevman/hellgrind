""""Kıvılcım (20 sn test)" projesinden çıkan düzeltmeler: robot karakter yüzsüz kareyle geçer; kontrolü geçemeyen kare
elle onaylanıp videoya gider; karakter ve mekân referansı RunPod'da Qwen-Image-2512 ile yüksek çözünürlüklü durağan görsel
olarak üretilir, eskisi geçmişte kalır; kuyrukta kareler videolardan önce ve model değişimi en az. Sahte RunPod (ağ yok)."""
import json
import pathlib
import threading
import time

import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.budget import killswitch
from app.config import settings
from app.core.state import load_characters
from app.db import session_scope
from app.engine.providers import GenRequest
from app.live import runpod_h3
from app.models import Asset, Character, Project, Scene
from app.quality import gate, scorer
from tests.test_qwen_edit import FLOWS, character_shot, edit, edit_runs, identity, qwen_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401


def robots(pid, *names) -> None:
    with session_scope() as s:
        for ch in s.scalars(select(Character).where(Character.project_id == pid, Character.name.in_(names))):
            ch.kind = "non_human"


# ---------- kimlik ----------

def test_a_robot_frame_without_a_face_passes_on_likeness(edit, sample_scene, tmp_path, monkeypatch):  # noqa: F811
    from app.engine.iteration import keyframe_blocks

    def score(path, target):            # robotun yüzü yok; görsel benzerlik referansa yüksek
        return scorer.Result(clip=0.95, face=None, composite=0.95,
                             detail={"faces_seen": 0, "faces_measured": 0, "identity": {},
                                     "look": {name: 0.84 for name, _ in target.looks}})
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", score)
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    robots(pid, "Ayşe", "Kerem")                                   # bu çekimin iki karakteri de "robot"
    assert keyframe_blocks(pid, sid) is False and len(edit_runs(edit)) == 1      # tek kare, yeniden deneme yok
    with session_scope() as s:
        assert s.get(Scene, sid).board_status == "pending"


def test_a_frame_accepted_by_hand_goes_on_to_the_video(edit, sample_scene, identity, tmp_path, api):  # noqa: F811
    from app.engine.iteration import keyframe_blocks
    identity([0.41, 0.43, 0.45])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    assert keyframe_blocks(pid, sid) is True                        # 3 deneme, kimlik kapısı geçilemedi
    killswitch.freeze(pid, "test")
    r = api.post(f"/api/scenes/{sid}/board/accept").json()
    assert r["board_status"] == "approved" and r["frozen"] is True and r["stages"] == []   # donmuşken yalnızca onay
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.board_note.startswith("Elle onaylandı (kontrol: Kare inceleme gerekiyor")
    killswitch.resume(pid)
    assert keyframe_blocks(pid, sid) is False and len(edit_runs(edit)) == 3       # yeniden çizilmedi, video üretilebilir
    with session_scope() as s:
        actions = s.execute(text("SELECT action FROM curation_events WHERE scene_id = :s"), {"s": sid}).scalars().all()
    assert "board_approve" in actions


def test_accepting_a_frame_dispatches_the_video(api, sample_scene, dispatched):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    with session_scope() as s:
        p = s.get(Project, pid)
        p.settings = {**(p.settings or {}), "lint_strictness": "off"}
        sc = s.get(Scene, sid)
        sc.board_key, sc.board_status, sc.board_note = "storyboard/x/y.jpg", "changes_requested", "Kare inceleme gerekiyor: x"
    r = api.post(f"/api/scenes/{sid}/board/accept").json()
    assert r["frozen"] is False and "VISUAL" in r["stages"]
    assert api.post(f"/api/scenes/{codes['S001_SH02']}/board/accept").status_code == 409           # karesi yok


def test_the_character_kind_is_set_on_the_assets_page(api, sample_scene):
    pid, _ = sample_scene()
    assets = {a["name"]: a for a in api.get(f"/api/projects/{pid}/assets").json()}
    kerem = assets["Kerem"]
    assert kerem["character"]["kind"] == "human"
    r = api.patch(f"/api/assets/{kerem['id']}", json={"character_kind": "non_human"}).json()
    assert r["character"]["kind"] == "non_human" and r["version"] == kerem["version"]           # görüntü değişmez
    place = next(a for a in assets.values() if a["type"] == "location")
    assert api.patch(f"/api/assets/{place['id']}", json={"character_kind": "non_human"}).status_code == 422


# ---------- referans: Qwen-Image-2512 durağan görsel ----------

@pytest.fixture
def stills(runpod):  # noqa: F811
    """keyframe.json boyutları da taşısın (referans 1328x1328 / 1664x928)."""
    (pathlib.Path(settings.h3_comfy_workflow_dir) / "keyframe.json").write_text(json.dumps({
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "seed": "{{seed}}"}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": "{{width}}", "height": "{{height}}"}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "{{filename_prefix}}"}}}))
    return runpod


def test_a_paid_character_reference_is_a_qwen_image_still_and_the_old_one_is_kept(api, sample_scene, stills):
    pid, _ = sample_scene()
    kerem = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Kerem")
    r = api.post(f"/api/assets/{kerem['id']}/references/placeholder")
    assert r.status_code == 201, r.text
    (job,) = stills.runs
    wf = job["input"]["workflow"]
    assert (wf["7"]["inputs"]["width"], wf["7"]["inputs"]["height"]) == (1328, 1328)
    prompt = wf["6"]["inputs"]["text"]
    assert "full body" in prompt and "face clearly visible" in prompt and "plain neutral light grey" in prompt
    first = r.json()["references"]["still"]
    with session_scope() as s:
        commits = s.execute(text("SELECT provider FROM cost_ledger WHERE project_id = :p AND kind = 'commit'"),
                            {"p": pid}).scalars().all()
        a = s.get(Asset, kerem["id"])
        a.version += 1                                             # yeni seed: farklı bir kare
    assert commits == ["h3:reference:qwen"]
    stills.image = _png((40, 120, 200))
    again = api.post(f"/api/assets/{kerem['id']}/references/placeholder").json()
    assert again["references"]["still"]["sha"] != first["sha"]
    assert [h["kind"] for h in again["reference_history"]] == ["still"]           # eskisi silinmedi, geçmişte
    assert again["reference_history"][0]["entry"]["sha"] == first["sha"]


def test_a_paid_location_reference_is_a_wide_qwen_image_still(api, sample_scene, stills):
    pid, _ = sample_scene()
    place = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["type"] == "location")
    assert api.post(f"/api/assets/{place['id']}/references/placeholder").status_code == 201
    wf = stills.runs[0]["input"]["workflow"]
    assert (wf["7"]["inputs"]["width"], wf["7"]["inputs"]["height"]) == (1664, 928) and "no people" in wf["6"]["inputs"]["text"]


def _png(color) -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def test_qwen_edit_gets_the_still_not_a_turnaround_frame(sample_scene):
    from app.core.h3mode import edit_references
    pid, codes = sample_scene()
    with session_scope() as s:
        ch = load_characters(s, codes["S001_SH01"])[0]
        ch.reference_images = [f"assets/{ch.id}/turnaround/frames/0.jpg", f"assets/{ch.id}/still/s.png"]
        refs = edit_references(s.get(Scene, codes["S001_SH01"]), [ch], [], strict=False)
    assert refs[0]["key"].endswith("/still/s.png")


# ---------- kuyruk sırası ----------

def _kind(run: dict) -> str:
    wf = run["input"]["workflow"]
    if "170:151" in wf:
        return "edit"
    return "qwen" if run["input"]["hellgrind"]["kind"] == "keyframe" else "video"


def _go(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def run():
        try:
            out["result"] = fn()
        except BaseException as e:        # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _jobs(pid, tmp_path):
    png = tmp_path / "ref.png"
    Image.new("RGB", (64, 64), (200, 150, 120)).save(png)
    storage.put_file(str(png), "assets/q/still/ref.png", "image/png")
    refs = [{"name": "Mila", "key": "assets/q/still/ref.png", "kind": "character"}]
    frames = [lambda s=s: runpod_h3.keyframe_image("attic", s, pid) for s in (1, 2)]
    frames += [lambda s=s: runpod_h3.edit_keyframe_image("Mila in the attic", s, refs, "", pid) for s in (3, 4)]
    videos = [lambda s=s: runpod_h3.get_live_provider().generate(
        GenRequest(prompt="shot", seed=s, resolution="480p", duration_s=4.0, extra={"project_id": pid}))
        for s in (5, 6, 7, 8)]
    return frames, videos


def test_frames_go_before_videos_with_at_most_two_model_switches(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runpod_h3, "POLL_S", 0.01)
    edit.polls_to_finish = 3
    pid = mk_project(10.0)
    frames, videos = _jobs(pid, tmp_path)
    threads = [_go(fn) for fn in videos + frames]                  # videolar önce başlasa da
    for t, out in threads:
        t.join(30)
        assert "error" not in out, out.get("error")
    order = [_kind(r) for r in edit.runs]
    switches = sum(1 for a, b in zip(order, order[1:]) if a != b)
    assert order == ["qwen", "qwen", "edit", "edit", "video", "video", "video", "video"] and switches == 2


def test_a_late_frame_waits_until_the_running_video_group_is_done(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runpod_h3, "POLL_S", 0.01)
    edit.polls_to_finish = 10 ** 9
    pid = mk_project(10.0)
    frames, videos = _jobs(pid, tmp_path)
    running = [_go(fn) for fn in videos[:2]]
    deadline = time.monotonic() + 10
    while len(edit.runs) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    late = _go(frames[2])                                          # yeniden deneme: video grubu çalışırken gelen kare
    time.sleep(0.5)
    assert [_kind(r) for r in edit.runs] == ["video", "video"]
    raw = [json.loads(x)["kind"] for x in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    assert "runpod_model_wait" in raw
    edit.polls_to_finish = 1
    for t, out in [*running, late]:
        t.join(30)
        assert "error" not in out, out.get("error")
    assert [_kind(r) for r in edit.runs] == ["video", "video", "edit"]
