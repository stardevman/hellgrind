""""Kıvılcım" zincir koşusu (14:02-14:48) bulguları: eşik kalibrasyonu (sınırdakiler "inceleme önerilir" ile geçer),
zincirin ortasında kare değişince bağlı işlerin iptali ve eski kareden kliplerin işaretlenmesi, eski karede videonun
başlamaması, temiz başlangıç. Sahte RunPod (ağ yok)."""
import io
import pathlib

import pytest
from PIL import Image
from sqlalchemy import select

from app.budget import killswitch
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.db import session_scope
from app.live import runpod_h3
from app.models import Scene, Variation
from app.quality import gate, scorer
from tests.test_chain import chained  # noqa: F401
from tests.test_qwen_edit import edit  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

BOLT = scorer.Target(looks=(("Bolt", ("assets/b/still/b.png",)),))
CFG = dict(DEFAULT_PROJECT_SETTINGS)


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, "PNG")
    return buf.getvalue()


# ---------- eşikler (bu koşunun ölçümleriyle) ----------

@pytest.mark.parametrize("shot,clip,dino,passes,review", [
    ("SH06 son kare (gözle doğru)", 0.7146, 0.7999, True, True),
    ("SH04 son kare", 0.7460, 0.8756, True, True),
    ("SH03 kare", 0.8562, 0.9077, True, False),
    ("SH05 kare", 0.8606, 0.8755, True, False),
    ("SH03 video adayı (eskiden elendi)", 0.7729, 0.8154, True, True),
    ("benzemeyen kare", 0.66, 0.74, False, False),
])
def test_the_recalibrated_likeness_floors(shot, clip, dino, passes, review):
    detail = {"look": {"Bolt": clip}, "look_dino": {"Bolt": dino}}
    assert (not gate.identity_anomalies(detail, BOLT, CFG)) is passes, shot
    assert bool(gate.identity_review(detail, BOLT, CFG)) is review, shot


def test_a_borderline_frame_passes_with_a_review_note(monkeypatch):
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    measured = scorer.Result(clip=0.95, face=None, composite=0.95,
                             detail={"faces_seen": 0, "look": {"Bolt": 0.7146}, "look_dino": {"Bolt": 0.7999}})
    score = gate._judge(measured, 1, [], CFG, BOLT, check_identity=True)
    assert score.passed and score.detail["review"] == ["Bolt benzerliği sınırda (OpenCLIP 0,71, eşik 0,70)",
                                                       "Bolt benzerliği sınırda (DINOv2 0,80, eşik 0,78)"]


def test_continuity_floor_for_continuing_shots_is_070():
    assert CFG["continuity_threshold"] == 0.70 and CFG["cut_continuity_threshold"] == 0.35   # kesme: yeni kamera


# ---------- zincirin ortasında değişiklik ----------

def _clip(sid, status="refined"):
    with session_scope() as s:
        v = Variation(scene_id=sid, tier="full", wave_no=0, engine="h3", seed=7, prompt_revision=0, status=status,
                      params={"resolution": "720p"}, video_key="variations/x.mp4", refined_rank=1 if status == "refined" else None)
        s.add(v)
        s.flush()
        return str(v.id)


def test_a_changed_frame_mid_chain_stops_and_marks_what_depends_on_it(chained):  # noqa: F811
    """K3 yeniden çizildi: SH02'nin videosu K3'te biter (SH03 devam ediyor), SH03 kendi karesi; SH04 üretiliyor (durur).
    SH01 etkilenmez: SH02 kesmeyle geldiği için SH01'in videosu kendi son karesinde biter."""
    from app.engine import storyboard
    from app.engine.iteration import CHAIN_STALE
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH04"])               # K1..K4
    c1, c2, c3 = _clip(codes["S001_SH01"]), _clip(codes["S001_SH02"]), _clip(codes["S001_SH03"])
    with session_scope() as s:
        for code in ("S001_SH01", "S001_SH02"):
            s.get(Scene, codes[code]).status = "done"
        s.get(Scene, codes["S001_SH04"]).status = "generating"   # SH04'ün videosu üretiliyor
    runpod_h3.remember("jk-sh04", "job-sh04", pid, "fl2va", codes["S001_SH04"])
    fake.jobs["job-sh04"] = {"id": "job-sh04", "polls": 0, "failed": False, "kind": "video"}
    fake.image = png((30, 160, 90))
    storyboard.make_board(pid, codes["S001_SH03"], standalone=True, explicit=True)   # K3 yeniden çizildi
    with session_scope() as s:
        v1, v2, v3 = (s.get(Variation, x) for x in (c1, c2, c3))
        sh1, sh2 = s.get(Scene, codes["S001_SH01"]), s.get(Scene, codes["S001_SH02"])
        assert all(v.params.get("stale_keyframe") == "S001_SH03 karesi değişti" for v in (v2, v3))
        assert not v1.params.get("stale_keyframe") and sh1.status == "done"             # kesme: SH01 etkilenmez
        assert sh2.status == "needs_review" and sh2.visual_hash is None                  # kurguda kullanılmaz
        assert "eski kareden" in sh2.last_error
    assert killswitch.r().get(CHAIN_STALE.format(codes["S001_SH04"]))                # süren üretim durur
    assert "job-sh04" in fake.cancelled and runpod_h3.pending(pid) == {}             # RunPod işi iptal edildi


def test_a_video_never_starts_from_a_stale_frame(chained):  # noqa: F811
    """SH05 "done" oldu ama karesi eskiydi: zincir durmuşken (önceki kare incelemede) ya da kare eskiyken video yok."""
    from app.engine import storyboard
    from app.engine.iteration import keyframe_blocks
    fake, pid, codes, state = chained
    storyboard.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        k2.board_status, k2.board_note = "changes_requested", "Kare inceleme gerekiyor: x"
    assert keyframe_blocks(pid, codes["S001_SH03"]) is True       # önceki kare incelemede
    with session_scope() as s:
        assert "inceleme bekliyor" in s.get(Scene, codes["S001_SH03"]).last_error


def test_a_running_generation_stops_when_its_frame_goes_stale(chained):  # noqa: F811
    from app.engine.iteration import CHAIN_STALE, ChainStale, Ctx, chain_check
    fake, pid, codes, _ = chained
    ctx = Ctx(project_id=pid, scene_id=codes["S001_SH03"], run_id="r", provider=None, cfg={}, duration=4.0,
              prompt_spec={}, revision=0, visual_hash="h", render={})
    chain_check(ctx)
    killswitch.r().set(CHAIN_STALE.format(codes["S001_SH03"]), "S001_SH02 karesi değişti")
    with pytest.raises(ChainStale, match="S001_SH02"):
        chain_check(ctx)


def test_the_impact_of_a_chain_change_is_shown_before(chained, api):  # noqa: F811
    from app.engine import storyboard
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH04"])
    _clip(codes["S001_SH01"]), _clip(codes["S001_SH02"]), _clip(codes["S001_SH03"])
    r = api.get(f"/api/scenes/{codes['S001_SH03']}/chain-impact").json()
    assert r["frame_codes"] == ["S001_SH03", "S001_SH04"] and r["clip_codes"] == ["S001_SH02", "S001_SH03"]
    assert r["frames"] == 2 and r["clips"] == 2 and r["estimate_usd"] > 0
    r = api.get(f"/api/scenes/{codes['S001_SH02']}/chain-impact").json()       # kesme: SH01'in klibi etkilenmez
    assert r["frame_codes"] == ["S001_SH02", "S001_SH03", "S001_SH04"] and "S001_SH01" not in r["clip_codes"]


# ---------- temiz başlangıç ----------

def test_a_clean_start_marks_clips_stale_and_every_chained_frame_but_the_first(chained, api):  # noqa: F811
    from app.engine import storyboard
    from app.engine.iteration import Ctx, _reusable
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH04"])
    clip = _clip(codes["S001_SH02"], status="passed")
    with session_scope() as s:
        s.get(Scene, codes["S001_SH02"]).status = "done"
    r = api.post(f"/api/projects/{pid}/chain/reset").json()
    assert r == {"clips": 1, "stale_frames": 3}
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert not shots["S001_SH01"]["chain_stale"] and all(shots[c]["chain_stale"] for c in ("S001_SH02", "S001_SH03", "S001_SH04"))
    assert shots["S001_SH02"]["status"] == "parsed" and shots["S001_SH02"]["variations"][0]["stale"] == "temiz başlangıç"
    ctx = Ctx(project_id=pid, scene_id=codes["S001_SH02"], run_id="r", provider=None, cfg={}, duration=4.0,
              prompt_spec={}, revision=0, visual_hash="h", render={})
    assert _reusable(ctx, 7, "full", "720p") is None                 # eski klip yeniden kullanılmaz
    assert api.get(f"/api/projects/{pid}/overview").json()["project"]["frozen"] is False


# =====================================================================================================================
# Kıvılcım temiz zincir koşusu bulguları: çekim içi "yolculuk", varış, zincirden gelen karakterler, tasarım kayması,
# kadraj çeşitliliği, durum sözcükleri, süre ve GPU havuzu.
# =====================================================================================================================

from app.core import chain as chain_rules  # noqa: E402
from app.engine import storyboard as sb  # noqa: E402

KIVILCIM = """# SEQUENCE: The Spark

INT. ATTIC WORKSHOP - NIGHT

Dusty boxes and tools fill the attic; moonlight falls through a round window.

[CHARACTER: Mila] Mila climbs into the attic holding a flashlight.

[CHARACTER: Bolt] The small robot Bolt sits lifeless on the workbench with its eyes dark.

[CHARACTER: Mila] [CHARACTER: Bolt] Mila connects a cable to Bolt's back and flips the switch.

[CHARACTER: Bolt] Bolt's eyes flicker and light up blue.

[CHARACTER: Bolt] [CHARACTER: Mila] Bolt looks at Mila and tilts its head.
"""
MEDIUM = {"shot": "medium", "movement": "static", "angle": "eye", "lens_mm": 35}
# Canlı veritabanındaki Kıvılcım çekimleri (2026-10-10): kamera, yazılı kadro, mekân
KIVILCIM_DB = [
    {"new_scene": True, "location": "Attic Workshop", "cast": [], "description": "Dusty attic.",
     "camera": {"shot": "wide", "movement": "pan", "angle": "eye", "lens_mm": 24}},
    {"location": "Attic Workshop", "cast": ["Mila"], "camera": MEDIUM, "description": "Mila climbs in."},
    {"location": "Attic Workshop", "cast": ["Bolt"], "camera": MEDIUM,
     "description": "Bolt sits lifeless on the workbench with its eyes dark."},
    {"location": "Attic Workshop", "cast": ["Bolt", "Mila"], "camera": MEDIUM, "description": "Mila connects a cable."},
    {"location": "Attic Workshop", "cast": ["Bolt"], "camera": MEDIUM, "description": "Bolt's eyes light up."},
    {"location": "Attic Workshop", "cast": ["Bolt", "Mila"], "camera": MEDIUM, "description": "Bolt looks at Mila."},
]


# ---------- (1a) otomatik sınıflama ----------

def test_the_kivilcim_shots_are_classified_cut_cut_cut_cut_continuous():
    plans = chain_rules.classify_shots(KIVILCIM_DB)
    assert [p["transition"] for p in plans] == ["new_scene", "cut", "cut", "cut", "cut", "continuous"]
    assert plans[4]["carried"] == ["Mila"] and set(plans[4]["cast"]) == {"Bolt", "Mila"}   # Mila K4'ten karede kalır
    assert plans[5]["reason"].startswith("kadro aynı:") and set(plans[5]["reason"][12:].split("+")) == {"Bolt", "Mila"}
    assert plans[2]["removed"] == ["Mila"] and plans[2]["carried"] == []                  # Mila'dan Bolt'a: Mila çıkar
    assert plans[1]["reason"] == "kadro değişti: karaktersiz → Mila; kadraj değişti: geniş → orta"
    assert "Mila zincirden gelir" in plans[4]["reason"]


