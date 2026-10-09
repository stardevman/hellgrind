"""Karakterli çekimin ilk karesi Qwen-Image-Edit-2511 ile, video FL2VA ile (RunPod'daki ikinci test turundan):
- karakterli çekim varsayılan olarak FL2VA; kare edit_keyframe.json ile karakterin onaylı referansından (image 1 ana
  karakter), H3 tuvalinde (1280x704);
- kare kimlik kapısından geçer (SFace, referansa karşı ≥ identity_threshold), en çok 2 yeniden deneme; geçemezse
  "Kare inceleme gerekiyor" ve video üretilmez; her deneme harcama geçmişine yazılır;
- kuyrukta Qwen-Image ve Qwen-Image-Edit kareleri ayrı model grupları; Seedance aynı kareyi kullanır;
- tahmini maliyet (çekim başına ve toplam) ve "Üretim (ekonomik)" ön ayarı.
Sahte RunPod (ağ yok); gerçek bir RunPod işi çalışmaz."""
import json
import pathlib

import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.budget import pricing
from app.config import settings
from app.core.state import load_characters
from app.db import session_scope
from app.models import Asset, Scene
from app.quality import gate, scorer
from tests.test_runpod import runpod  # noqa: F401  (sahte RunPod fikstürü)

FLOWS = pathlib.Path(__file__).resolve().parents[2] / "comfy-workflows"
if not FLOWS.exists():                                        # konteynerde depo kökü /comfy-workflows'a bağlanır
    FLOWS = pathlib.Path("/comfy-workflows")


@pytest.fixture
def edit(runpod, tmp_path):  # noqa: F811
    """Sahte RunPod + gerçek edit_keyframe.json şablonu (budama ve doldurma yolu sınansın)."""
    target = pathlib.Path(settings.h3_comfy_workflow_dir) / "edit_keyframe.json"
    target.write_text((FLOWS / "edit_keyframe.json").read_text())
    return runpod


def character_shot(pid, code: str, tmp_path, lock: bool = True) -> str:
    """Çekimin karakterlerine durağan referans görseli ve kilitli varlık verir (Qwen-Image-Edit bunları kullanır)."""
    with session_scope() as s:
        sc = s.scalars(select(Scene).where(Scene.project_id == pid, Scene.scene_code == code)).one()
        sc.face_expected = True                        # köprü çekimi geniş aksiyon planı: kimlik kapısı açık sınansın
        for i, ch in enumerate(load_characters(s, sc.id)):
            png = tmp_path / f"still_{ch.id}.png"
            Image.new("RGB", (64, 64), (200, 150 - 40 * i, 120)).save(png)            # her karakterin kendi görseli
            key = f"assets/{ch.id}/still/{ch.id}.png"
            storage.put_file(str(png), key, "image/png")
            ch.reference_images = [key]
            if ch.asset_id and lock:
                s.get(Asset, ch.asset_id).status = "locked"
        return str(sc.id)


@pytest.fixture
def identity(monkeypatch):
    """Gerçek skorlayıcı yerine sıralı kimlik ölçümleri: install([0.47, 0.71]) -> 1. kare 0.47, 2. kare 0.71."""
    def install(values: list[float]):
        seq = iter(values)

        def score(path, target):
            v = next(seq)
            return scorer.Result(clip=0.95, face=None, composite=0.95,
                                 detail={"faces_seen": 1, "identity": {name: v for name, _ in target.faces}})
        monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
        monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
        monkeypatch.setattr(scorer, "score_image", score)
    return install


def edit_runs(fake) -> list[dict]:
    return [r for r in fake.runs if "170:151" in r["input"]["workflow"]]


def qwen_runs(fake) -> list[dict]:
    return [r for r in fake.runs if r["input"]["hellgrind"]["kind"] == "keyframe" and "170:151" not in r["input"]["workflow"]]


def ledger(pid, label: str) -> list[float]:
    with session_scope() as s:
        return [float(x) for x in s.execute(text(
            "SELECT amount_usd FROM cost_ledger WHERE project_id = :p AND kind = 'commit' AND provider = :l ORDER BY created_at"),
            {"p": pid, "l": label}).scalars()]


