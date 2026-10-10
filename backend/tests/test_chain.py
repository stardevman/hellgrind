"""Sahneler arası süreklilik ("Kıvılcım" SH05 -> SH06 sıçraması): çekim geçişi (continuous | cut | new_scene), anahtar kare
zinciri (Kn = Qwen-Image-Edit, image 1 = K(n-1)), FL2VA last_frame (çekimin son karesi = sonraki çekimin ilk karesi),
karakter tasarım değişmezleri, DINOv2 benzerlik kapısı ve board/draw düzeltmesi. Sahte RunPod (ağ yok)."""
import json
import pathlib

import pytest
from sqlalchemy import select

from app import storage
from app.config import settings
from app.core import chain
from app.db import session_scope
from app.hermes import rules
from app.hermes.ingest import ingest
from app.live import runpod_h3
from app.models import Asset, Scene
from app.quality import gate, scorer
from tests.test_qwen_edit import FLOWS, edit, edit_runs, qwen_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

ATTIC = """INT. ATTIC WORKSHOP - NIGHT

Rain taps on the round attic window while a desk lamp glows over scattered gears.

A small round copper robot sits lifeless on the workbench.

The robot's eyes flicker on with a soft blue glow.

CUT TO:

The lamp tips over and the blueprint slides to the floor.

EXT. ROOFTOP - DAWN

The sun rises over the city.
"""


def png(color) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, "PNG")
    return buf.getvalue()


# ---------- geçiş türü ----------

def test_transitions_come_from_the_script():
    """Otomatik sınıflama: SH01 geniş -> SH02 orta (kadraj değişti) kesme; SH02 -> SH03 aynı kadro ve kadraj: devam;
    "CUT TO:" kesmeyi zorlar; yeni başlık new_scene."""
    shots = rules.parse(ATTIC)["shots"]
    assert [(s["scene_id"], s["transition"]) for s in shots] == [
        ("S001_SH01", "new_scene"), ("S001_SH02", "cut"), ("S001_SH03", "continuous"), ("S001_SH04", "cut"),
        ("S002_SH01", "new_scene")]
    assert shots[1]["transition_reason"] == "kadraj değişti: geniş → orta"
    assert shots[3]["transition_hint"] == "cut" and shots[3]["transition_reason"] == "senaryoda CUT TO"


def test_the_transition_is_stored_and_can_be_changed_per_shot(api, mk_project):
    pid = mk_project(5.0)
    ingest(pid, ATTIC, auto_music=False)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH03"]["transition"] == "continuous" and shots["S001_SH02"]["chain_parent"] == "S001_SH01"
    assert shots["S001_SH04"]["transition"] == "cut" and shots["S002_SH01"]["chain_parent"] is None
    sid = shots["S001_SH02"]["id"]
    api.patch(f"/api/scenes/{sid}", json={"transition_in": "new_scene"})
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert (shots["S001_SH02"]["transition"], shots["S001_SH02"]["chain_parent"]) == ("new_scene", None)
    api.patch(f"/api/scenes/{sid}", json={"transition_in": "auto"})
    with session_scope() as s:
        assert s.get(Scene, sid).transition_in is None


# ---------- anahtar kare zinciri ----------

@pytest.fixture
def chained(edit, monkeypatch, mk_project):  # noqa: F811
    """Sahte RunPod (keyframe + edit_keyframe) + gerçek skorlayıcı yerine sabit ölçüm; süreklilik ayarlanabilir."""
    state = {"continuity": [0.9] * 20}

    def score(path, target):
        return scorer.Result(clip=0.95, face=None, composite=0.95, detail={"faces_seen": 0, "identity": {}})
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", score)
    monkeypatch.setattr(scorer, "continuity", lambda path, previous: state["continuity"].pop(0))
    pid = mk_project(5.0, chain_end_frame=False)          # çekim sonu kareleri ayrı sınanır (test_chain_consistency)
    ingest(pid, ATTIC, auto_music=False)
    with session_scope() as s:
        codes = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return edit, pid, codes, state


