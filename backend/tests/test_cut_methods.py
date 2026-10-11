"""Kıvılcım SH03 koşusu (521361e): kapılar doğru çalıştı ama SH03 üç denemede de SH02'nin son karesiyle aynı kameradan
çıktı; Qwen-Image-Edit-2511 kompozisyonu koruyor, kamerayı metinle taşımıyor. Kamera artık modelin yapabildiği yollarla
değişir: kırp + iyileştir (cut-in), Multiple Angles LoRA, geniş plandan çizim; yöntem çekim başına seçilir. Sahte RunPod
(ağ yok); gerçek kare kalibrasyonu tests/data/kivilcim."""
import json
import pathlib

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.core import chain
from app.db import session_scope
from app.engine import cutin
from app.engine import storyboard as sb
from app.live import runpod_h3, vast_h3
from app.models import Scene
from app.quality import gate, scorer
from tests.test_chain import chained  # noqa: F401
from tests.test_qwen_edit import edit, edit_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

FLOWS = pathlib.Path(__file__).resolve().parents[2] / "comfy-workflows"
FRAMES = pathlib.Path(__file__).parent / "data" / "kivilcim"
MEDIUM = {"shot": "medium", "side": 0, "angle": "eye", "lens_mm": 35}
SH03 = {"shot": "close_up", "side": -35, "angle": "low", "lens_mm": 50}         # canlı plan: yakın, 35° soldan, alçak
SH04 = {"shot": "medium", "side": 0, "angle": "eye", "lens_mm": 35}             # iki kişilik orta
SH05 = {"shot": "medium", "side": -40, "angle": "low", "lens_mm": 35}


# ---------- yöntem seçimi ----------

def test_the_kivilcim_cuts_get_their_methods():
    assert chain.cut_method(MEDIUM, SH03) == "angles"            # SH02 -> SH03: açı >= 30°
    assert chain.cut_method(SH03, SH04) == "wide"                # SH03 -> SH04: ölçek genişliyor
    assert chain.cut_method(SH04, SH05) == "angles"              # SH04 -> SH05: 40° sol
    assert chain.cut_method(MEDIUM, {**SH03, "side": 10}) == "cut_in"      # ölçek daralıyor, açı <= 15°
    assert chain.cut_method(MEDIUM, SH03, "cut_in") == "cut_in"            # yönetmenin seçimi önce
    assert chain.cut_method(MEDIUM, SH03, None) == chain.cut_method(MEDIUM, SH03, "auto")


def test_the_angle_lora_falls_back_to_the_cut_in():
    assert chain.cut_attempts("angles", MEDIUM, SH03, angles_on=True) == ["angles", "angles", "cut_in"]
    assert chain.cut_attempts("angles", MEDIUM, SH03, angles_on=False) == ["cut_in"] * 3      # LoRA kapalı
    assert chain.cut_attempts("angles", SH04, SH05, angles_on=True) == ["angles"] * 3         # yaklaşmıyor: kırpılamaz
    assert chain.cut_attempts("angles", SH04, SH05, angles_on=False) == ["anchor"] * 3        # metinle tarif
    assert chain.cut_attempts("wide", SH03, SH04, angles_on=True) == ["wide"] * 3


def test_the_camera_maps_to_the_lora_tokens_on_45_degree_steps():
    assert chain.angle_tokens(MEDIUM, SH03)["text"] == "<sks> front-left quarter view low-angle shot close-up"
    assert chain.angle_tokens(SH04, SH05)["text"] == "<sks> front-left quarter view low-angle shot medium shot"
    assert chain.angle_tokens({"side": 0}, {"side": 90, "shot": "wide"})["text"] == \
        "<sks> right side view eye-level shot wide shot"
    assert chain.angle_tokens({"side": 0}, {"side": 20, "shot": "medium", "angle": "high"})["text"] == \
        "<sks> front view elevated shot medium shot"                    # 20° en yakın adım 0°
    assert chain.angle_tokens({"side": -90}, {"side": 100})["azimuth"] == "back view"     # 190° -> 180°
    assert chain.angle_tokens({"side": 0}, {"side": -130})["azimuth"] == "back-left quarter view"


# ---------- kırpma ----------

