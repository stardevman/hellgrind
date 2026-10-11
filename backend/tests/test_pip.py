"""Nora — En Küçüğü (Pip) bulguları: poz, eylem ve ölçek içeren kesme karesi ("compose": sahne sıfırdan), karakterin
gerçek boyutu (varlık alanı, istem, ölçek kapısı), elle yüklenen ilk kare (panelde görünmüyordu; son karesi geniş plana
açılıyordu), yönetmen notundaki poz fiili. Ağ yok; ölçek kalibrasyonu Pip karelerinden (civcivin boyu / Nora'nın yüz boyu)."""
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from app import storage
from app.config import DEFAULT_PROJECT_SETTINGS
from app.core import chain
from app.db import session_scope
from app.engine import storyboard as sb
from app.models import Asset, Character, Scene
from app.quality import gate, scorer
from tests.test_qwen_edit import edit, edit_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

CFG = dict(DEFAULT_PROJECT_SETTINGS)
NOTE = ("Nora is KNEELING on the dusty ground, holding out her cupped right palm full of grain; the tiny yellow chick "
        "Pip stands ON her open palm pecking the grain.")


# ---------- (2) elle yüklenen ilk kare ----------

def test_an_uploaded_keyframe_can_be_viewed(api, api_project, tmp_path):
    from app.hermes import rules
    pid = api_project()
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    sid = next(s["id"] for s in api.get(f"/api/projects/{pid}/overview").json()["scenes"] if s["scene_code"] == "S001_SH01")
    good = tmp_path / "pip_closeup.png"
    Image.fromarray(np.random.default_rng(3).integers(0, 255, (180, 320, 3), dtype=np.uint8)).save(good)
    assert api.post(f"/api/scenes/{sid}/keyframe?filename=pip_closeup.png", content=good.read_bytes()).status_code == 200
    url = next(s for s in api.get(f"/api/projects/{pid}/overview").json()["scenes"] if s["id"] == sid)["keyframe_url"]
    r = api.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"           # eskiden 400 (önek izinli değildi)


def test_the_end_frame_of_an_uploaded_keyframe_keeps_its_framing(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9, "Bolt": 0.9}}))
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.48 if path.endswith("previous.jpg") else 0.21)
    pid, codes = _kivilcim(mk_project, chain_end_frame=True)
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    up = tmp_path / "up.jpg"
    Image.fromarray(np.random.default_rng(4).integers(0, 255, (704, 1280, 3), dtype=np.uint8)).save(up)
    key = "keyframes/x/up.jpg"
    storage.put_file(str(up), key, "image/jpeg")
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        sc.keyframe_key, sc.keyframe_hash = key, "upsha"
    sb.make_board(pid, codes["S001_SH03"])
    ends = [r for r in edit_runs(edit) if r["input"]["workflow"]["170:151"]["inputs"]["prompt"]
            .startswith("Image 1 is the first frame of this shot") and sb.SAME_SIZE_END in
            r["input"]["workflow"]["170:151"]["inputs"]["prompt"]]
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.board_method == "upload" and sb.clean_key(sc.board_key)
        note = sc.end_board_note
    assert len(ends) == 3 and "son kare başlangıç kadrajından geniş" in note      # son kare yüklenen kareden, dar kadrajda
    from app.engine.iteration import prompt_camera
    with session_scope() as s:
        assert "side" not in prompt_camera(s.get(Scene, codes["S001_SH03"]))         # yüklenen kare: kamera döndürülmez


# ---------- (1) gerçek boyut ----------

def test_the_real_size_reaches_the_prompts_and_the_asset_api(api, mk_project):
    from app.hermes.ingest import ingest
    from app.style.composer import scale_text
    from tests.test_chain_consistency import KIVILCIM
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    bolt = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Bolt")
    r = api.patch(f"/api/assets/{bolt['id']}", json={"scale": "tiny, palm-sized;  much smaller than a hen"})
    assert r.status_code == 200 and r.json()["version"] == bolt["version"] + 1
    again = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Bolt")
    assert again["scale"] == "tiny, palm-sized; much smaller than a hen"
    with session_scope() as s:
        asset = s.get(Asset, bolt["id"])
        text = scale_text(asset)
    assert text.startswith("Bolt's real size: tiny, palm-sized; much smaller than a hen; draw Bolt at this size")
    refs = [{"name": "Pip", "key": "k", "kind": "character", "scale": "tiny, palm-sized"}]
    assert "at its real size (tiny, palm-sized), not the size in image 1" in sb.looks_text(refs)[0]


def test_size_words_become_scale_limits_only_when_a_human_is_in_the_shot():
    from types import SimpleNamespace as N
    pip = N(name="Pip", asset_id="p", kind="non_human")
    nora = N(name="Nora", asset_id="n", kind="human")
    assets = [N(id="p", scale="tiny, palm-sized; much smaller than a hen"), N(id="n", scale=None)]
    assert sb.scale_expectations([nora, pip], assets) == [{"name": "Pip", "max_faces": 1.5}]
    assert sb.scale_expectations([pip], assets) == []                               # insan yok: ölçülemez
    assert sb.scale_expectations([nora, pip], [N(id="p", scale="small, knee-high"), assets[1]])[0]["max_faces"] == 3.0