def test_the_kivilcim_script_is_classified_on_ingest_and_mila_is_carried_into_sh05(api, mk_project):
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    order = ["S001_SH01", "S001_SH02", "S001_SH03", "S001_SH04", "S001_SH05", "S001_SH06"]
    assert [shots[c]["transition"] for c in order] == ["new_scene", "cut", "cut", "cut", "cut", "continuous"]
    assert shots["S001_SH05"]["chain_cast"] == ["Mila"] and set(shots["S001_SH05"]["characters"]) == {"Bolt", "Mila"}
    assert shots["S001_SH06"]["transition_reason"].startswith("kadro aynı:")
    assert shots["S001_SH03"]["chain_cast"] == []
    ingest(pid, KIVILCIM, auto_music=False)                                  # aynı senaryo: değişiklik yok
    again = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert again["S001_SH05"]["chain_cast"] == ["Mila"]


def test_the_script_can_force_a_transition_and_a_leaving_character_is_not_carried():
    from app.hermes import rules
    text = KIVILCIM.replace("[CHARACTER: Bolt] Bolt's eyes flicker", "CONTINUOUS:\n\n[CHARACTER: Bolt] Bolt's eyes flicker")
    shots = rules.parse(text)["shots"]
    assert shots[4]["transition"] == "continuous" and shots[4]["transition_reason"] == "senaryoda CONTINUOUS"
    left = rules.parse(KIVILCIM.replace("flips the switch.", "flips the switch. Mila leaves the attic."))["shots"]
    assert left[4]["carried"] == [] and left[4]["removed"] == ["Mila"]          # senaryo çıktığını söylüyor
    cut = rules.parse(KIVILCIM.replace("[CHARACTER: Bolt] [CHARACTER: Mila] Bolt looks",
                                       "CUT TO:\n\n[CHARACTER: Bolt] [CHARACTER: Mila] Bolt looks"))["shots"]
    assert cut[5]["transition"] == "cut"


def test_a_camera_change_reclassifies_the_shots(api, mk_project):
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    api.patch(f"/api/scenes/{shots['S001_SH06']['id']}", json={"camera": {**MEDIUM, "shot": "close_up"}})
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH06"]["transition"] == "cut" and "kadraj değişti" in shots["S001_SH06"]["transition_reason"]


# ---------- (1b) çekim sonu karesi Kn_end ----------

@pytest.fixture
def ended(chained):  # noqa: F811
    """Zincir fikstürü, çekim sonu kareleri açık."""
    fake, pid, codes, state = chained
    with session_scope() as s:
        from app.models import Project
        p = s.get(Project, pid)
        p.settings = {**(p.settings or {}), "chain_end_frame": True}
    return fake, pid, codes, state


def _prompts(fake):
    from tests.test_qwen_edit import edit_runs
    return [r["input"]["workflow"]["170:151"]["inputs"]["prompt"] for r in edit_runs(fake)]


def test_a_shot_before_a_cut_ends_on_its_own_end_frame(ended):
    from app.engine.iteration import _prepare
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH01"])                       # SH02 kesmeyle gelir: SH01 kendi son karesine gider
    end = [p for p in _prompts(fake) if p.startswith("Image 1 is the first frame of this shot")]
    assert len(end) == 1 and "when its own action is completed" in end[0] and "wide shot" in end[0]
    edit_run = [r for r in fake.runs if "170:151" in r["input"]["workflow"]][-1]["input"]["workflow"]
    assert edit_run["170:168"]["inputs"]["value"] is True                       # Lightning
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH01"])
        assert sc.end_board_key and sc.end_board_parent == sc.board_sha and sc.end_board_note is None
    ctx = _prepare(pid, codes["S001_SH01"], "run")
    assert ctx.render["last_frame_kind"] == "end" and ctx.target.arrival_kind == "end"
    assert ctx.render["last_frame_key"] == sb.clean_key(sc.end_board_key)
    sb.make_board(pid, codes["S001_SH02"])                       # SH03 devam ediyor: SH02'nin son karesi K3, Kn_end yok
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH02"]).end_board_key is None


def test_the_end_frame_passes_the_gates_or_the_video_ends_freely(ended):
    from app.engine.iteration import _prepare
    fake, pid, codes, state = ended
    state["continuity"] = [0.3] * 3                              # K1 (öncesiz) geçer; son kare üç denemede K1'den kopuk
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH01"])
        assert sc.end_board_note.startswith(sb.END_FAIL) and "3 deneme" in sc.end_board_note
    ctx = _prepare(pid, codes["S001_SH01"], "run")
    assert ctx.last_frame is None                               # kapıdan geçmeyen son kare videoya verilmez


def test_the_end_frame_setting_can_be_turned_off(chained):  # noqa: F811
    fake, pid, codes, _ = chained                               # fikstür: chain_end_frame kapalı
    sb.make_board(pid, codes["S001_SH01"])
    assert not [p for p in _prompts(fake) if p.startswith("Image 1 is the first frame")]


def test_the_end_frame_is_charged_at_the_lightning_price_in_free_mode(sample_scene):
    from app.budget import pricing
    from sqlalchemy import text
    pid, codes = sample_scene()
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:
        rows = s.execute(text("SELECT amount_usd FROM cost_ledger WHERE project_id = :p AND kind = 'commit' "
                              "AND provider = 'h3:storyboard:end'"), {"p": pid}).scalars().all()
    assert rows and float(rows[0]) == pytest.approx(pricing.estimate_keyframe("h3", model="qwen_edit",
                                                                              edit_mode="lightning"), abs=1e-4)


def test_the_estimate_includes_the_end_frames(api):
    from app.budget import pricing
    r = api.post("/api/estimate", json={"text": KIVILCIM, "preset": "economy"}).json()
    ends = {x["code"]: x["end_frame"] for x in r["shots"]}
    assert ends == {"S001_SH01": True, "S001_SH02": True, "S001_SH03": True, "S001_SH04": True, "S001_SH05": False,
                    "S001_SH06": True}                          # SH05 -> SH06 devam: SH05'in son karesi K6
    price = pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode="lightning")
    assert r["end_frames"] == 5 and r["end_frame_usd"] == pytest.approx(5 * price, abs=1e-4)
    assert 0.003 < price < 0.012                                # ~0,007 $
    off = api.post("/api/estimate", json={"text": KIVILCIM, "preset": "economy",
                                          "settings": {"chain_end_frame": False}}).json()
    assert off["end_frames"] == 0 and off["expected_usd"] < r["expected_usd"]


# ---------- (2) varış skoru, kesintisiz dikiş, geçiş, içerik ----------

def test_the_arrival_score_is_recorded_and_flags_a_clip_that_does_not_land(monkeypatch, tmp_path):
    from tests.media_utils import make_video
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(scorer, "score_video", lambda path, target: scorer.Result(
        clip=0.95, face=None, composite=0.95, detail={"faces_seen": 0, "identity": {}}))
    clip = make_video(tmp_path / "c.mp4", seconds=2.0, size="160x90", fps=24)
    monkeypatch.setattr(scorer, "arrival", lambda path, key, target: {"score": 0.82, "dino": 0.83, "identity": 0.8})
    score = gate.score_video(clip, 1, CFG, scorer.Target(next_frame="storyboard/x/e_clean.jpg", arrival_kind="end"))
    assert score.passed and score.detail["arrival"]["target"] == "end"
    assert score.detail["review"] == ["son kare çekimin son karesine tam varmıyor (varış 0,82, eşik 0,85)"]
    monkeypatch.setattr(scorer, "arrival", lambda path, key, target: {"score": 0.9, "dino": 0.91, "identity": None})
    assert "review" not in gate.score_video(clip, 1, CFG, scorer.Target(next_frame="k", arrival_kind="next")).detail


def test_the_arrival_score_joins_the_ranking(mk_project):
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        sid = str(s.scalars(select(Scene).where(Scene.project_id == pid)).first().id)
        for seed, composite, arrival in ((1, 0.95, 0.70), (2, 0.92, 0.97)):
            s.add(Variation(scene_id=sid, tier="full", wave_no=0, engine="h3", seed=seed, prompt_revision=0,
                            status="passed", composite_score=composite, video_key=None,
                            params={"run": "r", "score_detail": {"arrival": {"score": arrival}}}))
    winners = gate.finalize_refined_pool(sid, "r", 1, {})
    with session_scope() as s:
        assert s.get(Variation, winners[0]).seed == 2               # 0,92×0,7+0,97×0,3 > 0,95×0,7+0,70×0,3
    assert gate.rank_score(0.9, None) == 0.9


def _winner(sid, tmp_path, status="refined"):
    from app import storage
    from tests.media_utils import make_video
    video = make_video(tmp_path / f"{sid}.mp4", seconds=1.0, size="160x90", fps=24)
    key = f"variations/{sid}/w.mp4"
    storage.put_file(video, key)
    with session_scope() as s:
        v = Variation(scene_id=sid, tier="full", wave_no=0, engine="h3", seed=3, prompt_revision=0, status=status,
                      params={"resolution": "720p"}, video_key=key, refined_rank=1)
        s.add(v)
        s.flush()
        return str(v.id)


def test_exact_seams_start_from_the_previous_clips_real_last_frame(mk_project, tmp_path, monkeypatch):
    from app import storage
    from app.engine import iteration
    from app.hermes.ingest import ingest
    pid = mk_project(5.0, seam_mode="exact")
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        codes = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        s.get(Scene, codes["S001_SH05"]).status = "generating"
    assert iteration.wait_for_seam(pid, codes["S001_SH05"]) == ("off", None)        # SH05 kesmeyle gelir
    monkeypatch.setattr(iteration, "SEAM_POLL_S", 0.01)
    monkeypatch.setattr(iteration, "SEAM_WAIT_S", 0.05)
    assert iteration.wait_for_seam(pid, codes["S001_SH06"])[0] == "stop"            # SH05 hâlâ üretiliyor, süre bitti
    with session_scope() as s:
        assert "Kesintisiz dikiş" in s.get(Scene, codes["S001_SH06"]).last_error
        s.get(Scene, codes["S001_SH05"]).status = "done"
    vid = _winner(codes["S001_SH05"], tmp_path)
    state, key = iteration.wait_for_seam(pid, codes["S001_SH06"])
    assert state == "ready" and key == f"seam/{vid}.jpg" and storage.exists(key)
    ctx = iteration._prepare(pid, codes["S001_SH06"], "run", key)
    assert ctx.render["keyframe_key"] == key and ctx.render["seam"] is True


def test_a_new_winner_makes_the_next_exact_seam_stale(mk_project, tmp_path):
    from app.engine import iteration
    from app.hermes.ingest import ingest
    pid = mk_project(5.0, seam_mode="exact")
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        codes = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    with session_scope() as s:
        old = Variation(scene_id=codes["S001_SH06"], tier="full", wave_no=0, engine="h3", seed=1, prompt_revision=0,
                        status="refined", refined_rank=1, video_key="variations/o.mp4",
                        params={"keyframe_key": "seam/old.jpg"})
        s.add(old)
        s.get(Scene, codes["S001_SH06"]).status = "done"
        s.flush()
        old_id = old.id
    _winner(codes["S001_SH05"], tmp_path)
    iteration.invalidate_next_seam(pid, codes["S001_SH05"])
    with session_scope() as s:
        assert s.get(Variation, old_id).params["stale_keyframe"] and s.get(Scene, codes["S001_SH06"]).status == "needs_review"