def test_a_character_shot_gets_its_first_frame_from_qwen_edit_on_the_h3_canvas(edit, sample_scene, identity, tmp_path):
    from app.engine import storyboard
    from app.engine.iteration import keyframe_blocks, keyframe_of
    identity([0.71])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    assert keyframe_blocks(pid, sid) is False                       # kare çizildi, kapıdan geçti: video üretilebilir
    runs = edit_runs(edit)
    assert len(runs) == 1 and qwen_runs(edit) == []
    wf, images = runs[0]["input"]["workflow"], runs[0]["input"]["images"]
    assert (wf["172"]["inputs"]["width"], wf["172"]["inputs"]["height"]) == (1280, 704)
    assert wf["170:168"]["inputs"]["value"] is False                 # Lightning varsayılan kapalı (40 adım, cfg 4)
    prompt = wf["170:151"]["inputs"]["prompt"]
    assert "is the person in image 1" in prompt and "is the person in image 2" in prompt and "image 3" not in prompt
    assert len(images) == 2 and "171" not in wf and "image3" not in wf["170:149"]["inputs"]   # iki karakter, mekân yok
    assert wf["41"]["inputs"]["image"] == images[0]["name"] and wf["83"]["inputs"]["image"] == images[1]["name"]
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert (sc.board_source, sc.board_status, sc.h3_mode) == ("qwen_edit", "pending", None)
        assert keyframe_of(sc, sc.board_hash) == storyboard.clean_key(sc.board_key)
    assert len(ledger(pid, "h3:storyboard:qwen_edit")) == 1


def test_a_frame_that_fails_the_identity_gate_three_times_stops_the_shot(edit, sample_scene, identity, tmp_path):
    from app.engine.iteration import keyframe_blocks
    identity([0.41, 0.47, 0.52])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    assert keyframe_blocks(pid, sid) is True                        # video üretilmez
    assert len(edit_runs(edit)) == 3                                # ilk deneme + 2 yeniden deneme
    seeds = [r["input"]["workflow"]["170:169"]["inputs"]["seed"] for r in edit_runs(edit)]
    assert len(set(seeds)) == 3
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.status == "board_review" and sc.board_status == "changes_requested"
        assert sc.board_note.startswith("Kare inceleme gerekiyor:") and "0,52 < 0,60" in sc.board_note
        assert "(3 deneme)" in sc.board_note and sc.last_error.startswith("Kare inceleme gerekiyor")
        assert sc.board_score == 0.95 and sc.board_key                # kare ve skor denetçide görünür
    assert len(ledger(pid, "h3:storyboard:qwen_edit")) == 3         # her deneme maliyetiyle geçmişte
    with session_scope() as s:
        calls = s.execute(text("SELECT details->>'frame_attempt', model, operation FROM service_calls WHERE project_id = :p "
                               "AND label = 'h3:storyboard:qwen_edit' ORDER BY details->>'frame_attempt'"),
                          {"p": pid}).all()
    assert [tuple(c) for c in calls] == [(f"{i}/3", "Qwen/Qwen-Image-Edit-2511 (ComfyUI edit_keyframe.json, RunPod)", "storyboard") for i in (1, 2, 3)]


def test_a_trial_project_uses_the_uploaded_reference_without_approval(edit, sample_scene, identity, tmp_path):
    """Deneme ön ayarı (lint_strictness warn): varlık taslakta kalsa da yüklenmiş referanstan Qwen-Image-Edit karesi."""
    from app.engine.iteration import keyframe_blocks
    from app.models import Project
    identity([0.8])
    pid, codes = sample_scene()
    with session_scope() as s:
        p = s.get(Project, pid)
        p.settings = {**(p.settings or {}), "lint_strictness": "warn"}
    sid = character_shot(pid, "S002_SH01", tmp_path, lock=False)
    assert keyframe_blocks(pid, sid) is False and len(edit_runs(edit)) == 1


def test_a_retry_that_passes_frees_the_shot(edit, sample_scene, identity, tmp_path):
    from app.engine.iteration import keyframe_blocks
    identity([0.47, 0.66])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    assert keyframe_blocks(pid, sid) is False and len(edit_runs(edit)) == 2
    with session_scope() as s:
        assert s.get(Scene, sid).board_status == "pending"


