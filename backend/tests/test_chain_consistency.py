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
    assert CFG["continuity_threshold"] == 0.70 and CFG["cut_continuity_threshold"] == 0.60


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
    assert len(end) == 1 and "after its action is completed" in end[0] and "wide shot" in end[0]
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
    ramp = np.tile(np.linspace(0, 255, 160, dtype=np.uint8), (90, 1))
    left = np.stack([ramp] * 3, axis=-1)
    right = left[:, ::-1]
    assert consistency.content_similarity(left, left) == pytest.approx(1.0, abs=1e-3)
    assert consistency.content_similarity(left, right) < consistency.SEAM_CONTENT_MIN       # aynı renk, başka görüntü
    assert consistency.compare(consistency.frame_stats(left), consistency.frame_stats(right))["score"] < 0.2


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
    assert len([p for p in _prompts(fake) if "Reframe the camera" in p]) == 2
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        assert k2.board_status == "pending" and not (k2.board_note or "").startswith("İnceleme")


def test_a_cut_frame_that_never_matches_the_framing_passes_with_a_review_note(chained, monkeypatch):  # noqa: F811
    fake, pid, codes, _ = chained
    monkeypatch.setattr(scorer, "framing", lambda path, target: 0.95)
    sb.make_board(pid, codes["S001_SH02"])
    with session_scope() as s:
        k2 = s.get(Scene, codes["S001_SH02"])
        assert k2.board_status == "pending" and "kadraj istenen ölçekte değil (orta plan beklendi" in k2.board_note
    assert len([p for p in _prompts(fake) if "Reframe the camera" in p]) == 3


# ---------- (6) durum sözcükleri, süre, GPU havuzu ----------

def test_state_words_go_into_the_prompt_and_their_opposites_into_the_negative():
    from app.style.composer import compose_prompt
    c = compose_prompt({"prompt": "Bolt sits lifeless on the workbench with its eyes dark.", "lang": "en",
                        "characters": ["Bolt"]}, [], None)
    assert "State: eyes dark." in c.prompt and "glowing eyes" in c.negative and "lit eyes" in c.negative
    turkish = compose_prompt({"prompt": "Boş yol.", "lang": "tr", "characters": []}, [], None)
    assert "State:" not in turkish.prompt                      # Türkçe (çevrilmemiş) metinde uydurma yok


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