def test_continuous_seams_can_crossfade_in_the_film(tmp_path):
    from app.post import timeline
    from tests.media_utils import make_video
    a = timeline.normalize(make_video(tmp_path / "a.mp4", seconds=2.0, fps=24), str(tmp_path / "na.mp4"))
    b = timeline.normalize(make_video(tmp_path / "b.mp4", seconds=2.0, fps=24, color="red"), str(tmp_path / "nb.mp4"))
    c = timeline.normalize(make_video(tmp_path / "c.mp4", seconds=2.0, fps=24), str(tmp_path / "nc.mp4"))
    durations = [timeline.probe_duration(x) for x in (a, b, c)]
    starts = timeline.crossfade_join([a, b, c], durations, [False, True, False], 4, str(tmp_path / "o.mp4"))
    assert starts[1] == pytest.approx(durations[0] - 4 / 24, abs=1e-3)              # devam: 4 kare bindirme
    assert starts[2] == pytest.approx(starts[1] + durations[1], abs=1e-3)           # kesme: düz
    assert timeline.probe_duration(str(tmp_path / "o.mp4")) == pytest.approx(sum(durations) - 4 / 24, abs=0.06)


def test_the_continuity_check_measures_content_at_seams():
    import numpy as np
    from app.quality import consistency
    rng = np.random.default_rng(1)
    left = rng.integers(60, 200, (90, 160, 3), dtype=np.uint8)
    right = rng.integers(60, 200, (90, 160, 3), dtype=np.uint8)
    assert consistency.content_similarity(left, left) == pytest.approx(1.0, abs=1e-3)
    assert consistency.content_similarity(left, right) < consistency.SEAM_CONTENT_MIN       # aynı renk, başka görüntü
    assert consistency.compare(consistency.frame_stats(left), consistency.frame_stats(right))["score"] < 1


# ---------- (3) zincirden gelen karakterler ----------

def test_a_carried_character_gets_reference_invariants_and_identity_gate(api, mk_project):
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    mila = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Mila")
    inv = ["round yellow glasses (always worn)", "curly black hair in a high ponytail",
           "green overalls over a white t-shirt"]
    assert api.patch(f"/api/assets/{mila['id']}", json={"invariants": inv}).status_code == 200    # insan karakterde de
    with session_scope() as s:
        sc = s.scalars(select(Scene).where(Scene.project_id == pid, Scene.scene_code == "S001_SH05")).one()
        chars = load_characters(s, sc.id)
        for ch in chars:
            ch.reference_images = [f"assets/{ch.id}/still/x.png"]
        composed = compose_for_scene(sc, chars, load_assets(s, sc.id), None)
        target = scorer.target_for(sc, chars, None)
    assert {c.name for c in chars} == {"Bolt", "Mila"}
    assert "Mila must always have: round yellow glasses (always worn)" in composed.prompt
    assert "missing glasses" in composed.negative
    assert "Mila" in {name for name, _ in target.faces}                              # kimlik kapısında


def test_a_character_who_is_not_in_the_shot_is_removed_from_the_chain_frame():
    refs = [{"name": "S001_SH02", "key": "k", "kind": "frame"}, {"name": "Bolt", "key": "b", "kind": "character"}]
    text = sb.chain_prompt("Bolt sits lifeless.", refs, "cut", {"cast": ["Bolt"], "removed": ["Mila"]}, MEDIUM)
    assert "Mila is not in this shot: remove Mila from the frame completely." in text


def test_a_locked_asset_still_takes_invariants(api, mk_project):
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    mila = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["name"] == "Mila")
    with session_scope() as s:
        s.get(__import__("app.models", fromlist=["Asset"]).Asset, mila["id"]).status = "locked"
    r = api.patch(f"/api/assets/{mila['id']}", json={"invariants": ["round yellow glasses (always worn)"]})
    assert r.status_code == 200 and r.json()["status"] == "locked" and r.json()["version"] == mila["version"] + 1


# ---------- (4) tasarım kayması ----------

def test_every_edit_prompt_names_the_still_and_the_invariants():
    refs = [{"name": "S001_SH04", "key": "k", "kind": "frame"},
            {"name": "Bolt", "key": "b", "kind": "character", "invariants": ["exactly one bent antenna with a small bulb"]},
            {"name": "Mila", "key": "m", "kind": "character", "invariants": []}]
    text = sb.chain_prompt("Bolt's eyes light up.", refs, "continuous")
    assert "Bolt must look exactly like image 2, always with exactly one bent antenna with a small bulb" in text
    assert "Mila must look exactly like image 3" in text
    assert "Bolt must look exactly like image 1" in sb.edit_prompt("p", refs[1:])


def test_invariants_give_automatic_negatives():
    from app.style.composer import invariant_negatives
    out = invariant_negatives(["exactly one bent antenna with a small bulb", "round yellow glasses (always worn)"])
    assert {"second antenna", "extra antenna", "extra bulb", "missing glasses"} <= set(out)


def test_storyboard_first_approves_the_whole_strip_then_starts_the_videos(api, mk_project, monkeypatch):
    from app.hermes.ingest import ingest
    pid = mk_project(5.0, require_board_approval=True)
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        shots = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        for code, sc in shots.items():
            sc.board_key, sc.board_status, sc.status = f"storyboard/{sc.id}/x.jpg", "pending", "board_review"
        shots["S001_SH03"].board_status = "changes_requested"
        shots["S001_SH06"].board_key = None
    calls = []
    monkeypatch.setattr("app.api.storyboard.dispatch_all", lambda s, pid, **kw: calls.append(pid) or {"planned": {}})
    r = api.post(f"/api/projects/{pid}/boards/approve").json()
    assert r["approved"] == ["S001_SH01", "S001_SH02", "S001_SH04", "S001_SH05"]
    assert r["review"] == ["S001_SH03"] and r["missing"] == ["S001_SH06"] and calls == [pid]
    with session_scope() as s:
        actions = [a for (a,) in s.execute(__import__("sqlalchemy").text(
            "SELECT action FROM curation_events WHERE project_id = :p"), {"p": pid})]
    assert "board_approve_all" in actions


def test_a_chained_paid_project_requires_board_approval_by_default(chained, api):  # noqa: F811
    fake, pid, codes, _ = chained
    assert api.get(f"/api/projects/{pid}/overview").json()["project"]["settings"]["require_board_approval"] is True


# ---------- (5) kadraj çeşitliliği ----------

def test_a_cut_frame_asks_for_its_scale_and_lens():
    refs = [{"name": "S001_SH04", "key": "k", "kind": "frame"}, {"name": "Bolt", "key": "b", "kind": "character"}]
    text = sb.chain_prompt("Bolt's eyes light up.", refs, "cut", {"cast": ["Bolt"], "removed": []}, MEDIUM)
    assert "Reframe the camera for this shot: a medium shot of Bolt, 35 mm lens." in text
    assert "Keep the same room, layout" not in text


def test_the_framing_check_follows_the_requested_scale():
    assert gate.framing_check(0.35, "medium")["ok"] and not gate.framing_check(0.9, "medium")["ok"]
    assert not gate.framing_check(0.1, "close_up")["ok"] and gate.framing_check(0.5, "close_up")["ok"]
    assert gate.framing_check(None, "medium") is None


