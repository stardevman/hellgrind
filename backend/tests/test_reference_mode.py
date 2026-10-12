"""Referans + senaryo ile video (VIDEO_INPUT_MODE=reference, SCENE_RENDER_MODE=whole_scene): hiçbir kare ya da görsel
üretilmez; sahnenin çekimleri tek istekte. Ücretli yol sahte kuyruk istemcisiyle ve `no_network` altında; gövdeler fal
OpenAPI tanımıyla doğrulanır. Sahne, Nora — En Küçüğü'nün üç çekiminin kısaltılmış hâlidir (3 + 3 + 4 sn).
"""
import json
import subprocess
import sys
import uuid

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.db import session_scope
from app.engine import fal_plan, scene_parts
from app.models import Asset, Character, Scene, SceneAsset, SceneCharacter, Variation
from tests.fal_queue_fake import FakeQueue, validate

NORA = [("S001_SH01", 3.0, ["Nora"], ["Hens", "Farmyard Behind The Old Red Barn"], "new_scene",
         "Nora scatters a wide golden arc of grain; a flock of plump brown hens rushes in from the right."),
        ("S001_SH02", 3.0, ["Pip"], ["Hens", "Farmyard Behind The Old Red Barn"], "cut",
         "Pip, a tiny fluffy yellow chick, stands alone at the edge of the feeding frenzy and peeps."),
        ("S001_SH03", 4.0, ["Nora", "Pip"], ["Farmyard Behind The Old Red Barn"], "cut",
         "Nora kneels and holds out her open palm; Pip hops onto it.")]
ASSETS = {"Nora": ("character", "a slim teenage girl with long straight copper-red hair, big blue eyes and freckles", None),
          "Pip": ("character", "a tiny round fluffy baby chick with bright lemon-yellow down", "tiny, palm-sized; much smaller than a hen"),
          "Hens": ("prop", "a small flock of plump brown farm hens", None),
          "Farmyard Behind The Old Red Barn": ("location", "a rustic farmyard behind an old weathered red barn", None)}


@pytest.fixture
def ref_defaults(monkeypatch):
    """Uygulamanın gerçek varsayılanları (conftest eski testler için kare yolunu ve H3/RunPod'u sabitler)."""
    for name in ("h3_platform", "keyframe_platform", "video_draft_engine", "video_final_engine", "video_input_mode",
                 "scene_render_mode"):
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)


def _still(key: str) -> str:
    path = f"/tmp/{uuid.uuid4().hex}.png"
    Image.fromarray(np.random.default_rng(len(key)).integers(0, 255, (64, 64, 3)).astype(np.uint8)).save(path, "PNG")
    storage.put_file(path, key, "image/png")
    return key


def nora(mk_project, shots=NORA, with_stills=True, extra_props=0, **cfg) -> tuple[str, dict]:
    """Nora sahnesi: varlıklar (referans görselleriyle), karakterler ve çekimler. (proje, kod -> çekim kimliği)."""
    pid = mk_project(500.0, **{"lint_strictness": "off", **cfg})
    ids = {}
    with session_scope() as s:
        assets = {}
        names = dict(ASSETS)
        for i in range(extra_props):
            names[f"Crate {i}"] = ("prop", f"wooden crate number {i}", None)
        for name, (type_, desc, scale) in names.items():
            a = Asset(project_id=pid, type=type_, name=name, description=desc, render_style="clean_3d",
                      status="locked", version=1, scale=scale,
                      references={"still": {"key": _still(f"assets/{pid}/{name}/still/a.png")}}
                      if with_stills or type_ != "prop" else {})
            s.add(a)
            s.flush()
            assets[name] = a
        chars = {}
        for name in ("Nora", "Pip"):
            ch = Character(project_id=pid, name=name, description=ASSETS[name][1], asset_id=assets[name].id,
                           reference_images=[assets[name].references["still"]["key"]],
                           kind="human" if name == "Nora" else "non_human", pinecone_namespace=f"{pid}:{name}")
            s.add(ch)
            s.flush()
            chars[name] = ch
        for order, (code, duration, cast, props, transition, prompt) in enumerate(shots, start=1):
            sc = Scene(project_id=pid, scene_code=code, seq_order=order, duration_s=duration, engine="h3max",
                       prompt_spec={"prompt": prompt, "lang": "en"}, camera={"shot": "medium", "movement": "static"},
                       status="pending", transition_auto=transition, emotion="neutral", method="generate")
            s.add(sc)
            s.flush()
            for n in cast:
                s.add(SceneCharacter(scene_id=sc.id, character_id=chars[n].id, role="primary", dialogue=[]))
            for n in props + ([f"Crate {i}" for i in range(extra_props)] if order == 1 else []):
                s.add(SceneAsset(scene_id=sc.id, asset_id=assets[n].id, role=assets[n].type, source="manual"))
            ids[code] = str(sc.id)
    return pid, ids


