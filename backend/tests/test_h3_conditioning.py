"""MiniMax H3 koşullandırma: kompozisyon seed'le değil koşulla kontrol edilir (storyboard karesi / yüklenen ilk kare ->
FL2VA; karakter referans seti -> Ref2VA), negatifler pozitif kısıt olur, gerçek H3 çıktılarından türeyen kararlar eğitim
verisine girmez. Sahte fal, sahte H3 sunucusu ve sahte ComfyUI ile; gerçek veritabanı, nesne deposu ve FFmpeg ile.
"""
import json
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.core import licensing
from app.db import session_scope
from app.engine import storyboard
from app.engine.iteration import reference_set, run_tiered_generation
from app.live import fal_video, vast_h3
from app.models import CurationEvent, Scene, Variation
from app.style.constraints import constrained_prompt, positive_constraints
from tests.h3_fake import FakeComfy
from tests.media_utils import make_video
from tests.test_live_production import commits, fake_scorer, ledger, studio, variations  # noqa: F401 (fikstür)

SHORT = dict(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0)


def scene(sid) -> Scene:
    with session_scope() as s:
        sc = s.get(Scene, sid)
        s.expunge(sc)
        return sc


# ---------- negatifler -> pozitif kısıtlar ----------

def test_negatives_become_positive_constraints_without_naming_the_unwanted_thing():
    c = positive_constraints("blurry, low quality, watermark, deformed hands, flickering, oversaturated")
    assert c.phrases == ("sharp focus with crisp, clean contours", "pristine, high-detail rendering",
                         "a clean, unmarked image area",
                         "anatomically correct bodies with natural proportions and well-formed hands",
                         "temporally stable lighting and smooth, steady motion", "balanced, natural color saturation")
    assert c.unmapped == () and not any(w in " ".join(c.phrases) for w in ("blur", "watermark", "deform", "flicker"))


def test_the_cast_negative_folds_into_one_constraint_and_unknown_terms_are_reported():
    from app.hermes.lint import CAST_NEGATIVE
    c = positive_constraints(f"{CAST_NEGATIVE}, lens flare, Lens Flare, chromatic aberration")
    assert c.phrases == ("only the listed characters are present and the surroundings are otherwise unpopulated",)
    assert c.unmapped == ("lens flare", "chromatic aberration")


def test_matching_is_by_word_start_so_texture_is_not_text():
    assert positive_constraints("texture seams").unmapped == ("texture seams",)
    assert positive_constraints("text overlay").phrases == ("a clean, unmarked image area",)


def test_turkish_negatives_are_understood_too():
    assert positive_constraints("bulanık, kalabalık, filigran").phrases == (
        "only the listed characters are present and the surroundings are otherwise unpopulated",
        "sharp focus with crisp, clean contours", "a clean, unmarked image area")


def test_no_negative_leaves_the_prompt_untouched():
    assert constrained_prompt("A shot.", "")[0] == "A shot."


# ---------- karakter referans seti ----------

def char(name, keys):
    return SimpleNamespace(name=name, reference_images=keys)


def test_the_reference_set_takes_still_expressions_and_three_turnaround_frames():
    frames = [f"assets/k/frames/x_{i:02d}.jpg" for i in range(8)]
    refs = reference_set([char("Kerem", frames + ["assets/k/still/s.png", "assets/k/expressions/e.png"])])
    assert [r["key"] for r in refs] == ["assets/k/still/s.png", "assets/k/expressions/e.png",
                                        "assets/k/frames/x_00.jpg", "assets/k/frames/x_02.jpg", "assets/k/frames/x_05.jpg"]


def test_the_nine_slots_are_shared_between_characters():
    full = lambda n: char(n, [f"assets/{n}/frames/f_{i:02d}.jpg" for i in range(8)] + [f"assets/{n}/still/s.png",  # noqa: E731
                                                                                        f"assets/{n}/expressions/e.png"])
    refs = reference_set([full("A"), full("B"), char("C", [])])
    assert len(refs) == 9 and [r["name"] for r in refs] == ["A"] * 5 + ["B"] * 4