def test_a_characterless_shot_uses_plain_qwen_and_the_warning_only_follows_a_text_frame(edit, sample_scene, identity,
                                                                                       tmp_path, monkeypatch, api):
    from app.engine import storyboard
    identity([0.9, 0.9])
    pid, codes = sample_scene()
    storyboard.make_board(pid, codes["S001_SH01"])                  # örnek senaryoda referanssız karakter: metinden
    assert len(qwen_runs(edit)) == 1 and edit_runs(edit) == []
    sid = character_shot(pid, "S002_SH01", tmp_path)
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen")    # Ayarlar: karakterli kare de metinden
    storyboard.make_board(pid, sid)
    assert len(qwen_runs(edit)) == 2
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S002_SH01"]["h3_mode_warning"] == "Storyboard karesi karakter referansı kullanmıyor; kimlik kayabilir."
    assert shots["S002_SH01"]["h3_mode"] == "fl2va" and shots["S002_SH01"]["keyframe_model"] == "qwen"
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen_edit")
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S002_SH01"]["keyframe_model"] == "qwen_edit" and len(shots["S002_SH01"]["edit_refs"]) == 2
    assert shots["S002_SH01"]["h3_mode_warning"] is None            # metinden kare videodan önce referanstan yeniden çizilir


def test_a_text_frame_is_redrawn_from_the_reference_before_the_video(edit, sample_scene, identity, tmp_path, monkeypatch):
    from app.engine import storyboard
    from app.engine.iteration import keyframe_blocks
    identity([0.9, 0.8])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen")
    storyboard.make_board(pid, sid)
    monkeypatch.setattr(settings, "keyframe_character_provider", "qwen_edit")
    assert keyframe_blocks(pid, sid) is False
    assert len(qwen_runs(edit)) == 1 and len(edit_runs(edit)) == 1
    with session_scope() as s:
        assert s.get(Scene, sid).board_source == "qwen_edit"


@pytest.mark.parametrize("mode,lightning,steps", [("full", False, 40), ("medium", False, 20), ("lightning", True, 4)])
def test_the_edit_quality_is_a_setting_and_a_project_choice(edit, sample_scene, identity, tmp_path, monkeypatch,
                                                            mode, lightning, steps):
    """Tam (40 adım, cfg 4) / Orta (20 adım, cfg 4) / Lightning (LoRA anahtarı: 4 adım, cfg 1). Proje ayarı önce gelir."""
    from app.engine import storyboard
    from app.models import Project
    identity([0.9])
    monkeypatch.setattr(settings, "qwen_edit_mode", "full")
    pid, codes = sample_scene()
    with session_scope() as s:
        p = s.get(Project, pid)
        p.settings = {**(p.settings or {}), "qwen_edit_mode": mode}
    storyboard.make_board(pid, character_shot(pid, "S002_SH01", tmp_path))
    wf = edit_runs(edit)[0]["input"]["workflow"]
    assert (wf["170:168"]["inputs"]["value"], wf["170:166"]["inputs"]["value"]) == (lightning, steps)
    with session_scope() as s:
        mode_noted = s.execute(text("SELECT details->>'edit_mode' FROM service_calls WHERE project_id = :p AND "
                                    "label = 'h3:storyboard:qwen_edit'"), {"p": pid}).scalar()
    assert mode_noted == mode


def test_a_seedance_shot_starts_from_the_same_frame(edit, sample_scene, identity, tmp_path):
    from app.engine import storyboard
    from app.engine.iteration import keyframe_blocks, keyframe_of
    identity([0.9])
    pid, codes = sample_scene()
    sid = character_shot(pid, "S002_SH01", tmp_path)
    with session_scope() as s:
        s.get(Scene, sid).engine = "seedance"
    assert keyframe_blocks(pid, sid) is False and len(edit_runs(edit)) == 1
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert keyframe_of(sc, sc.board_hash) == storyboard.clean_key(sc.board_key)
        sc.board_source = "local"                                   # yerel kare: Seedance kendi Flux karesini üretir
        assert keyframe_of(sc, sc.board_hash) is None


# ---------- kuyruk ----------

def test_qwen_and_qwen_edit_frames_are_separate_queue_groups():
    from app.core.rerender import ALL
    from app.worker.dispatch import GROUPS, Batch
    assert GROUPS == ("board_qwen", "board_edit", "fl2va", "ref2va")
    b = Batch("p")
    b.add("a", ALL, model="fl2va", board="qwen_edit")
    b.add("b", ALL, model="fl2va", board="qwen")
    b.add("c", ALL, model="ref2va", board="qwen")
    _, groups = b.plan()
    assert {g: sorted(v) for g, v in groups.items() if v} == {
        "board_qwen": ["b", "c"], "board_edit": ["a"], "fl2va": ["a", "b"], "ref2va": ["c"]}