@pytest.mark.parametrize("ratio,ok", [(2.8, False), (1.2, True)])                  # Pip SH03 dev civciv / SH01 doğru boy
def test_the_scale_gate_compares_the_character_with_the_human_face(monkeypatch, tmp_path, ratio, ok):
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(clip=0.95, face=None, composite=0.95,
                                                                                  detail={}))
    monkeypatch.setattr(scorer, "size_ratio", lambda path, target, name: ratio)
    frame = tmp_path / "f.jpg"
    Image.new("RGB", (64, 36)).save(frame)
    score = gate.score_frame(str(frame), 1, CFG, scales=[{"name": "Pip", "max_faces": 1.5}])
    assert score.detail["scale"]["Pip"] == {"ratio": ratio, "max": 1.5, "ok": ok}
    assert bool(score.detail.get("scale_mismatch")) is (not ok)


# ---------- (1)(3) compose ----------

def test_a_director_note_with_a_pose_or_a_sized_character_composes_the_frame():
    from types import SimpleNamespace as N
    assert chain.cut_method({"shot": "wide"}, {"shot": "medium", "side": 30}, compose=True) == "compose"
    assert chain.cut_method({"shot": "wide"}, {"shot": "medium"}, "cut_in", compose=True) == "cut_in"   # seçim önce
    assert chain.cut_attempts("compose", {}, {}, True) == ["compose"] * 3
    assert chain.wants_compose(N(description="Nora waves.", director_note=NOTE)) is True
    assert chain.wants_compose(N(description="Nora waves.", director_note=None), scaled=True) is True
    plain = N(description="The robot sits lifeless on the workbench.", director_note=None)
    assert chain.wants_compose(plain) is False and chain.compose_hint(plain) == ["sits"]   # tarif: yalnızca öneri
    assert chain.pose_words(NOTE) == ["kneeling", "holding", "cupped", "stands on"]


def _pip_project(edit, mk_project, tmp_path, monkeypatch, ratios):  # noqa: F811
    """Kıvılcım düzeni, Bolt avuç büyüklüğünde ve insan değil; SH04 (Bolt + Mila) yönetmen notuyla diz çökmeli."""
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9}, "look": {"Bolt": 0.85}}))
    seq = iter(ratios)
    monkeypatch.setattr(scorer, "size_ratio", lambda path, target, name: next(seq, ratios[-1]))
    pid, codes = _kivilcim(mk_project, chain_end_frame=False)
    with session_scope() as s:
        for ch in s.scalars(select(Character).where(Character.project_id == pid)):
            if ch.name == "Bolt":
                ch.kind = "non_human"
                s.get(Asset, ch.asset_id).scale = "tiny, palm-sized; much smaller than a hen"
        s.get(Scene, codes["S001_SH04"]).director_note = NOTE.replace("Nora", "Mila").replace("Pip", "Bolt")
    for code in ("S001_SH02", "S001_SH03", "S001_SH04"):
        character_shot(pid, code, tmp_path)
    return pid, codes


def test_the_kneeling_shot_is_composed_from_the_stills_with_the_real_size(edit, mk_project, tmp_path,  # noqa: F811
                                                                           monkeypatch):
    pid, codes = _pip_project(edit, mk_project, tmp_path, monkeypatch, [1.2])
    sb.make_board(pid, codes["S001_SH04"])
    run = edit_runs(edit)[-1]
    prompt = run["input"]["workflow"]["170:151"]["inputs"]["prompt"]
    assert prompt.startswith("Compose a brand-new film frame from scratch; image 1 shows")
    assert "Do not copy the framing, camera, pose or size of any reference image" in prompt
    assert "kneeling" in prompt and "Bolt's real size: tiny, palm-sized" in prompt
    assert "must have the same design (face, hair, outfit, colours) as image" in prompt
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH04"])
        assert sc.board_method == "compose" and sc.board_status != "changes_requested", sc.board_note
    assert not any(k.startswith("storyboard/") for k in [i["name"] for i in run["input"]["images"]])


def test_a_giant_chick_is_redrawn_and_finally_rejected(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    pid, codes = _pip_project(edit, mk_project, tmp_path, monkeypatch, [2.8])
    sb.make_board(pid, codes["S001_SH04"])
    composed = [r for r in edit_runs(edit) if r["input"]["workflow"]["170:151"]["inputs"]["prompt"].startswith("Compose")]
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH04"])
    assert len(composed) >= 3 and sc.board_status == "changes_requested"
    assert "boyut tutmadı: Bolt 2,8 yüz boyu > 1,5" in sc.board_note


def test_the_overview_explains_why_a_frame_is_composed(api, mk_project):
    from tests.test_cut_methods import _kivilcim
    pid, codes = _kivilcim(mk_project)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH04"]).director_note = NOTE
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH04"]["cut_method_effective"] == "compose"
    assert shots["S001_SH04"]["pose_words"] == ["kneeling", "holding", "cupped", "stands on"]
    assert shots["S001_SH03"]["cut_method_effective"] != "compose" and "sits" in shots["S001_SH03"]["compose_hint"]
    assert api.patch(f"/api/scenes/{codes['S001_SH03']}", json={"cut_method": "compose"}).status_code == 200
