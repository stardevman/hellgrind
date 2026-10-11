"""Kıvılcım testi (d9ed51b/a460328 sonrası): kapı kadraja duyarlı (geniş planda küçük Bolt, yakın planda stil metni),
kırp + iyileştir karesinde sınırdaki benzerlik, kare değişince eski test klipleri, yönetmen seçimi, kırp + iyileştir
çekiminin son karesi ve kamera tarifi. Ağ yok; kalibrasyon değerleri canlı Kıvılcım kliplerinden."""
import pytest
from PIL import Image
from sqlalchemy import select

from app import storage
from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.db import session_scope
from app.engine import storyboard as sb
from app.engine.iteration import (_prepare, conditioning, current_conditioning, prompt_camera, run_override_full,
                                  run_tiered_generation)
from app.engine.providers import LocalProvider
from app.models import Scene, Variation
from app.quality import gate, scorer
from tests.test_chain import chained  # noqa: F401
from tests.test_qwen_edit import edit, edit_runs  # noqa: F401
from tests.test_runpod import runpod  # noqa: F401

CFG = dict(DEFAULT_PROJECT_SETTINGS)
BOLT = scorer.Target(looks=(("Bolt", ("assets/b/still/b.png",)),))


# ---------- (1) geniş planda küçük karakter ----------

@pytest.mark.parametrize("clip,dino,area", [
    ("SH04 5de7a7c1", 0.761, 0.071), ("SH04 ca8fb68f", 0.703, 0.076), ("SH06 8abc2093", 0.749, 0.055),
    ("SH06 8e889dad", 0.722, 0.042)])
def test_a_small_character_is_not_eliminated_by_the_dino_likeness(clip, dino, area):
    detail = {"look": {"Bolt": 0.75}, "look_dino": {"Bolt": dino}, "look_area": {"Bolt": area}}
    assert gate.identity_anomalies(detail, BOLT, CFG) == [], clip              # gözle iyi klipler geçer
    assert any("karede küçük" in n for n in gate.identity_review(detail, BOLT, CFG))


def test_a_large_character_still_needs_the_dino_likeness_and_a_small_one_still_needs_openclip():
    big = {"look": {"Bolt": 0.75}, "look_dino": {"Bolt": 0.70}, "look_area": {"Bolt": 0.29}}
    assert gate.identity_anomalies(big, BOLT, CFG)[0]["type"] == "low_likeness"
    unlike = {"look": {"Bolt": 0.66}, "look_dino": {"Bolt": 0.74}, "look_area": {"Bolt": 0.05}}   # benzemeyen kare
    assert gate.identity_anomalies(unlike, BOLT, CFG)[0]["look"] == {"Bolt": 0.66}


# ---------- (2) yakın planda stil ----------

class _FakeModels:
    def encode_images(self, images):
        import numpy as np
        return np.array([[1.0, 0.0] for _ in images])


def test_close_up_style_is_measured_against_the_approved_frame_and_sees_the_palette():
    warm = Image.new("RGB", (64, 36), (150, 80, 40))
    same = scorer.frame_style(_FakeModels(), _FakeModels().encode_images([warm]), [warm], warm)
    assert same["score"] == 1.0 and same["delta_e"] < 1
    grey = warm.convert("L").convert("RGB")                                    # benzerlik rengi görmez, palet görür
    off = scorer.frame_style(_FakeModels(), _FakeModels().encode_images([grey]), [grey], warm)
    assert off["delta_e"] > scorer.STYLE_DE[1] and off["score"] == 0.0


def test_a_close_up_clip_is_scored_against_its_own_first_frame(mk_project, monkeypatch):
    from tests.test_cut_methods import _kivilcim
    pid, codes = _kivilcim(mk_project)
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.camera_plan["shot"] == "close_up"
    sb.make_board(pid, codes["S001_SH03"])
    ctx = _prepare(pid, codes["S001_SH03"], "run")
    assert ctx.render["keyframe_key"] and ctx.target.style_frame == ctx.render["keyframe_key"]
    wide = _prepare(pid, codes["S001_SH01"], "run2")
    assert wide.target.style_frame is None                                       # geniş plan: stil metni


# ---------- (3) kırp + iyileştir karesinde sınırdaki benzerlik ----------

def test_a_borderline_likeness_on_a_cut_in_frame_becomes_a_review_note():
    anomalies = [{"type": "low_likeness", "look_dino": {"Bolt": 0.7795}, "threshold": 0.78}]
    kept, notes = gate.soften_likeness(anomalies)
    assert kept == [] and notes == ["Bolt benzerliği sınırda (DINOv2 0,78, eşik 0,78; kırp + iyileştir)"]
    far = [{"type": "low_likeness", "look_dino": {"Bolt": 0.70}, "threshold": 0.78}]
    assert gate.soften_likeness(far) == (far, [])