def test_frames_are_drawn_in_chain_order_from_the_previous_frame(chained):
    from app.engine import storyboard
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH03"])                # 3. kare istenince 1 ve 2 önce çizilir
    kinds = ["edit" if "170:151" in r["input"]["workflow"] else "qwen" for r in fake.runs]
    assert kinds == ["qwen", "edit", "edit"]
    prompts = [r["input"]["workflow"]["170:151"]["inputs"]["prompt"] for r in edit_runs(fake)]
    assert prompts[0].startswith("Image 1 shows this location in an earlier shot (S001_SH01)")   # K2: kesme, geniş plandan
    assert "Move the camera closer" in prompts[0] and "must clearly differ from image 1" in prompts[0]
    assert prompts[1].startswith("Image 1 is the previous shot of this scene")                  # K3: devam
    assert "Keep the same room, layout, props" in prompts[1]                                    # K3: devam
    assert all(len(r["input"]["images"]) == 1 for r in edit_runs(fake))          # karakter yok: yalnızca önceki kare
    with session_scope() as s:
        k1, k2, k3 = (s.get(Scene, codes[c]) for c in ("S001_SH01", "S001_SH02", "S001_SH03"))
        assert k1.board_parent is None and k2.board_parent == k1.board_sha and k3.board_parent == k2.board_sha
        assert k1.status == k2.status != "storyboard"                  # zincirin önceki kareleri durum değiştirmedi


def test_a_cut_keeps_the_place_but_frees_the_camera(chained):
    from app.engine import storyboard
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH04"])
    cut = edit_runs(fake)[-1]["input"]["workflow"]["170:151"]["inputs"]["prompt"]
    assert "Move the camera" in cut and "must clearly differ from image 1" in cut and "characters' state" in cut
    storyboard.make_board(pid, codes["S002_SH01"])                # yeni sahne: zincir kırılır, referanslardan
    assert "170:151" not in fake.runs[-1]["input"]["workflow"]


def test_a_redrawn_frame_makes_the_next_frames_stale(chained, api):
    from app.engine import storyboard
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH03"])
    fake.image = png((30, 160, 90))                               # yeniden çizilen kare farklı
    storyboard.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)   # yönetmen K1'i yeniden çizdi
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH02"]["chain_stale"] is True and shots["S001_SH01"]["chain_redraw"] == ["S001_SH02", "S001_SH03",
                                                                                                "S001_SH04"]
    before = len(fake.runs)
    storyboard.make_board(pid, codes["S001_SH03"])                # eski zincir kareleri sırayla yeniden çizilir
    assert len(fake.runs) == before + 2
    with session_scope() as s:
        k1, k2 = s.get(Scene, codes["S001_SH01"]), s.get(Scene, codes["S001_SH02"])
        assert k2.board_parent == k1.board_sha


def test_a_frame_that_breaks_continuity_is_redrawn_and_the_chain_waits(chained):
    from app.engine import storyboard
    fake, pid, codes, state = chained
    state["continuity"] = [0.2, 0.25, 0.3]                         # K2 (kesme) üç denemede de bambaşka oda
    storyboard.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:
        k2, k3 = s.get(Scene, codes["S001_SH02"]), s.get(Scene, codes["S001_SH03"])
        assert k2.board_status == "changes_requested" and "süreklilik 0,30 < 0,35" in k2.board_note
        assert k3.board_key is None                                # K3, kabul edilmemiş K2'den çizilmez
    assert len(edit_runs(fake)) == 3


def test_board_draw_redraws_on_request_and_refuses_without_a_paid_source(chained, api, monkeypatch):
    fake, pid, codes, _ = chained
    from app.engine import storyboard
    storyboard.make_board(pid, codes["S001_SH01"])
    calls = []
    monkeypatch.setattr("app.worker.tasks.storyboard_scene.si",
                        lambda **kw: type("C", (), {"apply_async": lambda self: calls.append(kw)})())
    r = api.post(f"/api/scenes/{codes['S001_SH01']}/board/draw?paid=true")
    assert r.status_code == 200 and calls and calls[0]["paid"] is True
    before = len(fake.runs)
    storyboard.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)   # bekleyen kare de yeniden çizilir
    assert len(fake.runs) == before + 1
    monkeypatch.setattr(runpod_h3, "keyframes_ready", lambda kind="keyframe": False)
    monkeypatch.setattr(settings, "fal_key", "")
    r = api.post(f"/api/scenes/{codes['S001_SH02']}/board/draw?paid=true")
    assert r.status_code == 409 and "ücretli kare kaynağı yok" in r.text


# ---------- FL2VA last_frame ----------