# ---------- FL2VA ilk kare akışı ----------

def test_paid_fl2va_without_a_frame_source_stops_and_explains(sample_scene, h3, monkeypatch):
    monkeypatch.setattr(settings, "fal_key", "")                          # ne fal.ai ne ComfyUI keyframe.json
    pid, codes = sample_scene(**SHORT)
    sid = codes["S001_SH01"]
    fake_scorer(monkeypatch)
    assert run_tiered_generation(pid, sid) == "board_review"
    sc = scene(sid)
    assert sc.status == "board_review" and "FL2VA ilk kare ister" in sc.last_error and sc.board_source == "local"
    assert h3.submits == [] and variations(sid) == [] and [p for p, k, _ in ledger(pid) if k == "commit"] == []


def test_the_scored_storyboard_frame_is_the_first_frame_of_every_variation(sample_scene, studio, h3, monkeypatch):
    pid, codes = sample_scene(**SHORT)
    sid = codes["S001_SH01"]
    fake_scorer(monkeypatch)
    assert run_tiered_generation(pid, sid) == "refined"
    assert [app for app, _, _ in studio.calls] == [fal_video.FLUX]          # tek kare, kalite kapısını geçti
    sc = scene(sid)
    assert sc.board_source == "flux" and sc.board_score == pytest.approx(0.95)
    assert storage.exists(storyboard.clean_key(sc.board_key))
    frames = {json.dumps([c for c in j["conditions"] if c["role"] == "keyframe"]) for j in h3.submits}
    assert len(h3.submits) == 4 and len(frames) == 1 and {j["task"] for j in h3.submits} == {"fl2va"}
    assert {j["target"]["short_edge"] for j in h3.submits} == {480, 704}     # test turu ve tam tur aynı kareden
    assert all(v.params["keyframe_key"] == storyboard.clean_key(sc.board_key) for v in variations(sid))


def test_a_frame_that_fails_the_gate_is_drawn_once_then_blocks(sample_scene, studio, h3, monkeypatch):
    pid, codes = sample_scene(**SHORT)
    sid = codes["S001_SH01"]
    fake_scorer(monkeypatch)
    studio.black_frames = 99                                               # her kare siyah: kalite kapısı geçmez
    assert run_tiered_generation(pid, sid) == "board_review"
    sc = scene(sid)
    assert len(studio.frames) == storyboard.BOARD_ATTEMPTS == 1          # bir çekim x bir çizim: en çok 1 ücretli kare
    assert sc.board_status == "changes_requested" and sc.board_note.startswith("Kalite kapısı")
    assert "kalite kapısını geçemedi" in sc.last_error and h3.submits == []


def test_an_uploaded_first_frame_replaces_the_storyboard_frame(sample_scene, studio, h3, give_keyframe, monkeypatch):
    pid, codes = sample_scene(**SHORT)
    sid = codes["S001_SH01"]
    key = give_keyframe(sid)
    fake_scorer(monkeypatch)
    assert run_tiered_generation(pid, sid) == "refined"
    assert studio.calls == [] and all(v.params["keyframe_key"] == key for v in variations(sid))
    assert {j["task"] for j in h3.submits} == {"fl2va"}


def test_a_storyboard_from_an_uploaded_frame_costs_nothing(sample_scene, studio, h3, give_keyframe):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    key = give_keyframe(sid)
    board = storyboard.make_board(pid, sid, standalone=True)
    sc = scene(sid)
    assert sc.board_source == "upload" and storage.exists(board) and studio.calls == [] and ledger(pid) == []
    assert storage.read_bytes(storyboard.clean_key(board)) == storage.read_bytes(key)