def test_the_sh03_cut_in_frame_passes_with_a_review_instead_of_failing(edit, mk_project, tmp_path,  # noqa: F811
                                                                        monkeypatch):
    from app.models import Character
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9},
                                                     "look": {"Bolt": 0.83}, "look_dino": {"Bolt": 0.7795}}))
    pid, codes = _kivilcim(mk_project, chain_end_frame=False)
    with session_scope() as s:
        for ch in s.scalars(select(Character).where(Character.project_id == pid, Character.name == "Bolt")):
            ch.kind = "non_human"
        s.get(Scene, codes["S001_SH03"]).cut_method = "cut_in"
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    sb.make_board(pid, codes["S001_SH03"])
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        assert sc.board_status == "pending", sc.board_note                       # kritik hata değil
        assert "Bolt benzerliği sınırda (DINOv2 0,78, eşik 0,78; kırp + iyileştir)" in sc.board_note
        assert sc.board_method == "cut_in"


# ---------- (4) kare değişince eski test klipleri ----------

def test_the_conditioning_follows_the_frame_content_not_its_key():
    class S:
        keyframe_key = keyframe_hash = None
        board_sha, end_board_sha = "a" * 64, "e" * 64
    one = conditioning(None, S, "storyboard/x/f_clean.jpg", "storyboard/x/f_end.jpg", "end")
    S.board_sha = "b" * 64                                                        # aynı anahtar, yeni içerik
    assert conditioning(None, S, "storyboard/x/f_clean.jpg", "storyboard/x/f_end.jpg", "end") != one
    assert conditioning(None, S, None, None, None) is None


def test_a_redrawn_frame_makes_new_clips_and_resets_the_correction_rounds(mk_project, monkeypatch):
    from tests.test_cut_methods import _kivilcim
    pid, codes = _kivilcim(mk_project)
    sid = codes["S001_SH01"]
    sb.make_board(pid, sid)
    with session_scope() as s:
        s.get(Scene, sid).board_status = "approved"
    assert run_tiered_generation(pid, sid) == "refined"
    first = {v.seed for v in _rows(sid)}
    with session_scope() as s:                                                    # kare yeniden çizildi (yeni içerik)
        sc = s.get(Scene, sid)
        sc.board_sha, sc.visual_hash, sc.correction_count = "f" * 64, None, 5
    calls = []
    real = LocalProvider.generate
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req.seed) or real(self, req))
    assert run_tiered_generation(pid, sid) == "refined"
    assert calls and set(calls) <= first                                          # aynı seed'ler, ama yeniden üretildi
    with session_scope() as s:
        cond = current_conditioning(s, s.get(Scene, sid))
    assert all((v.params or {}).get("cond") == cond for v in _rows(sid) if (v.params or {}).get("run") and
               v.status in ("passed", "refined") and v.created_at == max(x.created_at for x in _rows(sid)))


def test_a_new_frame_resets_the_correction_counter(chained):  # noqa: F811
    from tests.test_chain import png
    fake, pid, codes, _ = chained
    sb.make_board(pid, codes["S001_SH01"])
    with session_scope() as s:
        s.get(Scene, codes["S001_SH01"]).correction_count = 4
    fake.image = png((30, 160, 90))
    sb.make_board(pid, codes["S001_SH01"], standalone=True, explicit=True)
    with session_scope() as s:
        assert s.get(Scene, codes["S001_SH01"]).correction_count == 0


def _rows(sid):
    with session_scope() as s:
        rows = list(s.scalars(select(Variation).where(Variation.scene_id == sid)))
        s.expunge_all()
    return rows


# ---------- (5) yönetmen seçimi ----------

@pytest.fixture
def produced(api, mk_project):
    """Ücretsiz modda üretilmiş SH01 (kare + klipler) ve bugünkü koşulda kapıdan kalmış bir aday."""
    from tests.test_cut_methods import _kivilcim
    pid, codes = _kivilcim(mk_project)
    sid = codes["S001_SH01"]
    sb.make_board(pid, sid)
    with session_scope() as s:
        s.get(Scene, sid).board_status = "approved"
    assert run_tiered_generation(pid, sid) == "refined"
    winner = next(v for v in _rows(sid) if v.refined_rank == 1)
    key = f"variations/{sid}/rejected.mp4"
    storage.copy(winner.video_key, f"quarantine/{key}")
    with session_scope() as s:
        cond = current_conditioning(s, s.get(Scene, sid))
        v = Variation(scene_id=sid, tier="probe", wave_no=0, engine=winner.engine, seed=777, prompt_revision=0,
                      status="purged", composite_score=0.6, anomalies=[{"type": "low_clip"}],
                      params={"resolution": "480p", "cond": cond, "purge_reason": "quality_gate",
                              "quarantine_key": f"quarantine/{key}", "run": "old"})
        s.add(v)
        s.flush()
        vid = str(v.id)
    return pid, sid, vid