def plan_for(sid, engine=None, **kw):
    with session_scope() as s:
        p = scene_parts.plan_part(s, s.get(Scene, sid), engine, **kw)
        return p.as_dict() | {"_shots": [x.scene_code for x in p.shots]}


# ---------- tüm sahne istemi ----------

def test_noras_three_shots_are_one_h3max_request_of_ten_seconds(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH02"])
    assert plan["part"] == ["S001_SH01", "S001_SH02", "S001_SH03"] and plan["duration_s"] == 10.0
    assert plan["endpoint"] == "minimax/h3-max/reference-to-video" and plan["body"]["duration"] == 10.0
    body = plan["body"]
    assert validate(plan["endpoint"], body) == [] and len(body["reference_image_urls"]) == 4
    assert [(r["token"], r["name"]) for r in plan["references"]] == [
        ("Image 1", "Nora"), ("Image 2", "Pip"), ("Image 3", "Hens"), ("Image 4", "Farmyard Behind The Old Red Barn")]
    prompt = body["prompt"]
    for label in ("Shot 1 (0-3s), medium shot", "Shot 2 (3-6s)", "Shot 3 (6-10s)"):
        assert label in prompt
    assert prompt.index("Shot 1") < prompt.index("Shot 2") < prompt.index("Shot 3")
    assert "Nora is Image 1: a slim teenage girl" in prompt and "The prop Hens is Image 3:" in prompt
    assert "Pip is tiny, palm-sized; much smaller than a hen." in prompt               # gerçek boyut
    assert ("Use Image 4 only for the look of the place (architecture, materials, colors, light); do not copy its camera "
            "position or viewpoint — stage the action as described in each shot.") in prompt
    assert "The cast of this scene is exactly Nora, Pip and the Hens; no other people, animals or figures appear." in prompt and "@Image" not in prompt
    assert "Mood:" not in prompt and "Visual constraints" not in prompt                  # sade kurallar istemi
    assert body["resolution"] == "480P"                                                  # en ucuz varsayılan
    assert plan["cost"]["usd"] == pytest.approx(10 * 0.05 * 1.05, abs=1e-3)           # 4 görsel: token dahil
    assert plan["continuity"]["transition"] == "new_scene" and "image_url" not in body


def test_the_same_scene_is_one_seedance_request_with_at_tokens(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH01"], "seedance25")
    body = plan["body"]
    assert plan["endpoint"] == "bytedance/seedance-2.5/reference-to-video" and body["duration"] == "10"
    assert [r["token"] for r in plan["references"]] == ["@Image1", "@Image2", "@Image3", "@Image4"]
    assert "Nora is @Image1:" in body["prompt"] and "Image 1" not in body["prompt"] and validate(plan["endpoint"], body) == []


def test_a_scene_over_15_seconds_is_split_at_shot_boundaries_for_h3max_only(ref_defaults, mk_project, s3):
    long = [(c, 6.0, cast, props, tr, p) for c, _, cast, props, tr, p in NORA]
    pid, ids = nora(mk_project, shots=long)
    assert plan_for(ids["S001_SH01"])["part"] == ["S001_SH01", "S001_SH02"]
    assert plan_for(ids["S001_SH03"])["part"] == ["S001_SH03"]
    assert plan_for(ids["S001_SH01"])["body"]["duration"] == 12.0
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).engine = "seedance25"
    whole = plan_for(ids["S001_SH03"])
    assert whole["part"] == ["S001_SH01", "S001_SH02", "S001_SH03"] and whole["body"]["duration"] == "18"


def test_per_shot_mode_makes_one_request_per_shot(ref_defaults, mk_project, s3, monkeypatch):
    monkeypatch.setattr(settings, "scene_render_mode", "per_shot")
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH02"])
    assert plan["part"] == ["S001_SH02"] and plan["body"]["duration"] == 3.0
    assert [r["name"] for r in plan["references"]] == ["Pip", "Hens", "Farmyard Behind The Old Red Barn"]


# ---------- süreklilik: önceki klibin son karesi ----------

def _winner(sid, seconds=3.0):
    """Çekime gerçek bir kazanan klip (son karesi çıkarılabilsin)."""
    from tests.media_utils import make_video
    vid = uuid.uuid4()
    path = make_video(f"/tmp/{vid}.mp4", seconds=seconds)
    storage.put_file(path, f"variations/{sid}/{vid}.mp4")
    with session_scope() as s:
        s.add(Variation(id=vid, scene_id=sid, tier="probe", wave_no=0, engine="h3max", seed=1, prompt_revision=0,
                        status="refined", refined_rank=1, video_key=f"variations/{sid}/{vid}.mp4", params={}))
    return str(vid)


def test_a_continuous_part_gets_the_previous_last_frame_in_the_right_slot(ref_defaults, mk_project, s3):
    long = [(c, 6.0, cast, props, "continuous" if c == "S001_SH03" else tr, p) for c, _, cast, props, tr, p in NORA]
    pid, ids = nora(mk_project, shots=long)
    winner = _winner(ids["S001_SH02"])
    h3 = plan_for(ids["S001_SH03"])
    assert h3["continuity"]["expected"] and h3["continuity"]["frame_key"] == f"seam/{winner}.jpg"
    assert h3["body"]["image_url"] == f"local:seam/{winner}.jpg"                      # H3 Max: ilk kare alanı
    assert h3["body"]["reference_image_urls"][0].startswith("local:assets/")          # referanslar yerinde
    assert [r["token"] for r in h3["references"]][:2] == ["Image 1", "Image 2"]
    assert validate(h3["endpoint"], h3["body"]) == []
    with session_scope() as s:
        s.get(Scene, ids["S001_SH03"]).engine = "seedance25"
    sd = plan_for(ids["S001_SH03"])
    assert sd["body"]["image_urls"][0] == f"local:seam/{winner}.jpg"                  # Seedance: başa konur
    assert sd["references"][0]["kind"] == "frame" and sd["references"][0]["token"] == "@Image1"
    assert [(r["token"], r["name"]) for r in sd["references"][1:]] == [            # numaralar bir kayar
        ("@Image2", "Nora"), ("@Image3", "Pip"), ("@Image4", "Farmyard Behind The Old Red Barn")]
    assert "starts exactly from @Image1" in sd["body"]["prompt"] and "Nora is @Image2: a slim" in sd["body"]["prompt"]
    assert "image_url" not in sd["body"]
    assert validate(sd["endpoint"], sd["body"]) == []


def test_a_cut_never_gets_the_previous_frame(ref_defaults, mk_project, s3):
    long = [(c, 6.0, cast, props, tr, p) for c, _, cast, props, tr, p in NORA]   # SH03: cut
    pid, ids = nora(mk_project, shots=long)
    _winner(ids["S001_SH02"])
    plan = plan_for(ids["S001_SH03"])
    assert plan["continuity"]["expected"] is False and "image_url" not in plan["body"]


def test_rendering_a_continuous_part_first_renders_the_previous_part_and_extracts_its_last_frame(
        ref_defaults, mk_project, fast, dispatched):
    from app.engine.iteration import run_tiered_generation
    long = [(c, 6.0, cast, props, "continuous" if c == "S001_SH03" else tr, p) for c, _, cast, props, tr, p in NORA]
    pid, ids = nora(mk_project, shots=long)
    assert run_tiered_generation(pid, ids["S001_SH03"]) == "refined"     # önce SH01+SH02 parçası, sonra SH03
    with session_scope() as s:
        winners = {code: s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1)).one()
                   for code, sid in ids.items()}
        sh03 = winners["S001_SH03"].params
        assert sh03["continuity"]["frame_key"] == f"seam/{winners['S001_SH02'].id}.jpg"
        assert storage.exists(sh03["continuity"]["frame_key"])                       # ffmpeg ile çıkarıldı
        assert winners["S001_SH01"].params["part"] == winners["S001_SH02"].params["part"] != sh03["part"]