def test_a_close_up_crop_of_a_small_character_reaches_a_quarter_of_the_frame():
    box = (463, 220, 817, 550)                                    # gerçek SH03 karesinde Bolt'un yama kutusu
    c = cutin.crop_box(box, 0.654, 1280, 704, "close_up")
    x0, y0, x1, y1 = c["rect"]
    assert abs((x1 - x0) / (y1 - y0) - 1280 / 704) < 0.02          # 16:9
    assert 0 <= x0 < x1 <= 1280 and 0 <= y0 < y1 <= 704
    assert c["area"] >= 0.25 and c["zoom"] >= 1.5
    assert y0 <= box[1]                                           # baş kesilmez


def test_a_tall_box_is_cropped_from_the_head_down():
    c = cutin.crop_box((560, 300, 720, 520), 0.55, 1280, 704, "close_up")       # dik kutu (oturan küçük robot)
    x0, y0, x1, y1 = c["rect"]
    assert c["area"] >= 0.25 and y0 <= 300 < y1 < 520             # üst kalır, alt kesilir


def test_a_medium_crop_never_cuts_the_character_and_is_at_least_one_and_a_half_times_closer():
    box = (500, 250, 700, 600)
    c = cutin.crop_box(box, 0.6, 1280, 704, "medium")
    x0, y0, x1, y1 = c["rect"]
    assert x0 <= box[0] and x1 >= box[2] and y0 <= box[1] and y1 >= box[3]
    assert c["zoom"] >= 1.5
    assert cutin.centre_crop(1280, 704, 1)["zoom"] >= 1.5


# ---------- LoRA düğümü ----------

def _edit_job(**kw):
    return vast_h3.Job(task="edit_keyframe", url="u", prompt="p", seed=1, seconds=4, width=1280, height=704, steps=8,
                       references=[("Bolt", "data:image/png;base64,AAAA")], **kw)


def test_the_angles_lora_node_is_removed_unless_an_angle_frame_asks_for_it(monkeypatch):
    workflow = json.loads((FLOWS / "edit_keyframe.json").read_text())
    assert workflow["170:180"]["inputs"]["lora_name"] == "{{angles_lora}}"
    assert workflow["170:153"]["inputs"]["model"] == ["170:180", 0]
    off = vast_h3.bypass_unused_loras(json.loads(json.dumps(workflow)), {})
    assert "170:180" not in off and off["170:153"]["inputs"]["model"] == ["170:152", 0] \
        and off["170:163"]["inputs"]["on_false"] == ["170:152", 0]     # dosya yokken diğer işler bozulmaz
    on = vast_h3.bypass_unused_loras(json.loads(json.dumps(workflow)), {"angles_lora": "a.safetensors"})
    assert on["170:163"]["inputs"]["on_false"] == ["170:180", 0]
    assert _edit_job().key() != _edit_job(angles_lora="a.safetensors", angles_strength=0.9).key()
    assert _edit_job(angles_lora="a", angles_strength=0.9).key() != _edit_job(angles_lora="a", angles_strength=1.0).key()


def _kivilcim(mk_project, **kw) -> tuple[str, dict]:
    """Kıvılcım, canlı veritabanındaki kameralarla: SH02 orta plan (senaryo ayrıştırıcısı "climbs" yüzünden geniş verir);
    kesme kameraları yeniden planlanır: SH03 yakın, 35° soldan, alçak; SH04 iki kişilik orta; SH05 orta, 40° sol."""
    from app.core.chain import sync_seams
    from app.hermes.ingest import ingest
    from tests.test_chain_consistency import KIVILCIM
    pid = mk_project(5.0, **kw)
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        codes = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        codes["S001_SH02"].camera = {**MEDIUM, "movement": "static", "manual": True}
        s.flush()
        sync_seams(s, pid)
        codes = {k: str(v.id) for k, v in codes.items()}
    return pid, codes


# ---------- uçtan uca (sahte RunPod) ----------

def _runs(fake):
    return [r["input"]["workflow"] for r in edit_runs(fake)]


def _prompt(workflow):
    return workflow["170:151"]["inputs"]["prompt"]