def test_ref2va_runs_without_a_frame_and_seedance_ignores_the_h3_flow(sample_scene, studio, h3, monkeypatch):
    pid, codes = sample_scene(**SHORT)
    fake_scorer(monkeypatch)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH01"]).h3_mode = "ref2va"
        s.get(Scene, codes["S001_SH02"]).engine = "seedance"
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    monkeypatch.setattr("app.engine.iteration.reference_set", lambda chars: [{"name": "Kerem", "key": "x/still/k.png"}])
    monkeypatch.setattr(vast_h3, "reference_uri", lambda key: "data:image/jpeg;base64,eA==")
    assert run_tiered_generation(pid, codes["S001_SH01"]) == "refined"
    assert {j["task"] for j in h3.submits} == {"ref2va"}
    assert run_tiered_generation(pid, codes["S001_SH02"]) == "refined"     # fal yolu: kendi Flux karesi, H3 yok
    assert len(h3.submits) == 4 and fal_video.SEEDANCE in [app for app, _, _ in studio.calls]


def test_the_storyboard_frame_can_be_drawn_on_the_h3_box_with_comfyui(sample_scene, h3, monkeypatch, tmp_path):
    video = make_video(tmp_path / "c.mp4", seconds=4.0, size="320x180", fps=24)
    pixels = np.random.default_rng(1).integers(0, 255, (180, 320, 3), dtype=np.uint8)
    still = tmp_path / "kf.png"
    Image.fromarray(pixels).save(still)
    comfy = FakeComfy(open(video, "rb").read(), still.read_bytes())
    comfy.output_ext = ".png"
    monkeypatch.setattr(vast_h3, "TRANSPORT", comfy.transport())
    monkeypatch.setattr(settings, "h3_backend", "comfyui")
    flows = tmp_path / "comfy"
    flows.mkdir(exist_ok=True)
    (flows / "keyframe.json").write_text(json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
                                                     "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}"}}}))
    pid, codes = sample_scene()
    fake_scorer(monkeypatch)
    storyboard.make_board(pid, codes["S001_SH01"], standalone=True)
    sc = scene(codes["S001_SH01"])
    (sent,) = comfy.prompts
    assert sc.board_source == "comfyui" and sent["6"]["inputs"]["text"] and isinstance(sent["3"]["inputs"]["seed"], int)
    assert commits(pid, "h3:storyboard") == [pytest.approx(0.95 / 3600 * 30, abs=0.0001)]


# ---------- panel ve API ----------

def test_the_keyframe_can_be_uploaded_and_cleared_and_changes_the_fingerprint(api, api_project, tmp_path):
    from app.hermes import rules
    pid = api_project()
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    sid = next(s["id"] for s in api.get(f"/api/projects/{pid}/overview").json()["scenes"] if s["scene_code"] == "S001_SH01")
    black = tmp_path / "black.png"
    Image.new("RGB", (320, 180)).save(black)
    r = api.post(f"/api/scenes/{sid}/keyframe?filename=black.png", content=black.read_bytes())
    assert r.status_code == 422 and "kalite kontrolünden" in r.json()["detail"]
    good = tmp_path / "good.png"
    Image.fromarray(np.random.default_rng(2).integers(0, 255, (180, 320, 3), dtype=np.uint8)).save(good)
    r = api.post(f"/api/scenes/{sid}/keyframe?filename=good.png", content=good.read_bytes())
    assert r.status_code == 200 and "yeniden üret" in r.json()["hint"]
    ov = next(s for s in api.get(f"/api/projects/{pid}/overview").json()["scenes"] if s["id"] == sid)
    assert ov["keyframe_source"] == "upload" and ov["keyframe_url"].startswith("/api/media?key=keyframes/")
    assert ov["h3_mode"] == "fl2va" and ov["h3_refs"] == []
    with session_scope() as s:
        assert s.get(Scene, sid).keyframe_hash
    assert api.patch(f"/api/scenes/{sid}", json={"h3_mode": "ref2va"}).status_code == 200
    assert api.patch(f"/api/scenes/{sid}", json={"h3_mode": "t2va"}).status_code == 422
    assert api.delete(f"/api/scenes/{sid}/keyframe").status_code == 200
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert (sc.keyframe_key, sc.keyframe_hash, sc.h3_mode) == (None, None, "ref2va")
        actions = set(s.scalars(select(CurationEvent.action).where(CurationEvent.scene_id == sid)))
    assert {"keyframe_upload", "keyframe_clear", "scene_edit"} <= actions