def test_the_director_can_pick_a_candidate_the_gate_eliminated(api, produced):
    pid, sid, vid = produced
    assert api.post(f"/api/scenes/{sid}/promote/{vid}").status_code == 404           # havuzda değil, geçersiz kılma yok
    shots = {x["id"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert next(v for v in shots[sid]["variations"] if v["id"] == vid)["overridable"] is True
    r = api.post(f"/api/scenes/{sid}/promote/{vid}?override=true")
    assert r.status_code == 200, r.text
    assert r.json()["override"] == "promote" and "LIPSYNC" in r.json()["stages"] and "VISUAL" not in r.json()["stages"]
    with session_scope() as s:
        v = s.get(Variation, vid)
        assert (v.refined_rank, v.status) == (1, "refined") and v.params["director_override"]["was"] == "purged"
        assert storage.exists(v.video_key)                                         # karantinadan geri alındı
        assert [x.refined_rank for x in s.scalars(select(Variation).where(
            Variation.scene_id == sid, Variation.refined_rank.is_not(None)).order_by(Variation.refined_rank))][:2] == [1, 2]


def test_a_candidate_from_an_old_frame_cannot_be_picked(api, produced):
    pid, sid, vid = produced
    with session_scope() as s:
        v = s.get(Variation, vid)
        v.params = {**v.params, "cond": "old-frame"}
    r = api.post(f"/api/scenes/{sid}/promote/{vid}?override=true")
    assert r.status_code == 409 and "eski bir kareden" in r.json()["detail"]


def test_the_director_can_ask_for_a_full_round_clip_with_the_same_seed(api, produced, monkeypatch):
    pid, sid, vid = produced
    sent = []
    from celery.canvas import _chain
    monkeypatch.setattr(_chain, "apply_async", lambda self, *a, **k: sent.append(self))
    r = api.post(f"/api/scenes/{sid}/promote/{vid}?override=true&full=true")
    assert r.status_code == 200 and r.json()["override"] == "full" and r.json()["stages"][0] == "VISUAL"
    names = [t.task for t in sent[0].tasks]
    assert names[0] == "hg.override_full" and "hg.mux_scene" in names
    from app.worker.dispatch import release_dispatch
    release_dispatch(sid)
    assert run_override_full(pid, sid, vid) == "refined"                            # görevin kendisi (ücretsiz mod)
    with session_scope() as s:
        top = s.scalars(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1)).one()
        assert top.seed == 777 and top.tier == "full" and top.params["director_override"]["full"] is True
        assert top.params["resolution"] == CFG["full_resolution"]


# ---------- (6) kırp + iyileştir çekiminin son karesi ve kamerası ----------

def test_a_cut_in_shot_keeps_its_framing_in_the_end_frame_and_the_video_prompt():
    class Sc:
        camera, camera_plan, board_method = {}, {"shot": "close_up", "side": -35, "angle": "low", "lens_mm": 50}, "cut_in"
    assert prompt_camera(Sc) == {"shot": "close_up", "lens_mm": 50}            # kırpım: kamera dönmedi
    Sc.board_method = "angles"
    assert prompt_camera(Sc)["side"] == -35
    text = sb.end_prompt("Bolt sits still.", [{"name": "S001_SH03", "key": "k", "kind": "frame"}],
                         {"shot": "close_up", "lens_mm": 50}, tight=True)
    assert sb.TIGHT_END in text and "do not widen, rotate or move the camera" in text


def test_the_end_frame_of_a_cut_in_close_up_is_tight_and_judged_against_its_first_frame(edit, mk_project, tmp_path,  # noqa: F811
                                                                                         monkeypatch):
    from tests.test_cut_methods import _kivilcim
    from tests.test_qwen_edit import character_shot
    seen = []
    monkeypatch.setattr(gate, "scorer_mode", lambda: "real")
    monkeypatch.setattr(gate, "detect_frame_anomalies", lambda path: [])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: seen.append(target.style_frame) or scorer.Result(
        clip=0.95, face=0.9, composite=0.95, detail={"faces_seen": 1, "identity": {"Mila": 0.9, "Bolt": 0.9}}))
    pid, codes = _kivilcim(mk_project, chain_end_frame=True)
    with session_scope() as s:
        s.get(Scene, codes["S001_SH03"]).cut_method = "cut_in"
    for code in ("S001_SH02", "S001_SH03"):
        character_shot(pid, code, tmp_path)
    sb.make_board(pid, codes["S001_SH03"])
    ends = [r["input"]["workflow"]["170:151"]["inputs"]["prompt"] for r in edit_runs(edit)
            if r["input"]["workflow"]["170:151"]["inputs"]["prompt"].startswith("Image 1 is the first frame of this shot")]
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH03"])
        kn = sb.clean_key(sc.board_key)
    assert ends and sb.TIGHT_END in ends[-1] and "35° to the left" not in ends[-1]   # son kare kırpım kadrajında
    assert kn in seen                                                                 # stil ilk kareye göre ölçüldü


def test_the_inspector_shows_the_path_the_frame_was_drawn_with(api, mk_project):
    from tests.test_cut_methods import _kivilcim
    pid, codes = _kivilcim(mk_project)
    with session_scope() as s:
        sc = s.get(Scene, codes["S001_SH05"])
        sc.board_method, sc.transition_in, sc.cut_method = "anchor", "continuous", "wide"
    shots = {x["scene_code"]: x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    sh05 = shots["S001_SH05"]
    assert sh05["board_method"] == "anchor" and sh05["cut_method"] == "wide" and sh05["cut_method_effective"] is None
    assert shots["S001_SH03"]["angle_tokens"] == "<sks> front-left quarter view low-angle shot close-up"