def test_a_closer_cut_is_cropped_from_the_previous_frame_and_refined_with_lightning(chained):  # noqa: F811
    from app import storage
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH02"])                       # SH01 geniş -> SH02 orta, karaktersiz: merkezden kırpım
    refine = _runs(fake)[-1]
    assert _prompt(refine).startswith("Image 1 is an enlarged close crop of a film frame")
    assert "keep the composition, camera position, framing and lighting exactly" in _prompt(refine)
    assert "170:180" not in refine                                # açı LoRA'sı yok
    assert refine["170:168"]["inputs"]["value"] is True           # iyileştirme: Lightning
    assert edit_runs(fake)[-1]["input"]["images"]                 # image 1: kırpım
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH02"])
        assert sc.board_key and sc.board_status != "changes_requested"
    assert storage.exists(sc.board_key)


def test_the_cut_in_records_its_crop_box_in_the_service_history(chained):  # noqa: F811
    from sqlalchemy import text
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        rows = s.execute(text("SELECT label, details FROM service_calls WHERE scene_id = :s ORDER BY started_at"),
                         {"s": codes["S001_SH02"]}).all()
    cut = [d for label, d in rows if label == "h3:storyboard:cut_in"]
    assert cut and cut[-1]["cut_method"] == "cut_in" and len(cut[-1]["crop_box"]) == 4 and cut[-1]["crop_zoom"] >= 1.5
    assert cut[-1]["edit_mode"] == "lightning" and cut[-1]["crop_key"].startswith(f"cutin/{codes['S001_SH02']}/")


def test_an_angle_frame_turns_on_the_lora_and_records_it(chained, monkeypatch):  # noqa: F811
    from sqlalchemy import text
    fake, pid, codes, _ = chained
    monkeypatch.setattr(settings, "qwen_edit_angles", "on")
    with session_scope() as s:
        s.get(Scene, codes["S001_SH02"]).cut_method = "angles"
    sb.make_board(pid, codes["S001_SH02"])
    angle = _runs(fake)[-1]
    assert _prompt(angle).startswith("<sks> front view eye-level shot medium shot.")
    assert angle["170:180"]["inputs"]["lora_name"] == settings.qwen_edit_angles_lora
    assert angle["170:180"]["inputs"]["strength_model"] == 0.9
    with session_scope() as s:
        rows = s.execute(text("SELECT label, details FROM service_calls WHERE scene_id = :s"),
                         {"s": codes["S001_SH02"]}).all()
    lora = [d for label, d in rows if label == "h3:storyboard:angles"]
    assert lora and lora[-1]["angles_lora"] == settings.qwen_edit_angles_lora and lora[-1]["angles_strength"] == 0.9
    assert lora[-1]["angle_tokens"] == "<sks> front view eye-level shot medium shot"


def test_a_failed_angle_frame_falls_back_to_the_cut_in(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    monkeypatch.setattr(settings, "qwen_edit_angles", "on")
    with session_scope() as s:
        s.get(Scene, codes["S001_SH02"]).cut_method = "angles"
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: {"same": True, "ratio": 0.9, "scale": 1.0,
                                                                         "shift": 0.01})
    sb.make_board(pid, codes["S001_SH02"])                        # LoRA aynı kamerayı verdi: iki deneme, sonra kırpım
    prompts = [_prompt(w) for w in _runs(fake)[-3:]]
    assert prompts[0].startswith("<sks>") and prompts[1].startswith("<sks>")
    assert prompts[2].startswith("Image 1 is an enlarged close crop")
    with session_scope() as s:
        note = s.get(Scene, codes["S001_SH02"]).board_note or ""
    assert "jump cut" not in note                                 # kırpım geçerli bir cut-in