def test_only_a_ref2va_choice_or_an_uploaded_frame_change_the_fingerprint():
    from app.core.hashing import visual_fingerprint
    base = dict(prompt_spec={"prompt": "p"}, camera={}, engine="h3", duration_s=5.0, method="generate")
    plain = visual_fingerprint(SimpleNamespace(**base), [])
    assert visual_fingerprint(SimpleNamespace(**base, h3_mode="fl2va"), []) == plain
    assert visual_fingerprint(SimpleNamespace(**base, h3_mode="ref2va"), []) != plain
    assert visual_fingerprint(SimpleNamespace(**base, keyframe_hash="abc"), []) != plain


# ---------- lisans: eğitim verisi ve paket bildirimi ----------

def h3_clip(sid, real=True) -> str:
    vid = uuid.uuid4()
    prov = {"model": "MiniMaxAI/MiniMax-H3", "model_revision": "turbo v4", "task": "fl2va", "backend": "sglang",
            "server_location": "Norway, NO"}
    with session_scope() as s:
        s.add(Variation(id=vid, scene_id=sid, tier="full", engine="h3", seed=1, prompt_revision=0, status="passed",
                        refined_rank=1, external_request_id="video_9" if real else f"local-{vid}",
                        params={"provider_meta": {"provenance": prov}}))
    return str(vid)


def test_training_exports_leave_out_decisions_on_real_h3_clips(api, api_project):
    from app.hermes import rules
    pid = api_project()
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    scenes = {s["scene_code"]: s["id"] for s in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    real, local = h3_clip(scenes["S001_SH01"]), h3_clip(scenes["S001_SH02"], real=False)
    with session_scope() as s:
        for sid, vid, action in ((scenes["S001_SH01"], real, "promote"), (scenes["S001_SH01"], None, "upgrade_request"),
                                 (scenes["S001_SH02"], local, "promote"), (scenes["S002_SH01"], None, "board_approve")):
            s.add(CurationEvent(project_id=pid, scene_id=sid, variation_id=vid, action=action, payload={}))
    r = api.get(f"/api/projects/{pid}/curation.jsonl")
    lines = [json.loads(x) for x in r.text.splitlines()]
    kept = {(x["scene_code"], x["action"]) for x in lines}
    assert ("S001_SH01", "promote") not in kept and ("S001_SH01", "upgrade_request") not in kept
    assert {("S001_SH02", "promote"), ("S002_SH01", "board_approve")} <= kept     # yerel çizim H3 çıktısı değildir
    assert r.headers["X-Hellgrind-H3-Records"] == "2"
    audit = [json.loads(x) for x in api.get(f"/api/projects/{pid}/curation.jsonl?include_h3=true").text.splitlines()]
    flagged = [x for x in audit if x.get("training_allowed") is False]
    assert {(x["scene_code"], x["action"]) for x in flagged} == {("S001_SH01", "promote"), ("S001_SH01", "upgrade_request")}
    recent = api.get(f"/api/projects/{pid}/curation?limit=50").json()
    assert {x["action"] for x in recent} >= {"promote", "upgrade_request"}         # panel her kararı görür
    with session_scope() as s:
        notice = licensing.notice(licensing.restricted(s, pid))
    assert "S001_SH01: MiniMaxAI/MiniMax-H3 turbo v4 · fl2va · arka uç sglang · sunucu konumu Norway, NO" in notice
    assert "S001_SH02" not in notice and "§V.3" in notice


def test_no_h3_output_means_no_notice(api, api_project):
    pid = api_project()
    with session_scope() as s:
        assert licensing.notice(licensing.restricted(s, pid)) is None