# ---------- tek istek, kesim, kapılar (ücretsiz mod) ----------

def _duration(key: str) -> float:
    path = storage.download(key, f"/tmp/{uuid.uuid4().hex}.mp4")
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def test_one_generation_is_cut_into_every_shot_of_the_scene(ref_defaults, mk_project, fast, dispatched):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    for sid in ids.values():                                               # üç görev, tek üretim
        assert run_tiered_generation(pid, sid) == "refined"
    with session_scope() as s:
        commits = s.execute(text("SELECT COUNT(*) FROM cost_ledger WHERE project_id = :p AND kind = 'commit'"),
                            {"p": pid}).scalar()
        rows = {code: s.scalars(select(Variation).where(Variation.scene_id == sid)).all() for code, sid in ids.items()}
        assert all(s.get(Scene, sid).cut_tier == "draft" for sid in ids.values())
    assert commits == 1 and all(len(v) == 1 for v in rows.values())
    parts = {v[0].params["part"] for v in rows.values()}
    assert len(parts) == 1
    for code, expected in (("S001_SH01", 3.0), ("S001_SH02", 3.0), ("S001_SH03", 4.0)):
        v = rows[code][0]
        assert v.refined_rank == 1 and v.params["segment"][1] - v.params["segment"][0] == expected
        assert abs(_duration(v.video_key) - expected) < 0.2
        gates = v.params["score_detail"]["video_gates"]
        assert gates["step"] == 0.5 and gates["passed"] is True                 # klip başına kapı sonucu kayıtlı