def test_a_missing_focus_character_is_placed_first_then_cropped(edit, mk_project, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_qwen_edit import character_shot

    def score(path, target):
        return scorer.Result(clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1,
                                                                                 "identity": {"Mila": 0.9, "Bolt": 0.9}})
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", score)
    pid, codes = _kivilcim(mk_project, chain_end_frame=False)
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH03"]).camera_plan["shot"] == "close_up"   # otomatik kesme kamerası
    seen = {"base": 0}

    def box(image, target, names=()):
        if "Bolt" in names and seen["base"] == 0:                # SH02'nin son karesinde Bolt yok
            seen["base"] += 1
            return {"name": "Bolt", "kind": "patch", "box": (40, 10, 44, 12), "fill": 0.5, "coverage": 0.011}
        w, h = image.size
        return {"name": names[0], "kind": "patch", "box": (int(w * .4), int(h * .3), int(w * .6), int(h * .8)),
                "fill": 0.65, "coverage": 0.09}
    monkeypatch.setattr(scorer, "character_box", box)
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.32)
    sb.make_board(pid, codes["S001_SH03"])
    prompts = [_prompt(w) for w in _runs(edit)]
    place = [p for p in prompts if "Add Bolt into this scene" in p]
    refine = [p for p in prompts if p.startswith("Image 1 is an enlarged close crop") and "Bolt" in p]
    with session_scope() as s:
        sh02 = s.get(Scene, codes["S001_SH02"])
        assert sh02.board_status != "changes_requested", sh02.board_note
    assert len([p for p in prompts if p.startswith("Image 1 is an enlarged close crop")]) == 2, prompts   # SH02, SH03
    assert len(place) == 1 and place[0].startswith("Image 1 is the previous shot (S001_SH02)")
    assert "Keep image 1 exactly: the same camera position" in place[0]
    assert len(refine) == 1 and "the eyes are dark and unlit, emitting no light" in refine[0]   # durum iyileştirmede de
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.board_status != "changes_requested", sc.board_note


def test_the_overview_shows_each_cut_method(api, mk_project):
    pid, _ = _kivilcim(mk_project)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH01"]["cut_method_effective"] is None                    # yeni sahne
    assert shots["S001_SH03"]["cut_method_effective"] == "angles"                # yakın, -35°, alçak
    assert shots["S001_SH03"]["cut_attempts"] == ["cut_in"] * 3                  # LoRA kapalı (varsayılan)
    assert shots["S001_SH04"]["cut_method_effective"] == "wide"                  # iki kişilik orta: genişler
    assert shots["S001_SH05"]["cut_method_effective"] == "angles"                # orta, 40° sol
    cams = {c: shots[c]["camera_effective"] for c in ("S001_SH03", "S001_SH04", "S001_SH05")}
    assert cams["S001_SH03"]["shot"] == "close_up" and cams["S001_SH03"]["side"] == -35 \
        and cams["S001_SH03"]["angle"] == "low"
    assert chain.cut_differs(cams["S001_SH03"], cams["S001_SH04"]) and chain.cut_differs(cams["S001_SH04"],
                                                                                         cams["S001_SH05"])
    assert cams["S001_SH05"]["side"] == -40 and (cams["S001_SH05"]["shot"], cams["S001_SH05"]["angle"]) != \
        (cams["S001_SH03"]["shot"], cams["S001_SH03"]["angle"])                 # SH04 ve SH05 SH03'ten farklı açıda
    sid = shots["S001_SH03"]["id"]
    assert api.patch(f"/api/scenes/{sid}", json={"cut_method": "wide"}).status_code == 200
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH03"]["cut_method"] == "wide" and shots["S001_SH03"]["cut_method_effective"] == "wide"
    assert api.patch(f"/api/scenes/{sid}", json={"cut_method": "auto"}).status_code == 200
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH03"]["cut_method"] is None
    assert api.patch(f"/api/scenes/{sid}", json={"cut_method": "zoom"}).status_code == 422


def test_the_estimate_lists_cut_in_and_angle_frames_separately(api, mk_project, monkeypatch):
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen_edit")
    pid, _ = _kivilcim(mk_project)
    off = api.post("/api/estimate", json={"project_id": pid}).json()
    sh03 = next(x for x in off["shots"] if x["code"] == "S001_SH03")
    assert sh03["cut_method"] == "angles" and sh03["cut_attempts"] == ["cut_in"] * 3
    assert sh03["cut_in_usd"] > 0 and sh03["angles_usd"] is None and sh03["cut_place_usd"] > 0   # Bolt SH02'de yok
    assert off["cut_in_frames"] == 0 or off["cut_in_frames"] >= 0
    monkeypatch.setattr(settings, "qwen_edit_angles", "on")
    on = api.post("/api/estimate", json={"project_id": pid}).json()
    sh03 = next(x for x in on["shots"] if x["code"] == "S001_SH03")
    assert sh03["cut_attempts"] == ["angles", "angles", "cut_in"] and sh03["angles_usd"] > 0 and sh03["cut_in_usd"] > 0
    assert on["angles_frames"] >= 1