def test_a_cut_frame_with_the_wrong_framing_is_redrawn(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    areas = [0.92, 0.4]                                          # K2 (kesme, orta plan): önce çok yakın, sonra doğru
    monkeypatch.setattr(scorer, "framing", lambda path, target: areas.pop(0) if areas else 0.4)
    sb.make_board(pid, codes["S001_SH02"])
    assert len([p for p in _prompts(fake) if "must clearly differ from image 1" in p]) == 2
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        assert k2.board_status == "pending" and "kadraj" not in (k2.board_note or "")


def test_a_cut_frame_that_never_matches_the_framing_passes_with_a_review_note(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.95)
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        assert k2.board_status == "pending" and "kadraj istenen ölçekte değil (orta plan beklendi" in k2.board_note
    assert len([p for p in _prompts(fake) if "must clearly differ from image 1" in p]) == 3


# ---------- (6) durum sözcükleri, süre, GPU havuzu ----------

def test_state_words_go_into_the_prompt_and_their_opposites_into_the_negative():
    from app.style.composer import compose_prompt
    c = compose_prompt({"prompt": "Bolt sits lifeless on the workbench with its eyes dark.", "lang": "en",
                        "characters": ["Bolt"]}, [], None)
    assert c.prompt.endswith("Exception for this shot, overriding the reference images and the invariants: the eyes are "
                             "dark and unlit, emitting no light.")
    assert "glowing eyes" in c.negative and "lit eyes" in c.negative
    turkish = compose_prompt({"prompt": "Boş yol.", "lang": "tr", "characters": []}, [], None)
    assert "Exception" not in turkish.prompt                   # Türkçe (çevrilmemiş) metinde uydurma yok


def test_a_long_clip_is_trimmed_to_its_duration_and_a_continuous_shot_keeps_its_arrival_end(tmp_path):
    from app.post import pipeline
    from tests.media_utils import make_video
    assert pipeline.trim_window(107 / 24, 4.0, keep_end=True) == (round(107 / 24 - 4.0, 3), 4.0)
    assert pipeline.trim_window(107 / 24, 4.0, keep_end=False) == (0.0, 4.0)
    assert pipeline.trim_window(4.03, 4.0, keep_end=False) is None
    clip = make_video(tmp_path / "c.mp4", seconds=107 / 24, size="160x90", fps=24)
    out = pipeline.trimmed(clip, 4.0, True, str(tmp_path / "t.mp4"))
    assert pipeline.probe_duration(out) == pytest.approx(4.0, abs=0.05)


def test_the_gpu_pools_can_be_limited_to_the_rtx_5090_pool(monkeypatch):
    from app.live import PaidCallBlocked
    monkeypatch.setattr(settings, "runpod_gpu_pools", "ADA_32_PRO")
    monkeypatch.setattr(settings, "runpod_workers_max", "3")
    monkeypatch.setattr(settings, "runpod_max_pending_jobs", 6)
    body = runpod_h3.desired_config()
    assert body["gpu"]["pools"] == ["ADA_32_PRO"] and body["workers"]["max"] == 3
    monkeypatch.setattr(settings, "runpod_gpu_pools", "auto")
    assert body["gpu"]["pools"] != runpod_h3.desired_config()["gpu"]["pools"]          # auto: GPU tiplerinden
    monkeypatch.setattr(settings, "runpod_gpu_pools", "AMPERE_16")
    with pytest.raises(PaidCallBlocked, match="Bilinmeyen GPU havuzu"):
        runpod_h3.desired_config()


# =====================================================================================================================
# Kıvılcım kare şeridi testi (c55e8f5 sonrası): zincirden gelen karakterin ikinci örneği, durum/referans çakışması,
# "Değişiklik iste" akışı, sekansın ilk çekiminin son karesi, önbellekteki eski görsel, zincir geçersizleşmesi, soğuk
# başlatma.
# =====================================================================================================================

MILA_REFS = [{"name": "S001_SH04", "key": "k", "kind": "frame"},
             {"name": "Bolt", "key": "b", "kind": "character", "invariants": ["round glass eyes (glow blue only when switched on)"]},
             {"name": "Mila", "key": "m", "kind": "character", "invariants": ["round yellow glasses (always worn)"]}]


# ---------- (1) zincirden gelen karakter iki kez çizilmesin ----------

def test_characters_already_in_the_previous_frame_are_kept_not_added():
    plans = chain_rules.classify_shots(KIVILCIM_DB)
    assert plans[4]["kept"] == ["Bolt", "Mila"] and plans[2]["kept"] == []            # SH05: ikisi de K4'te var
    text = sb.chain_prompt("Bolt's eyes light up.", MILA_REFS, "cut", plans[4], MEDIUM)
    assert "keep the single Mila already in image 1 in the same place and pose — image 3 is Mila's identity reference" in text
    assert "do not add another Mila" in text and "Mila must look exactly like image" not in text
    assert "Reframe the camera for this shot: a medium shot of Bolt, 35 mm lens." in text    # kadraj yazılı kadroya göre
    assert sb.ONCE in text
    entering = sb.chain_prompt("Mila climbs in.", MILA_REFS, "cut", plans[3], MEDIUM)       # SH04: Mila K3'te yok
    assert "Mila must look exactly like image 3" in entering and "keep the single Bolt already in image 1" in entering


def test_the_end_frame_keeps_every_character_of_its_first_frame():
    text = sb.end_prompt("Bolt sits lifeless.", MILA_REFS, MEDIUM)
    assert "keep the single Bolt already in image 1 in the same place and pose" in text and "keep the single Mila" in text
    assert "unchanged" not in text                              # image 1'deki yanık göz ya da fazla parça korunmasın
    assert "must look exactly like" not in text and sb.ONCE in text


def test_a_character_twice_in_the_frame_fails_the_gate():
    detail = {"faces_seen": 3, "faces_measured": 3, "identity": {"Mila": 1.0},
              "face_instances": {"Mila": [[1.0, 0.93]], "Ayşe": [[]]}}
    target = scorer.Target(faces=(("Mila", ("assets/m/still/m.png",)), ("Ayşe", ("assets/a/still/a.png",))),
                           face_expected=False)
    out = gate.identity_anomalies(detail, target, CFG)
    assert out == [{"type": "duplicate_character", "characters": ["Mila"]}]
    one = {**detail, "face_instances": {"Mila": [[1.0, 0.31]]}}                       # ikinci yüz başka biri
    assert gate.identity_anomalies(one, target, CFG) == []
    robot = {"look": {"Bolt": 0.9}, "look_dino": {"Bolt": 0.9}, "look_instances": {"Bolt": [[0.9, 0.88]]}}
    assert gate.duplicates(robot, CFG) == ["Bolt"]
    drawn = sb.Drawn("qwen_edit", 0.95, False, 3, out, {})
    assert sb.identity_text(drawn) == "Mila karede birden fazla"


def test_overlapping_crops_of_one_robot_count_once_and_two_robots_twice():
    boxes = scorer.crop_boxes()
    center = boxes.index((0.25, 0.25, 0.75, 0.75))
    sims = [0.80] * 9
    sims[center] = 0.92
    assert scorer.separate_instances(sims) == [0.92]                                 # komşular merkezle örtüşür
    left, right = boxes.index((0.0, 0.25, 0.5, 0.75)), boxes.index((0.5, 0.25, 1.0, 0.75))
    sims = [0.70] * 9
    sims[left], sims[right] = 0.91, 0.90
    assert scorer.separate_instances(sims) == [0.91, 0.90]


def test_each_face_counts_for_the_character_it_matches_best(monkeypatch):
    import threading
    import numpy as np
    from PIL import Image as Img
    e = np.eye(4)

    class FakeModels:
        scale, lock = 100.0, threading.Lock()

        def encode_images(self, images):
            return np.tile(e[3], (len(images), 1))

        def encode_texts(self, texts):
            return np.tile(e[3], (len(texts), 1))

        def faces(self, image):
            return [(0.9, e[0], 140), (0.9, e[0] * 0.99 + e[2] * 0.14, 130), (0.9, e[1], 120)]   # iki Mila, bir Ayşe

        def reference_face(self, key):
            return e[0] if "mila" in key else e[1]

    monkeypatch.setattr(scorer, "models", lambda: FakeModels())
    target = scorer.Target(faces=(("Mila", ("assets/mila/still/m.png",)), ("Ayşe", ("assets/ayse/still/a.png",))))
    detail = scorer.score_images([Img.new("RGB", (64, 36))], target).detail
    assert [len(x) for x in detail["face_instances"]["Mila"]] == [2] and len(detail["face_instances"]["Ayşe"][0]) == 1
    assert gate.duplicates(detail, CFG) == ["Mila"]


# ---------- (2) durum referansı ve değişmezi ezer ----------

def test_the_state_comes_last_and_overrides_the_reference_and_the_invariant():
    from types import SimpleNamespace
    from app.style.composer import compose_prompt
    bolt = SimpleNamespace(type="character", name="Bolt", description="small copper robot", render_style=None,
                           invariants=["round glass eyes (glow blue only when switched on)"])
    c = compose_prompt({"prompt": "Bolt is still switched off, eyes dark and unlit.", "lang": "en",
                        "characters": ["Bolt"]}, [bolt], None)
    assert c.prompt.index("Exception for this shot") > c.prompt.index("Bolt must always have")
    assert "Bolt is switched off — dark, emitting no light" in c.prompt and \
        "the eyes are dark and unlit, emitting no light" in c.prompt
    assert {"glowing eyes", "lit eyes", "blue glow"} <= {x.strip() for x in c.negative.split(",")}
    text = sb.chain_prompt(c.prompt, MILA_REFS[:2], "cut", {"cast": ["Bolt"], "kept": ["Bolt"]}, MEDIUM)
    assert text.index("Exception for this shot") > text.index("keep the single Bolt")
    end = sb.end_prompt(c.prompt, MILA_REFS[:2], MEDIUM)
    assert "Keep this shot's own state; do not anticipate the next shot." in end and "Exception for this shot" in end


def test_the_director_note_is_a_state_source_too():
    from app.style.composer import compose_prompt
    c = compose_prompt({"prompt": "Bolt sits on the workbench.", "lang": "en", "characters": ["Bolt"],
                        "note": "Bolt's eyes stay dark, no glow."}, [], None)
    assert "Director note: Bolt's eyes stay dark, no glow." in c.prompt
    assert c.prompt.endswith("the eyes are dark and unlit, emitting no light.") and "glowing eyes" in c.negative


# ---------- (3) "Değişiklik iste" yeniden çizer ----------

def test_a_change_request_note_goes_into_the_prompt_but_not_the_fingerprint(api, mk_project):
    """Not görsel özete girmez: yönetmen notlu kareyi yeniden çizdirmeden onaylayabilmeli (duman testi B)."""
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        sc = s.scalars(select(Scene).where(Scene.project_id == pid, Scene.scene_code == "S001_SH05")).one()
        sc.board_key, sid = f"storyboard/{sc.id}/x.jpg", str(sc.id)
        before = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), None)
    r = api.post(f"/api/scenes/{sid}/board", json={"status": "changes_requested", "note": "exactly ONE Mila"})
    assert r.status_code == 200
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.director_note == "exactly ONE Mila"
        after = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), None)
        prompt = compose_for_scene(sc, load_characters(s, sc.id), load_assets(s, sc.id), None).prompt
    assert after == before and "Director note: exactly ONE Mila." in prompt


def test_prepare_redraws_a_requested_frame_with_the_note_and_then_its_successors(ended, api, eager):
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH04"])
    with session_scope() as s:
        old = {c: s.get(Scene, codes[c]).board_sha for c in ("S001_SH02", "S001_SH03", "S001_SH04")}
    assert api.post(f"/api/projects/{pid}/storyboard").json()["queued"] == ["S002_SH01"]      # yalnızca eksik kare
    api.post(f"/api/scenes/{codes['S001_SH02']}/board", json={"status": "changes_requested", "note": "lamp off"})
    plan = api.get(f"/api/projects/{pid}/storyboard/plan").json()
    assert plan["requested"] == ["S001_SH02"] and plan["followers"] == ["S001_SH03", "S001_SH04"]
    assert plan["frames"] == ["S001_SH02", "S001_SH03", "S001_SH04"] and plan["estimate_usd"] > 0
    fake.image = png((30, 160, 90))
    r = api.post(f"/api/projects/{pid}/storyboard").json()
    assert r["queued"][:3] == ["S001_SH02", "S001_SH03", "S001_SH04"]
    with session_scope() as s:
        k2, k3, k4 = (s.get(Scene, codes[c]) for c in ("S001_SH02", "S001_SH03", "S001_SH04"))
        assert k2.board_sha != old["S001_SH02"] and k2.board_status == "pending"
        assert k3.board_parent == k2.board_sha and k4.board_parent == k3.board_sha


def test_the_note_reaches_the_redrawn_frames_prompt(ended, api, eager):
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH03"])
    api.post(f"/api/scenes/{codes['S001_SH03']}/board", json={"status": "changes_requested", "note": "exactly ONE lamp"})
    api.post(f"/api/projects/{pid}/storyboard")
    assert any("Director note: exactly ONE lamp." in p for p in _prompts(fake))


def test_redraw_chain_on_a_requested_frame_redraws_it_and_its_successors(ended, api, monkeypatch):
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH04"])
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).board_status = "changes_requested"
    calls = []
    monkeypatch.setattr("app.api.storyboard.queue_requested_redraw",
                        lambda s, sc, followers: calls.append((sc.scene_code, [x.scene_code for x in followers]))
                        or [sc.scene_code, *(x.scene_code for x in followers)])
    r = api.post(f"/api/scenes/{codes['S001_SH03']}/board/redraw-chain").json()
    assert r["codes"] == ["S001_SH03", "S001_SH04"] and calls == [("S001_SH03", ["S001_SH04"])]


# ---------- (4) sekansın ilk çekiminin son karesi ----------

def test_a_frame_drawn_before_the_chain_still_gets_its_end_frame(ended, api, eager):
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:                              # zincirden önce çizilmiş kare: sha yok, son kare yok
        sc = s.get(Scene, codes["S001_SH01"])
        sc.board_sha = sc.end_board_key = sc.end_board_parent = sc.end_board_sha = None
    plan = api.get(f"/api/projects/{pid}/storyboard/plan").json()
    assert "S001_SH01" in plan["ends"]
    api.post(f"/api/projects/{pid}/storyboard")
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH01"])
        assert sc.end_board_key and sc.end_board_parent.startswith("k") and sc.board_sha is None
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH01"]["end_board_current"] is True


# ---------- (5) yeniden çizilen kare tarayıcıda yeni görünür ----------

