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
    from app.engine import storyboard
    from app.engine.iteration import CHAIN_STALE
    fake, pid, codes, _ = chained
    storyboard.make_board(pid, codes["S001_SH04"])               # K1..K4
    c1, c2, c3 = _clip(codes["S001_SH01"]), _clip(codes["S001_SH02"]), _clip(codes["S001_SH03"])
    with session_scope() as s:
        for code in ("S001_SH01", "S001_SH03"):
            s.get(Scene, codes[code]).status = "done"
        s.get(Scene, codes["S001_SH04"]).status = "generating"   # SH04'ün videosu üretiliyor
    runpod_h3.remember("jk-sh04", "job-sh04", pid, "fl2va", codes["S001_SH04"])
    fake.jobs["job-sh04"] = {"id": "job-sh04", "polls": 0, "failed": False, "kind": "video"}
    fake.image = png((30, 160, 90))
    storyboard.make_board(pid, codes["S001_SH02"], standalone=True, explicit=True)   # K2 yeniden çizildi
    with session_scope() as s:
        v1, v2, v3 = (s.get(Variation, x) for x in (c1, c2, c3))
        sh1, sh3 = s.get(Scene, codes["S001_SH01"]), s.get(Scene, codes["S001_SH03"])
        assert all(v.params.get("stale_keyframe") == "S001_SH02 karesi değişti" for v in (v1, v2, v3))
        assert sh1.status == sh3.status == "needs_review" and sh1.visual_hash is None    # kurguda kullanılmaz
        assert "eski kareden" in sh3.last_error
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
    _clip(codes["S001_SH01"]), _clip(codes["S001_SH03"])
    r = api.get(f"/api/scenes/{codes['S001_SH02']}/chain-impact").json()
    assert r["frame_codes"] == ["S001_SH02", "S001_SH03", "S001_SH04"] and r["clip_codes"] == ["S001_SH01", "S001_SH03"]
    assert r["frames"] == 3 and r["clips"] == 2 and r["estimate_usd"] > 0


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