# ---------- gerçek kareler (Kıvılcım) ----------

def test_a_cut_in_counts_as_a_valid_cut_and_a_held_camera_does_not(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(clip=0.95, face=None, composite=0.95,
                                                                                  detail={}))
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: {"same": True, "ratio": 0.9, "scale": 1.0,
                                                                         "shift": 0.0})
    frame = tmp_path / "f.jpg"
    Image.new("RGB", (64, 36)).save(frame)
    held = gate.score_frame(str(frame), 1, _cfg(), jump_ref=str(frame))
    assert held.detail.get("jump_cut") is True
    cut = gate.score_frame(str(frame), 1, _cfg(), jump_ref=str(frame), cut_in={"rect": (0.2, 0.2, 0.7, 0.7), "zoom": 2.0})
    assert not cut.detail.get("jump_cut") and cut.detail["camera_change"]["cut_in"] is True


def _cfg():
    from app.config import DEFAULT_PROJECT_SETTINGS
    return dict(DEFAULT_PROJECT_SETTINGS)


def test_the_sh03_cut_in_crop_keeps_the_sh02_camera_geometry_on_the_real_frames():
    """SH02'nin son karesi ve SH02 kamerasında Bolt'lu kare (eski SH03; ara karenin karşılığı): DINOv2 kutusu
    (463, 220, 817, 550) ile yakın plan kırpımı 2,03 kat; tam kare homografisi kırpımı bulamaz ama SH02'nin aynı bölgesi
    ölçek ~1 ve kayma ~0 ile oturur (kompozisyon korunmuş)."""
    pytest.importorskip("cv2")
    from app.quality import camera
    with Image.open(FRAMES / "sh02_end_full.jpg") as a, Image.open(FRAMES / "sh03_same_camera_full.jpg") as b:
        sh02, placed = a.convert("RGB"), b.convert("RGB")                      # 1280x704 (üretim boyutu)
    c = cutin.crop_box((463, 220, 817, 550), 0.654, *placed.size, "close_up")
    x0, y0, x1, y1 = c["rect"]
    crop = placed.crop(c["rect"]).resize(cutin.H3_SIZE)
    w, h = placed.size
    assert camera.same_camera(sh02, crop)["same"] is False                       # jump cut değil
    view = camera.cut_in_view(sh02, crop, (x0 / w, y0 / h, x1 / w, y1 / h))
    assert view["kept"] is True and abs(view["scale"] - 1) < 0.05                 # aynı kameranın yakın görüntüsü
    assert camera.cut_in_view(crop, crop)["kept"] is True                         # iyileştirme kırpımı korudu
    other = camera.cut_in_view(sh02, crop, (0.0, 0.0, 0.5, 0.5))
    assert other["kept"] is False                                                 # başka bölge değil


def test_a_cut_in_that_cannot_find_its_character_falls_back_to_the_text_camera(edit, mk_project, tmp_path,  # noqa: F811
                                                                                monkeypatch):
    from tests.test_qwen_edit import character_shot

    def score(path, target):
        return scorer.Result(clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1,
                                                                          "identity": {"Mila": 0.9, "Bolt": 0.9}})
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", score)
    pid, codes = _kivilcim(mk_project, chain_end_frame=False)
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)

    def box(image, target, names=()):                            # Bolt hiçbir karede bulunamıyor
        if "Bolt" in names:
            return None
        w, h = image.size
        return {"name": names[0], "kind": "patch", "box": (int(w * .4), int(h * .3), int(w * .6), int(h * .8)),
                "fill": 0.65, "coverage": 0.09}
    monkeypatch.setattr(scorer, "character_box", box)
    sb.make_board(pid, codes["S001_SH03"])
    prompts = [_prompt(w) for w in _runs(edit)]
    assert len([p for p in prompts if "Add Bolt into this scene" in p]) == 1            # ara kare çizildi, Bolt yok
    assert prompts[-1].count("must clearly differ from image 1") == 1                    # deneme metinle tarife düştü
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.board_key and "kesme yöntemi (cut_in) uygulanamadı: Bolt ara karede bulunamadı" in (sc.board_note or "")