def test_a_redrawn_frame_gets_a_new_url_and_the_media_is_revalidated(ended, api):
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH01"])
    first = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}["S001_SH01"]
    fake.image = png((200, 30, 30))
    sb.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)
    second = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}["S001_SH01"]
    assert first["board_url"] != second["board_url"] and "&v=" in second["board_url"]
    assert first["board_url"].split("&v=")[0] == second["board_url"].split("&v=")[0]      # aynı dosya, yeni sürüm
    r = api.get(second["board_url"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers["etag"]
    assert api.get(second["board_url"], headers={"If-None-Match": r.headers["etag"]}).status_code == 304


# ---------- (6) zincir geçersizleşmesi bütün sonraki halkaları sayar ----------

def test_a_redrawn_frame_lists_every_following_link_even_after_a_cut(ended):
    import json as _json
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH04"])
    killswitch.r().delete(killswitch.EVENTS.format(pid))
    fake.image = png((10, 200, 40))
    sb.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)       # SH02 kesmeyle K1'den düzenlenir
    events = [_json.loads(e) for e in killswitch.r().lrange(killswitch.EVENTS.format(pid), 0, -1)]
    inv = next(e for e in events if e["kind"] == "chain_invalidated")
    assert inv["frames"] == ["S001_SH02", "S001_SH03", "S001_SH04"]


# ---------- (7) şerit incelemesinde boşta bekleme ----------

def test_frame_jobs_keep_the_worker_warm_during_strip_review(runpod, monkeypatch):  # noqa: F811
    import time as _time
    from app.live import vast_box
    applied = []
    monkeypatch.setattr(settings, "runpod_idle_timeout_s", 60)
    monkeypatch.setattr(settings, "runpod_review_idle_s", 300)
    monkeypatch.setattr(runpod_h3, "set_endpoint_idle", lambda seconds: (applied.append(seconds), killswitch.r().set(
        vast_box._k("rp_idle_applied"), seconds)))
    assert runpod_h3.idle_timeout_s() == 60
    runpod_h3.review_started()
    assert applied == [300] and runpod_h3.idle_timeout_s() == 300
    runpod_h3.review_started()                                                    # zaten uygulandı: yeniden yazılmaz
    assert applied == [300]
    assert runpod_h3.idle_overlap(1000.0, 1250.0, 1000.0) == pytest.approx(min(250.0, runpod_h3.MAX_TICK_S))
    runpod_h3.review_ended(_time.time())                                          # pencere sürüyor: dokunulmaz
    assert applied == [300]
    killswitch.r().set(vast_box._k("rp_review_until"), _time.time() - 1)
    runpod_h3.review_ended(_time.time())
    assert applied == [300, 60] and runpod_h3.idle_timeout_s() == 60
    monkeypatch.setattr(settings, "runpod_review_idle_s", 0)                      # kapalı
    killswitch.r().delete(vast_box._k("rp_idle_applied"))
    runpod_h3.review_started()
    assert applied == [300, 60]


# =====================================================================================================================
# Kıvılcım tam koşusu (12bf435 sonrası): göz durumu (durum referansı, otomatik durum referansı, durum kapısı), eski
# dikiş kuralıyla üretilmiş klip, eski kare notu, kazanan klibin varış skoru.
# =====================================================================================================================

from app.core import states as state_rules  # noqa: E402


def _kivilcim(api, mk_project, state_off: str | None = None):
    """Kıvılcım: Bolt insan değil, durağan görseli ve ışık rengi değişmezi var; isteğe bağlı elle durum referansı."""
    from app.hermes.ingest import ingest
    from app.models import Asset, Character
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    with session_scope() as s:
        for ch in s.scalars(select(Character).where(Character.project_id == pid)):
            ch.reference_images = [f"assets/{ch.id}/still/s.png"]
            if ch.name == "Bolt":
                ch.kind = "non_human"
                a = s.get(Asset, ch.asset_id)
                a.description = "small round copper robot, riveted body"
                a.invariants = ["round glass eyes (glow blue only when switched on)", "red button on its back"]
                if state_off:
                    a.references = {**(a.references or {}), "state_off": {"key": state_off}}
        codes = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    return pid, codes


def _shot(s, sid):
    from app.core.state import load_assets, load_characters
    from app.engine.iteration import compose_for_scene
    sc = s.get(Scene, sid)
    chars, assets = load_characters(s, sid), load_assets(s, sid)
    return sc, chars, assets, compose_for_scene(sc, chars, assets, None)


# ---------- (1a) durum referansı ----------

def test_a_state_reference_replaces_the_still_while_the_shot_is_in_that_state(api, mk_project):
    pid, codes = _kivilcim(api, mk_project, state_off="assets/b/state_off/off.png")
    with session_scope() as s:
        sc, chars, assets, composed = _shot(s, codes["S001_SH03"])                 # "with its eyes dark"
        assert state_rules.state_key(composed.states) == "off"
        refs, probes = sb.state_plan(s, sc, composed, chars, assets, CFG, None)
        lit = _shot(s, codes["S001_SH02"])                                         # durum yok: referans değişmez
        assert sb.state_plan(s, lit[0], lit[3], lit[1], lit[2], CFG, None) == ({}, [])
    assert refs == {"Bolt": "assets/b/state_off/off.png"}
    probe = probes[0]
    assert (probe["name"], probe["key"], probe["color"], probe["label"]) == ("Bolt", "off", "blue", "eyes dark")
    assert probe["wanted"] == "a photo of small round copper robot with dark, unlit eyes"
    assert probe["opposite"] == "a photo of small round copper robot with glowing blue eyes"
    out = sb.with_state_refs([{"name": "Bolt", "key": "still", "kind": "character"}], refs, "off")
    assert out == [{"name": "Bolt", "key": "assets/b/state_off/off.png", "kind": "character", "state": "off",
                    "state_image": True, "state_parts": ["eyes"], "invariants": []}]
    text = sb.chain_prompt("p", [{"name": "K", "key": "k", "kind": "frame"}, *out], "cut", {"cast": ["Bolt"], "kept": []},
                           MEDIUM)
    assert "Bolt must look exactly like image 2 (image 2 shows Bolt in this shot's state)" in text


def test_a_state_reference_can_be_added_to_a_locked_character(api, mk_project):
    from app.models import Asset, Character
    pid, codes = _kivilcim(api, mk_project)
    with session_scope() as s:
        aid = str(s.scalars(select(Character).where(Character.project_id == pid, Character.name == "Bolt")).one().asset_id)
        s.get(Asset, aid).status = "locked"
        version = s.get(Asset, aid).version
    r = api.post(f"/api/assets/{aid}/references?kind=state_off&filename=off.png", content=png((10, 10, 12)),
                 headers={"content-type": "image/png"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "locked" and body["version"] == version and body["references"]["state_off"]["key"]


# ---------- (1b) otomatik durum referansı ----------

def test_a_verified_earlier_frame_in_the_same_state_becomes_the_state_reference(api, mk_project, monkeypatch, tmp_path):
    from PIL import Image as Img
    from app import storage
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    pid, codes = _kivilcim(api, mk_project)
    with session_scope() as s:                                  # SH03'ün karesi (gözler karanlık) çizilmiş ve güncel
        sh03 = s.get(Scene, codes["S001_SH03"])
        sh03.board_key = f"storyboard/{sh03.id}/k.jpg"
        sh03.board_hash = visual_fingerprint(sh03, load_characters(s, sh03.id), load_assets(s, sh03.id), None)
        sh03.board_sha, sh03.board_status = "a" * 64, "approved"
        s.get(Scene, codes["S001_SH04"]).director_note = "Bolt's eyes stay dark"         # SH04 de kapalı
        clean = sb.clean_key(sh03.board_key)
    Img.new("RGB", (64, 36), (20, 20, 20)).save(tmp_path / "k.jpg")
    storage.put_file(str(tmp_path / "k.jpg"), clean, "image/jpeg")
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    probed = []
    monkeypatch.setattr(scorer, "state_probe", lambda image, probes: probed.append(probes) or
                        {probes[0]["label"]: {"score": 0.9, "glow": 0.1, "clip": 0.4}})
    monkeypatch.setattr(scorer, "character_crop", lambda image, keys: image.crop((0, 0, 32, 32)))
    with session_scope() as s:
        sc, chars, assets, composed = _shot(s, codes["S001_SH04"])
        refs, probes = sb.state_plan(s, sc, composed, chars, assets, CFG, None)
        again, _ = sb.state_plan(s, sc, composed, chars, assets, CFG, None)        # içerik-adresli: yeniden ölçülmez
    key = refs["Bolt"]
    assert key.startswith(f"state_refs/{codes['S001_SH03']}/bolt_off_") and storage.exists(key) and again == refs
    assert len(probed) == 1
    with session_scope() as s:                                  # sonda tutmazsa referans yok, aday yeniden ölçülmez
        storage.purge(key)
        killswitch.r().delete(sb.STATE_MISS.format(key))
        monkeypatch.setattr(scorer, "state_probe", lambda image, probes: probed.append(probes) or
                            {probes[0]["label"]: {"score": 0.1, "glow": 4.5, "clip": 0.2}})
        sc, chars, assets, composed = _shot(s, codes["S001_SH04"])
        assert sb.state_plan(s, sc, composed, chars, assets, CFG, None)[0] == {}
        sb.state_plan(s, sc, composed, chars, assets, CFG, None)
    assert len(probed) == 2 and killswitch.r().get(sb.STATE_MISS.format(key))


# ---------- (1c) durum kapısı ----------

def test_the_glow_measure_tells_dark_eyes_from_lit_ones():
    from PIL import Image as Img, ImageDraw
    def robot(eye):
        im = Img.new("RGB", (320, 180), (90, 55, 30))
        d = ImageDraw.Draw(im)
        for x in (120, 200):
            d.ellipse([x - 18, 70, x + 18, 106], fill=eye)
        return im
    dark, lit = robot((8, 8, 10)), robot((130, 248, 255))
    assert scorer.glow_fraction(dark, "blue") == 0 and scorer.glow_fraction(lit, "blue") > 10 * scorer.GLOW_REF
    red = robot((255, 40, 30))
    assert scorer.glow_fraction(red, "red") > scorer.GLOW_REF and scorer.glow_fraction(red, "blue") == 0


def test_the_state_probe_decides_by_the_glow_when_the_colour_is_known(monkeypatch):
    import threading
    import numpy as np
    from PIL import Image as Img, ImageDraw

    class FakeModels:
        scale, lock = 100.0, threading.Lock()

        def encode_images(self, images):
            return np.array([[1.0, 0.0]] * len(images))

        def encode_texts(self, texts):
            return np.array([[0.2, 0.98], [0.9, 0.44]])             # metin sondası yanlış yöne bakıyor

        def reference_dino(self, key):
            return None

    monkeypatch.undo()                                          # gerçek sonda (otomatik taklit kaldırılır), sahte modeller
    monkeypatch.setattr(scorer, "models", lambda: FakeModels())
    dark = Img.new("RGB", (160, 90), (40, 30, 20))
    lit = dark.copy()
    ImageDraw.Draw(lit).ellipse([60, 30, 90, 60], fill=(130, 248, 255))
    off = {"label": "eyes dark", "key": "off", "color": "blue", "wanted": "w", "opposite": "o", "keys": []}
    assert scorer.state_probe(dark, [off])["eyes dark"]["score"] == 1.0
    assert scorer.state_probe(lit, [off])["eyes dark"]["score"] == 0.0
    assert scorer.state_probe(lit, [{**off, "key": "on"}])["eyes dark"]["score"] == 1.0
    text_only = scorer.state_probe(dark, [{**off, "color": None}])["eyes dark"]
    assert text_only["glow"] is None and text_only["score"] == text_only["clip"] < 0.5   # renk yoksa OpenCLIP


def test_a_frame_whose_state_does_not_hold_is_redrawn_and_finally_flagged(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    probe = {"name": "Bolt", "label": "eyes dark", "key": "off", "color": "blue", "wanted": "w", "opposite": "o", "keys": []}
    monkeypatch.setattr(sb, "state_plan", lambda *a, **k: ({}, [probe]))
    scores = [0.0, 0.0, 0.0]
    monkeypatch.setattr(scorer, "state_probe_path", lambda path, probes: {"eyes dark": {
        "score": scores.pop(0) if scores else 0.0, "glow": 4.5, "clip": 0.2}})
    sb.make_board(pid, codes["S001_SH02"])
    assert len([p for p in _prompts(fake) if "must clearly differ from image 1" in p]) == 3                   # üç deneme
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        assert k2.board_status == "pending" and "durum (eyes dark) tutmadı" in k2.board_note


def test_a_frame_whose_state_holds_on_retry_needs_no_review(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    probe = {"name": "Bolt", "label": "eyes dark", "key": "off", "color": "blue", "wanted": "w", "opposite": "o", "keys": []}
    monkeypatch.setattr(sb, "state_plan", lambda *a, **k: ({}, [probe]))
    scores = [0.9, 0.0, 0.8]                                    # K1 (önce çizilir), K2 1. deneme, K2 2. deneme
    monkeypatch.setattr(scorer, "state_probe_path", lambda path, probes: {"eyes dark": {
        "score": scores.pop(0) if scores else 0.8, "glow": 0.1, "clip": 0.4}})
    sb.make_board(pid, codes["S001_SH02"])
    assert len([p for p in _prompts(fake) if "must clearly differ from image 1" in p]) == 2
    with session_scope() as s:
        assert "durum" not in (s.get(Scene, codes["S001_SH02"]).board_note or "")


# ---------- (2) eski dikiş kuralıyla üretilmiş klip ----------

def test_a_clip_made_under_the_old_seam_rule_is_marked_and_replanned(ended, api, monkeypatch):
    from app.engine import iteration
    fake, pid, codes, _ = ended
    sb.make_board(pid, codes["S001_SH01"])                       # SH02 kesme: SH01'in son karesi Kn_end
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH01"])
        end = sb.clean_key(sc.end_board_key)
        v = Variation(scene_id=sc.id, tier="full", wave_no=0, engine="h3", seed=1, prompt_revision=0, status="refined",
                      refined_rank=1, video_key="variations/x.mp4",
                      params={"keyframe_key": sb.clean_key(sc.board_key), "last_frame_key": "storyboard/k2_clean.jpg",
                              "last_frame_kind": "next"})
        s.add(v)
        sc.status, sc.board_status = "done", "approved"
        s.flush()
        vid = v.id
    with session_scope() as s:
        assert iteration.seam_outdated(s, s.get(Scene, codes["S001_SH01"])) == \
            "son kare kuralı değişti (sonraki çekimin karesi → çekimin son karesi)"
    queued = []
    monkeypatch.setattr("app.api.plan.build_chain", lambda pid, sid, stages, **kw: type(
        "C", (), {"apply_async": lambda self: queued.append(stages)})())
    r = api.post(f"/api/scenes/{codes['S001_SH01']}/render").json()
    assert "VISUAL" in r["stages"] and queued
    with session_scope() as s:
        assert s.get(Variation, vid).params["stale_keyframe"].startswith("son kare kuralı değişti")
        v = s.get(Variation, vid)                               # kurala uyan klip eskimez
        v.params = {**v.params, "stale_keyframe": None, "last_frame_key": end, "last_frame_kind": "end"}
        s.flush()
        assert iteration.seam_outdated(s, s.get(Scene, codes["S001_SH01"])) is None
        v.params = {k: x for k, x in v.params.items() if k != "keyframe_key"}       # ücretsiz modun yerel klibi
        s.flush()
        assert iteration.seam_outdated(s, s.get(Scene, codes["S001_SH01"])) is None


# ---------- (3) eski kare notu ----------

def test_a_clean_redraw_clears_the_previous_attempts_note(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:
        s.get(Scene, codes["S001_SH01"]).board_note = "İnceleme önerilir: Bolt benzerliği sınırda (DINOv2 0,81, eşik 0,78)"
    fake.image = png((90, 140, 200))
    sb.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH01"]).board_note is None


# ---------- (4) kazanan klibin varış skoru ----------

def test_the_winning_clips_arrival_is_shown_on_the_shot(api, mk_project):
    pid, codes = _kivilcim(api, mk_project)
    arrival = {"score": 0.9467, "dino": 0.9753, "identity": 0.88, "target": "end"}
    with session_scope() as s:
        s.add(Variation(scene_id=codes["S001_SH03"], tier="full", wave_no=0, engine="h3", seed=1, prompt_revision=0,
                        status="refined", refined_rank=1, params={"score_detail": {"arrival": arrival}}))
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH03"]["arrival"] == arrival and shots["S001_SH02"]["arrival"] is None


# =====================================================================================================================
# Kıvılcım göz durumu testi (658e9de sonrası): yarım yanık göz (image 1'deki yanık gözü "unchanged" koruyordu), "glow"
# sözcükleri, ilk karenin durumu olaydan önce, kritik durumda video yok, antende iki ampul, eski onay.
# =====================================================================================================================

import base64  # noqa: E402
import json as _json  # noqa: E402


def test_state_events_and_frame_states():
    assert state_rules.events("Bolt's eyes flicker and light up blue.") == [("eyes", "on")]
    assert state_rules.events("Bolt is still switched off, eyes dark and unlit.") == []     # durum, olay değil
    assert state_rules.frame_states([], [("eyes", "on")], "start") == [("eyes", "dark")]   # SH05 ilk karesi karanlık
    assert state_rules.frame_states([], [("eyes", "on")], "end") == [("eyes", "lit")]
    assert state_rules.frame_states([("eyes", "dark")], [], "end") == [("eyes", "dark")]


def test_off_text_drops_the_glow_words():
    assert state_rules.state_text("round glass eyes (glow blue only when switched on)", "off", ["eyes"]) == \
        "round glass eyes (dark and unlit)"
    assert state_rules.state_text("big round glass eyes that glow soft blue when on", "off", ["eyes"]) == \
        "big round glass eyes (dark and unlit)"
    assert state_rules.state_text("glowing blue eyes", "off", ["eyes"]) == "eyes (dark and unlit)"
    assert state_rules.state_text("round glass eyes (glow blue only when switched on)", "on", ["eyes"]).endswith("on)")


def test_an_off_frame_prompt_has_no_glow_words_and_says_how_to_change_image_1(api, mk_project):
    from app.models import Asset, Character
    pid, codes = _kivilcim(api, mk_project, state_off="assets/b/state_off/off.png")
    with session_scope() as s:
        bolt = s.scalars(select(Character).where(Character.project_id == pid, Character.name == "Bolt")).one()
        s.get(Asset, bolt.asset_id).description = "small round copper robot, big round glass eyes that glow soft blue when on"
    with session_scope() as s:
        sc, chars, assets, composed = (lambda x: (x[0], x[1], x[2], __import__("app.engine.iteration", fromlist=["x"])
                                                  .compose_for_scene(x[0], x[1], x[2], None, frame="start")))(
            _shot(s, codes["S001_SH03"]))
        refs, _ = sb.state_plan(s, sc, composed, chars, assets, CFG, None)
    body = composed.prompt[:composed.prompt.index("Exception for this shot")]
    assert "glow" not in body.lower() and "round glass eyes (dark and unlit)" in body
    assert {"one glowing eye", "mismatched eyes", "glowing eye"} <= {x.strip() for x in composed.negative.split(",")}
    out = sb.with_state_refs([{"name": "K3", "key": "k", "kind": "frame"},
                              {"name": "Bolt", "key": "still", "kind": "character",
                               "invariants": ["round glass eyes (glow blue only when switched on)"]}], refs, "off",
                             ["Bolt"], ["eyes"])
    text = sb.chain_prompt(composed.prompt, out, "cut", {"cast": ["Bolt"], "kept": ["Bolt"]}, MEDIUM)
    assert "replace Bolt's eyes with dark unlit glass as in image 2" in text and "unchanged" not in text
    assert "always with round glass eyes (dark and unlit)" in text and "glow blue" not in text


def test_the_first_frame_of_a_switch_on_shot_is_still_dark_and_its_end_frame_lit(api, mk_project):
    from app.engine.iteration import compose_for_scene
    pid, codes = _kivilcim(api, mk_project, state_off="assets/b/state_off/off.png")
    with session_scope() as s:
        sc, chars, assets, _ = _shot(s, codes["S001_SH05"])                 # "eyes flicker and light up blue"
        start = compose_for_scene(sc, chars, assets, None, frame="start")
        end = compose_for_scene(sc, chars, assets, None, frame="end")
        refs, probes = sb.state_plan(s, sc, start, chars, assets, CFG, None)
        assert sb.state_critical(s, sc, "off", "start", None) is True       # kendi olayı bu duruma dayanıyor
    assert state_rules.state_key(start.states) == "off" and state_rules.state_key(end.states) == "on"
    assert "The moment just before this happens:" in start.prompt and "still dark and unlit" in start.prompt
    assert refs == {"Bolt": "assets/b/state_off/off.png"} and probes[0]["key"] == "off"


def test_a_state_is_critical_when_a_later_event_depends_on_it(api, mk_project):
    pid, codes = _kivilcim(api, mk_project)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH04"]).director_note = "Bolt's eyes stay dark"
    with session_scope() as s:
        crit = {c: sb.state_critical(s, s.get(Scene, codes[c]), "off", "start", None)
                for c in ("S001_SH03", "S001_SH04", "S001_SH06")}
    assert crit == {"S001_SH03": True, "S001_SH04": True, "S001_SH06": False}


def test_a_critical_state_that_never_holds_blocks_the_video(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    probe = {"name": "Bolt", "label": "eyes dark", "key": "off", "color": "blue", "wanted": "w", "opposite": "o", "keys": []}
    monkeypatch.setattr(sb, "state_plan", lambda *a, **k: ({}, [probe]))
    monkeypatch.setattr(sb, "state_critical", lambda *a, **k: True)
    monkeypatch.setattr(scorer, "state_probe_path", lambda path, probes: {"eyes dark": {"score": 0.0, "glow": 4.5,
                                                                                         "clip": 0.2}})
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:
        k1 = s.get(Scene, codes["S001_SH01"])
        assert k1.board_status == "changes_requested" and k1.board_note.startswith(sb.REVIEW), k1.board_note
        assert "durum (eyes dark) tutmadı" in k1.board_note
        assert sb.keyframe_problem(k1, k1.board_hash) is not None           # video bu kareden başlamaz


def test_an_off_shot_sends_only_the_state_reference_for_the_character(edit, api, mk_project, tmp_path, monkeypatch):
    from PIL import Image as Img
    from app import storage
    from app.models import Asset, Character
    pid, codes = _kivilcim(api, mk_project)
    files = {}
    with session_scope() as s:
        for i, ch in enumerate(s.scalars(select(Character).where(Character.project_id == pid))):
            path = tmp_path / f"{ch.name}.png"
            Img.new("RGB", (64, 64), (200, 120 + 40 * i, 60)).save(path)
            storage.put_file(str(path), ch.reference_images[0], "image/png")
            files[ch.name] = path.read_bytes()
            if ch.name == "Bolt":
                off = tmp_path / "off.png"
                Img.new("RGB", (64, 64), (12, 12, 14)).save(off)
                key = f"assets/{ch.asset_id}/state_off/off.png"
                storage.put_file(str(off), key, "image/png")
                files["off"] = off.read_bytes()
                a = s.get(Asset, ch.asset_id)
                a.references = {**(a.references or {}), "state_off": {"key": key}}
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=None, composite=0.95, detail={"faces_seen": 0, "identity": {}}))
    sb.make_board(pid, codes["S001_SH03"])                                  # K1, K2, K3 (+ son kareler)
    import io

    def colours(run) -> list[tuple]:                            # gönderilen görseller JPEG'e çevrilir: rengine bak
        out = []
        for img in run["input"]["images"]:
            with Img.open(io.BytesIO(base64.b64decode(img["image"]))) as im:
                out.append(tuple(round(v) for v in im.convert("RGB").resize((1, 1)).getpixel((0, 0))))
        return out

    def near(c, ref, tol=12):
        return all(abs(a - b) <= tol for a, b in zip(c, ref))

    with Img.open(io.BytesIO(files["Bolt"])) as im:
        still = im.convert("RGB").getpixel((0, 0))
    off_runs = [r for r in fake_runs(edit) if "replace Bolt's eyes with dark unlit glass" in
                r["input"]["workflow"]["170:151"]["inputs"]["prompt"]]
    assert len(off_runs) >= 2                                               # SH03 ilk karesi ve son karesi
    for r in off_runs:
        sent = colours(r)
        assert any(near(c, (12, 12, 14)) for c in sent) and not any(near(c, still) for c in sent)   # yalnızca durum görseli
        prompt = r["input"]["workflow"]["170:151"]["inputs"]["prompt"]
        assert "glow blue" not in prompt and "unchanged" not in prompt and "characters' state as in image 1" not in prompt
    with session_scope() as s:                                              # iş girdisi kayıtlı: yuva -> anahtar
        steps = s.execute(__import__("sqlalchemy").text(
            "SELECT details->'steps' FROM service_calls WHERE scene_id = :s AND label LIKE 'h3:storyboard%' "
            "ORDER BY started_at DESC LIMIT 1"), {"s": codes["S001_SH03"]}).scalar()
    slots = steps[0]["reference_keys"]
    assert any(x["key"].endswith("/state_off/off.png") for x in slots) and not any("/still/" in x["key"] and
                                                                                  x["name"] == "Bolt" for x in slots)


def fake_runs(fake):
    return [r for r in fake.runs if "170:151" in r["input"]["workflow"]]


def test_counted_parts_are_flagged_for_a_human_look(api, mk_project):
    from app.core.state import load_assets, load_characters
    from app.models import Asset, Character
    pid, codes = _kivilcim(api, mk_project)
    with session_scope() as s:
        bolt = s.scalars(select(Character).where(Character.project_id == pid, Character.name == "Bolt")).one()
        a = s.get(Asset, bolt.asset_id)
        a.invariants = [*a.invariants, "exactly one bent antenna with a small bulb"]
    with session_scope() as s:
        notes = sb.count_notes(load_characters(s, codes["S001_SH03"]), load_assets(s, codes["S001_SH03"]))
    assert notes == ["Bolt: “exactly one bent antenna with a small bulb” sayılamıyor, gözle kontrol edin"]
    from app.style.composer import invariant_negatives
    assert {"two bulbs", "double bulb"} <= set(invariant_negatives(["exactly one bent antenna with a small bulb"]))


def test_a_redrawn_frame_needs_approval_again(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).board_status = "approved"
    fake.image = png((70, 200, 120))
    sb.make_board(pid, codes["S001_SH02"], standalone=True, explicit=True)  # K2 değişti: K3 eskir
    sb.make_board(pid, codes["S001_SH03"])                                  # K3 zincirle yeniden çizilir
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH03"]).board_status == "pending"   # eski onay başka bir kareyi onaylamıştı


# =====================================================================================================================
# Kıvılcım izleme bulgusu: kesmede kamera değişmiyor (jump cut) ve çekimler arası renk farkı.
# =====================================================================================================================

from app.post import color as colour  # noqa: E402
from tests.test_grade import produced  # noqa: E402,F401


# ---------- (1a) kesme kamerası: 30° kuralı ----------

def test_cut_cameras_follow_the_thirty_degree_rule():
    wide = {"shot": "wide", "movement": "pan", "angle": "eye", "lens_mm": 24}
    assert chain_rules.plan_cut_camera(wide, MEDIUM, 1) is None                  # geniş -> orta: bir ölçek adımı yeter
    sh03 = chain_rules.plan_cut_camera(MEDIUM, MEDIUM, 1)                        # orta -> orta: jump cut
    assert (sh03["shot"], sh03["side"], sh03["angle"], sh03["lens_mm"]) == ("close_up", -35, "low", 50)
    assert chain_rules.cut_differs(MEDIUM, sh03)
    two = chain_rules.plan_cut_camera({"shot": "medium", "side": -40}, {**MEDIUM, "side": -40}, 2)
    assert two["side"] == 40 and chain_rules.cut_differs({"shot": "medium", "side": -40}, two)   # ters açı
    assert chain_rules.plan_cut_camera(MEDIUM, {**MEDIUM, "manual": True}, 1) is None      # yönetmenin kamerası


def test_the_kivilcim_cuts_get_distinct_cameras(api, mk_project):
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.hermes.ingest import ingest
    pid = mk_project(5.0)
    ingest(pid, KIVILCIM, auto_music=False)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    cams = [shots[c]["camera_effective"] for c in sorted(shots)]
    for a, b, code in zip(cams, cams[1:], sorted(shots)[1:]):
        if shots[code]["transition"] == "cut":
            assert chain_rules.cut_differs(a, b), (code, a, b)                   # her kesmede kamera belirgin değişir
    planned = [c for c in sorted(shots) if shots[c]["camera_plan"]]
    assert planned                                                               # aynı ölçekli kesmeye kamera atandı
    with session_scope() as s:                                                   # kesme kamerası görsel özete girer
        sc = s.get(Scene, shots[planned[0]]["id"])
        chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
        with_plan = visual_fingerprint(sc, chars, assets, None)
        sc.camera_plan = None
        assert visual_fingerprint(sc, chars, assets, None) != with_plan
    r = api.patch(f"/api/scenes/{shots[planned[0]]['id']}", json={"camera": MEDIUM})   # elle: atanmaz
    assert r.status_code == 200
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots[planned[0]]["camera_plan"] is None and shots[planned[0]]["camera"]["manual"] is True


# ---------- (1b) kesme karesi geniş plandan, kamera açıkça ----------

def test_a_cut_frame_starts_from_the_wide_anchor_and_moves_the_camera(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH03"])
    k2 = _prompts(fake)[0]                                                       # K2: SH01 geniş -> SH02 orta (yaklaşır)
    assert k2.startswith("Image 1 is the previous shot (S001_SH01); this shot cuts to a new camera position")
    assert "Move the camera closer" in k2 and "must clearly differ from image 1" in k2
    text = sb.camera_move_text({"shot": "wide", "side": 0}, {"shot": "close_up", "side": -35, "angle": "low",
                                                              "lens_mm": 50}, ["Bolt"])
    assert text.startswith("Move the camera closer and 35° to the left, slightly low angle: a close-up of Bolt, 50 mm lens.")


def test_a_location_still_is_the_cut_anchor_when_there_is_one(chained, tmp_path):  # noqa: F811
    from PIL import Image as Img
    from app import storage
    from app.models import Asset
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH01"])
    Img.new("RGB", (64, 36), (90, 60, 40)).save(tmp_path / "loc.png")
    storage.put_file(str(tmp_path / "loc.png"), "assets/loc/still/loc.png", "image/png")
    with session_scope() as s:
        loc = s.scalars(select(Asset).where(Asset.project_id == pid, Asset.type == "location")).first()
        loc.references = {"still": {"key": "assets/loc/still/loc.png"}}
        sc = s.get(Scene, codes["S001_SH02"])
        prev = s.get(Scene, codes["S001_SH01"])
        prev.camera_plan = {"shot": "close_up", "side": 0}                      # SH02 ondan geniş: kamera uzaklaşır
        anchor = sb.cut_anchor(s, sc, prev, [loc], None)
    assert anchor["key"] == "assets/loc/still/loc.png" and anchor["cast"] == [] and anchor["camera"]["shot"] == "wide"
    assert "must not repeat the previous shot's viewpoint (S001_SH01)" in anchor["avoid"]


# ---------- (1c) jump cut kapısı ----------

def test_a_cut_frame_with_the_same_camera_is_redrawn(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    results = [{"same": True, "ratio": 0.9, "scale": 1.0, "shift": 0.01}, {"same": False, "ratio": 0.03}]
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: results.pop(0) if results else {"same": False})
    sb.make_board(pid, codes["S001_SH02"])
    assert len([p for p in _prompts(fake) if "must clearly differ from image 1" in p]) == 2
    with session_scope() as s:
        assert "jump cut" not in (s.get(Scene, codes["S001_SH02"]).board_note or "")


def test_a_cut_frame_that_keeps_the_camera_is_flagged_as_a_jump_cut(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: {"same": True, "ratio": 0.9, "scale": 1.0,
                                                                         "shift": 0.01})
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        note = s.get(Scene, codes["S001_SH02"]).board_note
    assert "jump cut riski: kamera önceki çekimle aynı (iç oran 0,90, ölçek 1,00)" in note


def test_the_homography_tells_a_held_camera_from_a_moved_one():
    pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image as Img
    from app.quality import camera
    rng = np.random.default_rng(3)
    base = Img.fromarray(np.kron(rng.integers(0, 255, (88, 160, 3), dtype=np.uint8), np.ones((8, 8, 1), np.uint8)))
    assert camera.same_camera(base, base.copy())["same"] is True                 # jump cut: kamera aynı
    w, h = base.size
    zoom = base.crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8))).resize((w, h))
    moved = camera.same_camera(base, zoom)
    assert moved["same"] is False                                                 # yakınlaşma: kamera değişti
    assert camera.same_camera(base, base.transpose(Img.FLIP_LEFT_RIGHT))["same"] is False   # ters açı


# ---------- (1d) karakter girişi ----------

def test_an_entering_character_can_walk_in_instead_of_a_cut(api, mk_project):
    from app.engine.iteration import compose_for_scene
    pid, codes = _kivilcim(api, mk_project)
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH04"]["entering"] == ["Mila"] and shots["S001_SH04"]["entry_options"] == ["cut", "walk_in"]
    assert shots["S001_SH04"]["entry_mode"] is None                               # varsayılan: kesme
    assert api.patch(f"/api/scenes/{codes['S001_SH04']}", json={"entry_mode": "walk_in"}).status_code == 200
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH04"]["transition"] == "continuous" and shots["S001_SH04"]["camera_plan"] is None
    with session_scope() as s:
        sc, chars, assets, _ = _shot(s, codes["S001_SH04"])
        start = compose_for_scene(sc, chars, assets, None, frame="start", entering=["Mila"])
        video = compose_for_scene(sc, chars, assets, None, entering=["Mila"])
    assert "Mila is not yet in view in this first frame" in start.prompt
    assert "Mila walks into the frame from the right" in video.prompt
    api.patch(f"/api/scenes/{codes['S001_SH04']}", json={"entry_mode": "cut"})
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert shots["S001_SH04"]["transition"] == "cut"


# ---------- (1e) kamera hareketi ----------

def test_camera_movement_is_spelled_out_and_checked_in_the_clip(monkeypatch, tmp_path):
    from app.style.composer import camera_text
    from tests.media_utils import make_video
    assert "Camera movement: the camera slowly pans horizontally across the scene from left to right." in \
        camera_text({"shot": "wide", "movement": "pan", "lens_mm": 24})
    assert "Camera movement" not in camera_text(MEDIUM)
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(scorer, "score_video", lambda path, target: scorer.Result(
        clip=0.95, face=None, composite=0.95, detail={"faces_seen": 0, "identity": {}}))
    monkeypatch.setattr(scorer, "clip_motion", lambda path, movement: {"dx": 0.0, "dy": 0.0, "zoom": 1.0,
                                                                         "expected": movement, "seen": False})
    clip = make_video(tmp_path / "c.mp4", seconds=2.0, size="160x90", fps=24)
    score = gate.score_video(clip, 1, CFG, scorer.Target(movement="pan"))
    assert score.passed and score.detail["camera_motion"]["seen"] is False
    assert "kamera hareketi (pan) görülmedi" in score.detail["review"]


# ---------- (2) renk ----------

def test_lab_background_stats_flag_a_brightness_jump_and_ignore_the_centre():
    import numpy as np
    dark = np.full((90, 160, 3), 40, np.uint8)
    bright = np.full((90, 160, 3), 60, np.uint8)
    d = colour.difference(colour.stats(dark), colour.stats(bright))
    assert d["L"] > colour.JUMP_DL and colour.is_jump(d)
    centre = dark.copy()
    centre[30:, 50:130] = 230                                                     # karakter bölgesinde parlak cilt/lamba
    assert colour.difference(colour.stats(dark), colour.stats(centre))["E"] < 0.5   # karakter dışı ölçü etkilenmez


def test_colour_transfer_moves_a_frame_toward_its_reference():
    import numpy as np
    ref = np.full((90, 160, 3), (40, 34, 30), np.uint8)
    frame = np.full((90, 160, 3), (52, 45, 40), np.uint8)              # ΔL ~4: aktarım sınırı (8) içinde
    target = colour.stats(ref)
    before = colour.difference(target, colour.stats(frame))
    after = colour.difference(target, colour.stats(colour.transfer(frame, target)))
    assert abs(after["L"]) < 1 and after["E"] < before["E"] / 4


def test_a_drawn_frame_is_colour_matched_to_the_previous_frame(tmp_path, s3):
    import numpy as np
    from PIL import Image as Img
    from app import storage
    Img.fromarray(np.full((90, 160, 3), (40, 34, 30), np.uint8)).save(tmp_path / "ref.jpg")
    storage.put_file(str(tmp_path / "ref.jpg"), "storyboard/t/ref_clean.jpg", "image/jpeg")
    Img.fromarray(np.full((90, 160, 3), (54, 47, 41), np.uint8)).save(tmp_path / "clean.jpg")
    out = sb.match_colour(str(tmp_path / "clean.jpg"), "storyboard/t/ref_clean.jpg", str(tmp_path))
    assert abs(out["after"]["L"]) < 1.5 < abs(out["before"]["L"])


def test_auto_match_grades_the_off_shot_and_can_be_undone(api, produced, monkeypatch):
    from app.quality import consistency
    from app.worker import tasks
    pid, ids = produced
    monkeypatch.setattr(consistency, "frame_stats", lambda img: {        # yalnızca parlaklık (hızlı, deterministik)
        "lab": {"L": float(img.mean()) / 2.55, "a": 0.0, "b": 0.0, "sdL": 10.0, "sda": 1.0, "sdb": 1.0}})
    with session_scope() as s:                                                    # SH02 yönetmen ayarı değil
        assert not s.get(Scene, ids["S001_SH02"]).grade
    changed = consistency.auto_match(pid)
    with session_scope() as s:
        graded = [s.get(Scene, sid) for sid in changed]
        manual = s.get(Scene, ids["S001_SH01"])
    if not changed:
        pytest.skip("iki prosedürel çekim zaten referansa yakın")
    sc = graded[0]
    assert sc.grade_auto["applied"] and sc.grade_auto["reference"] in ("S001_SH01", "S001_SH02")
    monkeypatch.setattr(tasks, "apply_auto_match", lambda pid, sids: [])
    r = api.post(f"/api/scenes/{sc.id}/grade/auto-undo")
    assert r.status_code == 200
    with session_scope() as s:
        row = s.get(Scene, sc.id)
        assert row.grade_auto["disabled"] is True and row.grade == (sc.grade_auto.get("previous") or {})
    assert consistency.auto_match(pid) == [] or str(sc.id) not in consistency.auto_match(pid)    # geri alınan dokunulmaz


# =====================================================================================================================
# Kıvılcım kamera ve renk testi (c64fd79 sonrası): SH03 kamerası, jump cut, durum kapısının yanlış negatifi, zincir durması,
# eski onay, elle kabulün şerit akışı.
# =====================================================================================================================

import pathlib as _pathlib  # noqa: E402

KIVILCIM_FRAMES = _pathlib.Path(__file__).parent / "data" / "kivilcim"


def _frame(name):
    from PIL import Image as Img
    with Img.open(KIVILCIM_FRAMES / name) as im:
        return im.convert("RGB")


def _centre(im):                                       # karakter kırpımının yaklaşığı: ortadaki yarım boy
    w, h = im.size
    return im.crop((w // 4, h // 4, w // 4 + w // 2, h // 4 + h // 2))


# ---------- kalibrasyon örnekleri (gerçek kareler) ----------

def test_the_sh02_to_sh03_cut_is_a_jump_cut_on_the_real_frames():
    pytest.importorskip("cv2")
    from app.quality import camera
    held = camera.same_camera(_frame("sh02_end.jpg"), _frame("sh03_dark_new_angle.jpg"))
    assert held["same"] is True and held["ratio"] > 0.8                          # başarısız örnek: kamera değişmedi


def test_dark_eyes_on_a_new_angle_pass_and_lit_eyes_fail_on_the_real_frames():
    pytest.importorskip("cv2")
    dark = scorer.glow_fraction(_centre(_frame("sh03_dark_new_angle.jpg")), "blue")
    lit = scorer.glow_fraction(_centre(_frame("sh04_lit.jpg")), "blue")
    one = scorer.glow_fraction(_centre(_frame("sh03_one_lit.jpg")), "blue")
    assert dark < scorer.GLOW_REF / 2 < scorer.GLOW_REF < lit                   # doğru pozitif ve doğru negatif
    assert one > scorer.GLOW_REF / 2                                             # tek göz yanık: "kapalı" değil


def test_small_reflections_are_not_counted_as_a_lit_eye():
    pytest.importorskip("cv2")
    import numpy as np
    mask = np.zeros((352, 640), bool)
    for x, y in ((100, 80), (300, 120), (500, 60)):                              # cam gözde küçük yansıma parıltıları
        mask[y:y + 5, x:x + 5] = True
    mask[150:290, 200:203] = True                                                # yağmur çizgisi
    assert scorer.lit_blobs(mask) == 0
    mask[200:226, 400:426] = True                                                # yanan göz (676 px)
    assert scorer.lit_blobs(mask) > scorer.BLOB_MIN


def test_the_character_area_follows_the_dino_patch_map(monkeypatch):
    import numpy as np
    from PIL import Image as Img
    fg = np.eye(4)[[0]]
    bg = np.eye(4)[[1]]
    frame = np.tile(np.eye(4)[1], (10, 10, 1)).astype(float)                    # arka plan
    frame[6:10, 4:7] = np.eye(4)[0]                                              # karakter: 12 yama / 100
    monkeypatch.setattr(scorer, "reference_patches", lambda m, key: (fg, bg))
    monkeypatch.setattr(scorer, "dino_patches", lambda m, image, short=224: frame)
    assert scorer.patch_coverage(object(), Img.new("RGB", (64, 36)), "k") == 0.12
    assert gate.framing_check(0.12, "close_up")["ok"] is False                   # SH03: küçük Bolt yakın plan değil
    assert gate.framing_check(0.4, "close_up")["ok"] is True


# ---------- kesme kaynağı: önceki çekime göre ----------

def test_a_closer_cut_starts_from_the_previous_shot_and_a_wider_one_avoids_it(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        sc, prev = s.get(Scene, codes["S001_SH02"]), s.get(Scene, codes["S001_SH01"])
        sc.camera_plan = {"shot": "close_up", "side": -35, "angle": "low", "lens_mm": 50}
        closer = sb.cut_anchor(s, sc, prev, [], None)
        sc.camera_plan, prev.camera_plan = {"shot": "medium", "side": 40}, {"shot": "close_up", "side": -35}
        wider = sb.cut_anchor(s, sc, prev, [], None)
    assert closer["name"] == "S001_SH01" and closer["text"].startswith("Image 1 is the previous shot (S001_SH01)")
    assert wider["avoid"] == "" or "must not repeat the previous shot's viewpoint" in wider["avoid"]
    text = sb.camera_move_text({"shot": "medium", "side": 0}, {"shot": "close_up", "side": -35, "angle": "low",
                                                               "lens_mm": 50}, ["Bolt"])
    assert text.startswith("Move the camera closer and 35° to the left, slightly low angle: a close-up of Bolt")


# ---------- kapı notları kaybolmasın ----------

def test_a_critical_failure_keeps_the_jump_cut_and_framing_findings(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    probe = {"name": "Bolt", "label": "eyes dark", "key": "off", "color": "blue", "wanted": "w", "opposite": "o", "keys": []}
    monkeypatch.setattr(sb, "state_plan", lambda *a, **k: ({}, [probe]))
    monkeypatch.setattr(sb, "state_critical", lambda *a, **k: True)
    monkeypatch.setattr(scorer, "state_probe_path", lambda path, probes: {"eyes dark": {"score": 0.0, "glow": 4.5,
                                                                                         "clip": 0.2}})
    monkeypatch.setattr(scorer, "camera_change", lambda previous, path: {"same": True, "ratio": 0.9, "scale": 1.0,
                                                                         "shift": 0.0})
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.02)            # orta plan için çok küçük
    monkeypatch.setattr(sb, "state_critical", lambda s, scene, key, frame, style: scene.scene_code == "S001_SH02")
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        note = s.get(Scene, codes["S001_SH02"]).board_note
    assert note.startswith(sb.REVIEW) and "durum (eyes dark) tutmadı" in note
    assert "jump cut riski" in note and "kadraj istenen ölçekte değil" in note


def test_colour_matching_runs_before_the_gates(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    order = []
    real_match, real_score = sb.match_colour, gate.score_frame
    monkeypatch.setattr(sb, "match_colour", lambda *a: order.append("colour") or real_match(*a))
    monkeypatch.setattr(gate, "score_frame", lambda *a, **k: order.append("gate") or real_score(*a, **k))
    sb.make_board(pid, codes["S001_SH02"])
    first_edit = order.index("colour")
    assert order[first_edit + 1] == "gate"                                       # kapı aktarılmış kareyi ölçer


# ---------- zincir durunca hiçbir sonraki halka çizilmez ----------

def test_no_later_link_is_drawn_while_the_chain_waits(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH04"])                                       # K1..K4
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        k2.board_status, k2.board_note = "changes_requested", "Kare inceleme gerekiyor: x"
        s.get(Scene, codes["S001_SH03"]).board_parent = "eski"                   # K3 eski (K2'den çizilecek)
    before = len(fake.runs)
    sb.make_board(pid, codes["S001_SH04"])
    assert len(fake.runs) == before                                              # K3 de K4 de çizilmedi


def test_a_link_whose_previous_frame_is_stale_is_not_drawn_without_the_chain(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).board_hash = "eski"                     # K3 bu görsel özet için değil
        s.get(Scene, codes["S001_SH04"]).board_key = None
    before = len(fake.runs)
    sb._make_board(pid, codes["S001_SH04"])                                      # tek kare çizimi de beklemeli
    assert len(fake.runs) == before
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH04"]).board_key is None


# ---------- önceki halka değişince sonrakiler onay bekler ----------

def test_later_links_go_back_to_pending_when_an_earlier_frame_changes(chained):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH04"])
    with session_scope() as s:
        for c in ("S001_SH03", "S001_SH04"):
            sc = s.get(Scene, codes[c])
            sc.board_status, sc.board_note = "approved", "İnceleme önerilir: eski deneme"
    fake.image = png((20, 120, 200))
    sb.make_board(pid, codes["S001_SH02"], standalone=True, explicit=True)
    with session_scope() as s:
        for c in ("S001_SH03", "S001_SH04"):
            sc = s.get(Scene, codes[c])
            assert (sc.board_status, sc.board_note) == ("pending", None), c


# ---------- elle kabul: şerit akışında zincir sürer, video başlamaz ----------

def test_a_manual_accept_in_the_strip_flow_continues_the_chain_not_the_video(chained, api, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        k2.board_status, k2.board_note = "changes_requested", "Kare inceleme gerekiyor: durum (eyes dark) tutmadı"
    queued = []
    monkeypatch.setattr("app.worker.tasks.storyboard_scene.si",
                        lambda **kw: type("C", (), {"apply_async": lambda self: queued.append(kw)})())
    monkeypatch.setattr("app.api.storyboard.dispatch_result", lambda *a, **k: pytest.fail("video başlatılmamalı"))
    r = api.post(f"/api/scenes/{codes['S001_SH02']}/board/accept").json()
    assert r["board_status"] == "approved" and r["stages"] == []
    assert r["chain_continues"] == ["S001_SH03", "S001_SH04"] and queued[0]["scene_id"] == codes["S001_SH04"]