def test_last_frame_is_in_the_workflow_and_the_request(chained):
    from app.engine import storyboard
    from app.engine.iteration import _prepare
    from app.engine.providers import GenRequest
    fake, pid, codes, _ = chained
    flows = pathlib.Path(settings.h3_comfy_workflow_dir)
    (flows / "i2v.json").write_text((FLOWS / "i2v.json").read_text())        # gerçek FL2VA şablonu (last_frame düğümü)
    storyboard.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:                                    # sahte RunPod her kareye aynı görseli verir: K3 farklı olsun
        k3 = s.get(Scene, codes["S001_SH03"]).board_key
    path = pathlib.Path(settings.work_dir) / "k3.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png((200, 40, 40)))
    storage.put_file(str(path), storyboard.clean_key(k3), "image/jpeg")
    ctx = _prepare(pid, codes["S001_SH02"], "run")               # SH03 devam ediyor: son kare = K3
    assert ctx.keyframe and ctx.last_frame and ctx.target.next_frame.endswith("_clean.jpg")
    runpod_h3.get_live_provider().generate(GenRequest(
        prompt="p", seed=1, resolution="480p", duration_s=4.0, image_url=ctx.keyframe,
        extra={**ctx.render, "last_frame": ctx.last_frame}))
    wf, images = fake.runs[-1]["input"]["workflow"], fake.runs[-1]["input"]["images"]
    h3 = next(n for n in wf.values() if n["class_type"] == "MiniMaxH3ImageToVideo")["inputs"]
    assert wf[h3["last_frame"][0]]["inputs"]["image"] in {i["name"] for i in images} and len(images) == 2
    assert wf[h3["first_frame"][0]]["inputs"]["image"] != wf[h3["last_frame"][0]]["inputs"]["image"]
    assert ctx.render["last_frame_key"] == ctx.target.next_frame
    last = _prepare(pid, codes["S001_SH03"], "run")              # sonraki çekim kesme: yalnızca ilk kare
    assert last.keyframe and last.last_frame is None
    runpod_h3.get_live_provider().generate(GenRequest(prompt="p", seed=2, resolution="480p", duration_s=4.0,
                                                      image_url=last.keyframe, extra=dict(last.render)))
    wf = fake.runs[-1]["input"]["workflow"]
    h3 = next(n for n in wf.values() if n["class_type"] == "MiniMaxH3ImageToVideo")["inputs"]
    assert "last_frame" not in h3 and "{{" not in json.dumps(wf)


def test_a_clip_that_does_not_end_on_the_next_frame_is_rejected(monkeypatch, tmp_path):
    from tests.media_utils import make_video
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(scorer, "score_video", lambda path, target: scorer.Result(
        clip=0.95, face=None, composite=0.95, detail={"faces_seen": 0, "identity": {}}))
    monkeypatch.setattr(scorer, "arrival", lambda path, key, target: {"score": 0.55, "dino": 0.55, "identity": None})
    clip = make_video(tmp_path / "c.mp4", seconds=4.0, size="320x180", fps=24)
    score = gate.score_video(str(clip), 1, {"clip_threshold": 0.85, "transition_threshold": 0.8},
                             scorer.Target(next_frame="storyboard/x/k_clean.jpg"))
    assert not score.passed and score.anomalies[-1] == {"type": "transition_jump", "score": 0.55, "threshold": 0.8}


# ---------- tasarım değişmezleri ve DINOv2 ----------

def test_invariants_go_into_every_frame_and_video_prompt(api, sample_scene):
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene
    pid, codes = sample_scene()
    kerem = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Kerem")
    r = api.patch(f"/api/assets/{kerem['id']}", json={"invariants": ["exactly one bent antenna with a small bulb",
                                                                     "big round blue glass eyes"]}).json()
    assert r["invariants"] == ["exactly one bent antenna with a small bulb", "big round blue glass eyes"]
    assert r["version"] == kerem["version"] + 1                    # görüntüyü değiştirir: çekimler yeniden üretilir
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH01"])
        prompt = compose_for_scene(sc, load_characters(s, sc.id), load_assets(s, sc.id), None).prompt
    assert "Kerem must always have: exactly one bent antenna with a small bulb; big round blue glass eyes." in prompt


def test_the_dino_gate_rejects_a_changed_design():
    """Kıvılcım ölçümü: Bolt SH03/SH05 kırpım DINOv2 0,85/0,89 (geçer), iki antenli SH06 0,82 (elenir)."""
    bolt = scorer.Target(looks=(("Bolt", ("assets/b/still/b.png",)),))
    cfg = {"look_threshold": 0.80, "look_dino_threshold": 0.84}
    detail = lambda clip, dino: {"look": {"Bolt": clip}, "look_dino": {"Bolt": dino}}     # noqa: E731
    assert gate.identity_anomalies(detail(0.852, 0.853), bolt, cfg) == []                     # SH03
    assert gate.identity_anomalies(detail(0.877, 0.893), bolt, cfg) == []                     # SH05
    assert gate.identity_anomalies(detail(0.764, 0.821), bolt, cfg)[0]["type"] == "low_likeness"   # SH06 (OpenCLIP)
    assert gate.identity_anomalies(detail(0.81, 0.821), bolt, cfg) == [
        {"type": "low_likeness", "look_dino": {"Bolt": 0.821}, "threshold": 0.84}]             # DINOv2 ayrıntıyı yakalar