# ---------- fiyat ve tahmini maliyet ----------

@pytest.mark.parametrize("mode,seconds", [("full", 195), ("medium", 100), ("lightning", 15)])
def test_the_qwen_edit_frame_is_priced_from_its_own_row(mode, seconds):
    rp = pricing.serverless()
    rate = pricing.runpod_rate()
    assert pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode=mode) == round(rate * seconds, 4)
    cold = seconds + rp["keyframe_cold_seconds"] - rp["keyframe_seconds"]
    assert pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode=mode, cold=True) == round(rate * cold, 4)
    assert pricing.estimate_keyframe("h3") == round(rate * rp["keyframe_seconds"], 4)


def test_the_reader_accepts_the_old_pricing_layout():
    """Kod ve pricing.yaml birlikte devreye girmezse de okunur: düz render_seconds, keyframe_edit_seconds yok."""
    old = pricing.table()
    old = {**old, "serverless": {"runpod": {**{k: v for k, v in old["serverless"]["runpod"].items()
                                               if not k.startswith("keyframe_edit") and k != "pools"},
                                           "render_seconds": {"480p": 8, "720p": 16.5}}}}
    pricing.validate_table(json.dumps(old))
    assert pricing.estimate_video("h3", "480p", 4, old, mode="ref2va") == pricing.estimate_runpod(32, None, old)
    assert pricing.estimate_keyframe("h3", old, model="qwen_edit") == pricing.estimate_keyframe("h3", old)


def test_the_economy_preset_and_the_estimate_per_shot_and_total(api):
    meta = api.get("/api/meta").json()
    assert meta["presets"]["economy"] == {"probe_n": 3, "probe_resolution": "480p", "full_n": 3, "full_resolution": "720p",
                                          "wave_size": 3, "refined_k": 1, "max_correction_rounds": 1}
    assert meta["defaults"]["probe_n"] == 10 and meta["defaults"]["full_n"] == 50   # varsayılanlar değişmedi
    script = meta["trial_script"]
    r = api.post("/api/estimate", json={"text": script, "preset": "economy"}).json()
    shot = r["shots"][0]
    probe = pricing.estimate_video("h3", "480p", 4, mode="fl2va")
    full = pricing.estimate_video("h3", "720p", 4, mode="fl2va")
    frame = pricing.estimate_keyframe("h3", model="qwen_edit")
    assert (shot["billed_seconds"], shot["frame"], shot["clips"]) == (4, "qwen_edit", 6)
    assert shot["expected_usd"] == pytest.approx(frame + 3 * probe + 3 * full, abs=1e-4)
    assert shot["max_usd"] == pytest.approx(3 * frame + 6 * probe + 3 * full, abs=1e-4)
    loads = r["model_loads"] * pricing.estimate_runpod(pricing.serverless()["cold_start_seconds"])
    assert r["model_loads"] == 2 and r["cold_start_usd"] == pytest.approx(loads, abs=1e-4)   # Qwen-Image-Edit + FL2VA
    assert r["expected_usd"] == pytest.approx(sum(s["expected_usd"] for s in r["shots"]) + loads, abs=1e-3)
    brief = api.post("/api/estimate", json={"text": script, "preset": "brief"}).json()
    assert brief["shots"][0]["clips"] == 60 and brief["expected_usd"] > r["expected_usd"]
    assert api.post("/api/estimate", json={"text": script, "preset": "nope"}).status_code == 422
    assert api.post("/api/estimate", json={}).status_code == 422


def test_the_estimate_of_a_project_follows_its_shots(api, sample_scene):
    from app.models import Scene as S
    pid, codes = sample_scene()
    with session_scope() as s:
        s.get(S, codes["S001_SH02"]).h3_mode = "ref2va"
    r = api.post("/api/estimate", json={"project_id": pid}).json()
    shots = {x["code"]: x for x in r["shots"]}
    assert set(shots) == set(codes)
    assert shots["S001_SH02"]["mode"] == "ref2va" and shots["S001_SH02"]["frame"] is None and shots["S001_SH02"]["frame_usd"] == 0
    assert shots["S001_SH01"]["mode"] == "fl2va" and shots["S001_SH01"]["frame_usd"] > 0
    cheaper = api.post("/api/estimate", json={"project_id": pid, "settings": {"full_n": 1}}).json()
    assert cheaper["expected_usd"] < r["expected_usd"]