def test_reference_mode_makes_zero_image_generation_calls(ref_defaults, paid, s3, mk_project, monkeypatch, tmp_path,
                                                          api, fast):
    from app.engine import frames, storyboard
    from app.engine.iteration import run_tiered_generation
    from app.live import fal_queue, fal_video
    from app.quality import gate
    from app.engine.procedural import render_clip
    fake = FakeQueue()
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(fal_queue, "POLL_S", 0)
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    monkeypatch.setattr(gate, "ensure_scorer", lambda: None)
    fal_queue._local_uploads.clear()
    for key in fal_queue._r().scan_iter("hg:fal:*"):
        fal_queue._r().delete(key)

    def download(url, suffix=".mp4"):
        path = str(tmp_path / f"{uuid.uuid4().hex}{suffix}")
        render_clip(path, 1, 160, 90, 10.5, 8, emotion="neutral", camera={"shot": "wide"}, characters=["Nora"])
        return path
    monkeypatch.setattr(fal_video, "_download", download)
    image_calls = []
    for mod, name in ((frames, "keyframe_image"), (frames, "edit_keyframe_image"), (fal_video, "image")):
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: image_calls.append(_n))
    pid, ids = nora(mk_project)
    assert storyboard.make_board(pid, ids["S001_SH01"]).startswith("skip")              # şerit çizilmez
    # ücretli modda plan onayı varsayılan açık: onaysız render yok
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()
    assert r["awaiting_plan"] is True and r["stages"] == [] and fake.submits == []
    plan = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    assert plan["approval"] == {"required": True, "approved": False, "plan_hash": plan["plan_hash"]}
    assert run_tiered_generation(pid, ids["S001_SH01"]) == "plan_review" and fake.submits == []
    ok = api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve", json={"plan_hash": plan["plan_hash"]})
    assert ok.status_code == 200 and ok.json()["part"] == list(ids)
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    assert fake.endpoints() == ["minimax/h3-max/reference-to-video"]                  # tek video isteği
    assert not any("qwen" in e or "flux" in e or "kontext" in e for e in fake.endpoints()) and image_calls == []
    assert validate(fake.endpoints()[0], fake.submits[0][1]) == []
    assert fake.submits[0][1]["duration"] == 10.0 and len(fake.submits[0][1]["reference_image_urls"]) == 4


def test_image_generation_entry_points_refuse_in_reference_mode(ref_defaults, paid):
    from app.live import PaidCallBlocked, fal_qwen
    with pytest.raises(PaidCallBlocked, match="Referans modunda"):
        fal_qwen.keyframe_image("x", 1)
    with pytest.raises(PaidCallBlocked, match="Referans modunda"):
        fal_qwen.edit_keyframe_image("x", 1, [{"name": "a", "key": "k"}])


# ---------- eksik referans, dosya sınırı ----------

def test_a_missing_reference_blocks_the_render_and_the_plan(ref_defaults, mk_project, s3, api, dispatched):
    pid, ids = nora(mk_project, with_stills=False)          # Hens nesnesinin görseli yok
    plan = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    assert plan["blocked"] and [m["name"] for m in plan["missing"]] == ["Hens"]
    assert any(w.startswith("Eksik referans: Hens") for w in plan["warnings"])
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()
    assert r["stages"] == [] and r["blocked"]["missing"][0]["name"] == "Hens" and dispatched == []
    assert api.post(f"/api/scenes/{ids['S001_SH01']}/plan/approve", json={}).status_code == 409
    with session_scope() as s:
        assert s.get(Scene, ids["S001_SH01"]).status == "blocked"


def test_the_h3max_reference_limit_drops_the_least_important_props_with_a_warning(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project, extra_props=8)              # 2 karakter + 9 nesne + 1 mekân = 12 görsel
    plan = plan_for(ids["S001_SH01"])
    assert len(plan["body"]["reference_image_urls"]) == 9 and len(plan["dropped"]) == 3
    assert {d["kind"] for d in plan["dropped"]} <= {"prop", "location"}
    kept = [r["name"] for r in plan["references"]]
    assert kept[:2] == ["Nora", "Pip"] and "Hens" in kept                       # adı geçen nesne kalır
    assert any(w.startswith("Referans sınırı (9 görsel)") for w in plan["warnings"])
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).engine = "seedance25"
    assert plan_for(ids["S001_SH01"])["dropped"] == []                          # Seedance 30 görsel alır


# ---------- plan onayı ve yükseltme ----------

def test_plan_approval_is_required_when_enabled_and_survives_until_the_plan_changes(ref_defaults, mk_project, s3, api,
                                                                                     dispatched):
    pid, ids = nora(mk_project, require_plan_approval=True)
    r = api.post(f"/api/scenes/{ids['S001_SH02']}/render").json()
    assert r["awaiting_plan"] is True and r["plan"]["part"] == list(ids) and dispatched == []
    with session_scope() as s:
        assert {s.get(Scene, sid).status for sid in ids.values()} == {"plan_review"}
    plan = api.post(f"/api/scenes/{ids['S001_SH02']}/render?dry_run=true").json()
    assert api.post(f"/api/scenes/{ids['S001_SH02']}/plan/approve", json={"plan_hash": "eski"}).status_code == 409
    ok = api.post(f"/api/scenes/{ids['S001_SH02']}/plan/approve?render=true", json={"plan_hash": plan["plan_hash"]})
    assert ok.status_code == 200 and all("VISUAL" in d["stages"] for d in ok.json()["dispatched"].values())
    api.patch(f"/api/scenes/{ids['S001_SH03']}", json={"duration_s": 5})        # plan değişti: yeniden onay
    again = api.get(f"/api/scenes/{ids['S001_SH01']}/fal-plan").json()
    assert again["approval"]["approved"] is False and again["plan_hash"] != plan["plan_hash"]
    with session_scope() as s:
        events = s.execute(text("SELECT action FROM curation_events WHERE project_id = :p"), {"p": pid}).scalars().all()
    assert "plan_approve" in events


def _draft(ids):
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).cut_tier = "draft"
    for sid in ids.values():
        _winner(sid)


def test_upgrade_shows_the_whole_scene_total_and_respects_the_budget(ref_defaults, mk_project, s3, api, dispatched):
    pid, ids = nora(mk_project)
    _draft(ids)
    plan = api.get(f"/api/scenes/{ids['S001_SH02']}/fal-plan").json()
    up = plan["upgrade"]
    assert up["available"] and up["scope"] == "scene" and up["shots"] == list(ids)
    assert up["parts"] == [{"part": list(ids), "duration_s": 10.0, "usd": up["total_usd"],
                            "endpoint": "bytedance/seedance-2.5/reference-to-video", "plan_hash": up["parts"][0]["plan_hash"]}]
    # Seedance taslağı (draft: 480p): 864 × 480 × 10 × 24 / 1024 = 97.200 token × 0,0214 $ + %25
    assert up["total_usd"] == pytest.approx(97200 / 1000 * 0.0214 * 1.25, abs=1e-3) and up["budget_ok"]
    stale = api.post(f"/api/scenes/{ids['S001_SH02']}/upgrade-final?expected_usd=1.0")
    assert stale.status_code == 409 and "Tahmin değişti" in stale.json()["detail"]
    with session_scope() as s:
        s.execute(text("UPDATE projects SET budget_limit_usd = 1 WHERE id = :p"), {"p": pid})
    poor = api.post(f"/api/scenes/{ids['S001_SH02']}/upgrade-final?expected_usd={up['total_usd']}")
    assert poor.status_code == 409 and "Bütçe yetmiyor" in poor.json()["detail"]
    with session_scope() as s:
        assert {s.get(Scene, sid).engine for sid in ids.values()} == {"h3max"}     # hiçbir şey değişmedi
        s.execute(text("UPDATE projects SET budget_limit_usd = 500 WHERE id = :p"), {"p": pid})
    r = api.post(f"/api/scenes/{ids['S001_SH02']}/upgrade-final?expected_usd={up['total_usd']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "seedance25" and body["shots"] == list(ids) and body["total_usd"] == up["total_usd"]
    assert all("VISUAL" in d["stages"] for d in body["dispatched"].values())     # tek adım: plan onaylı sayılır
    with session_scope() as s:
        assert {s.get(Scene, sid).engine for sid in ids.values()} == {"seedance25"}
        assert all(s.get(Scene, sid).plan_approval["engine"] == "seedance25" for sid in ids.values())


def test_h3max_outputs_are_not_license_restricted(mk_project, mk_scene, s3):
    from app.core import licensing
    pid = mk_project()
    sid = mk_scene(pid)
    with session_scope() as s:
        s.add(Variation(scene_id=sid, tier="probe", wave_no=0, engine="h3max", seed=1, prompt_revision=0,
                        status="refined", external_request_id="fal-req-1", params={}))
        s.flush()
        assert licensing.restricted(s, pid).variations == set()


# ---------- video kapıları ----------

def test_video_gates_sample_every_half_second_and_reject_a_wrong_scale(monkeypatch, tmp_path):
    from app.quality import gate, scorer
    from tests.media_utils import make_video
    path = make_video(tmp_path / "clip.mp4", seconds=3.0)
    monkeypatch.setattr(settings, "quality_scorer", "real")
    frames = [(0.25 + 0.5 * i, Image.new("RGB", (32, 18))) for i in range(6)]
    monkeypatch.setattr(scorer, "frames_every", lambda p, step, limit: frames)
    monkeypatch.setattr(scorer, "score_images", lambda images, target: scorer.Result(
        clip=0.95, face=None, composite=0.95, detail={"look": {"Pip": 0.9}, "images": len(images)}))
    monkeypatch.setattr(scorer, "face_counts", lambda p: [(t, 1) for t, _ in frames])
    monkeypatch.setattr(scorer, "size_ratio_image", lambda image, target, name: 2.8)   # dev civciv: 2,8 yüz boyu
    target = scorer.Target(faces=(("Nora", ("k",)),), looks=(("Pip", ("k2",)),))
    score = gate.score_video(path, 1, {"clip_threshold": 0.85, "identity_threshold": 0.6, "look_threshold": 0.7,
                                       "look_dino_threshold": 0.78}, target, video_gates_on=True,
                             scales=[{"name": "Pip", "max_faces": 1.5}])
    gates = score.detail["video_gates"]
    assert gates["step"] == 0.5 and gates["frames"] == 6 and gates["scale"]["Pip"]["ok"] is False
    assert not score.passed and "scale_mismatch" in {a["type"] for a in score.anomalies}
    monkeypatch.setattr(scorer, "size_ratio_image", lambda image, target, name: 1.1)
    ok = gate.score_video(path, 1, {"clip_threshold": 0.85, "identity_threshold": 0.6, "look_threshold": 0.7,
                                    "look_dino_threshold": 0.78}, target, video_gates_on=True,
                          scales=[{"name": "Pip", "max_faces": 1.5}])
    assert ok.detail["video_gates"]["scale"]["Pip"]["ok"] is True
