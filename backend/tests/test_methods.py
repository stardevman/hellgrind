"""Faz 15: çekim üretim yöntemi, dış çekimler ve elle düzeltme.

Kabul:
  - `external` bir çekim hiçbir üretim harcaması yapmadan `done` olur.
  - Bir revizyon yüklenince VISUAL çalışmaz, LIPSYNC ve MUX çalışır.
"""
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.core.hashing import visual_fingerprint
from app.core.rerender import Stage, plan_rerender
from app.db import session_scope
from app.engine import methods
from app.engine.procedural import render_clip
from app.engine.providers import LocalProvider
from app.hermes import rules
from app.models import Revision, Scene, Variation
from app.style.bible import EXAMPLE
from tests.media_utils import frame, make_mixed, make_video
from tests.scene_utils import plans, scene_row

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


# ---------- birim (veritabanı gerekmez) ----------

def _scene(**kw):
    base = dict(prompt_spec={"prompt": "p"}, camera={}, engine="fal_wan", duration_s=5.0, method="generate",
                external_hash=None, guide_hash=None, visual_hash=None, dialogue_hash=None, bed_hash=None,
                lipsync_video_key=None, final_video_key=None, board_key=None, board_hash=None, action_complexity=0.1)
    return SimpleNamespace(**{**base, **kw})


def test_an_external_shot_is_identified_only_by_its_file():
    a = _scene(method="external", external_hash="abc")
    assert visual_fingerprint(a, [], []) == visual_fingerprint(_scene(method="external", external_hash="abc",
                                                                       prompt_spec={"prompt": "başka"}), [], [], 3)
    assert visual_fingerprint(a, [], []) != visual_fingerprint(_scene(method="external", external_hash="xyz"), [], [])


def test_a_previs_guide_joins_the_fingerprint_and_generate_keeps_its_old_one():
    plain = visual_fingerprint(_scene(), [], [])
    assert visual_fingerprint(SimpleNamespace(**{k: v for k, v in vars(_scene()).items() if k != "method"}), [], []) == plain
    assert visual_fingerprint(_scene(method="previs_guide", guide_hash="g1"), [], []) != plain
    assert visual_fingerprint(_scene(method="previs_guide", guide_hash="g1"), [], []) != \
        visual_fingerprint(_scene(method="previs_guide", guide_hash="g2"), [], [])


def test_an_external_shot_never_plans_a_storyboard_or_a_visual():
    assert plan_rerender(_scene(method="external", external_hash="abc"), [], [])[0] == Stage.LIPSYNC | Stage.MUX


@pytest.mark.parametrize("complexity,movement,method,hint", [
    (0.8, "handheld", "generate", "previs_guide"),
    (0.8, "tracking", "generate", "previs_guide"),
    (0.8, "static", "generate", None),
    (0.3, "handheld", "generate", None),
    (0.9, "orbit", "external", None),
])
def test_the_router_suggests_previs_for_intense_special_motion(complexity, movement, method, hint):
    sc = _scene(action_complexity=complexity, camera={"movement": movement}, method=method)
    assert methods.suggest_method(sc) == hint


def test_the_guide_grade_follows_the_lighting_language():
    assert methods.grade_filter(EXAMPLE["lighting_prefix"]) == methods.GRADES["golden_hour"]
    assert methods.grade_filter("") == "null" and methods.grade_filter(None) == "null"


def test_a_first_frame_edit_opens_the_clip_and_dissolves_into_it(tmp_path):
    base = make_video(tmp_path / "base.mp4", seconds=3.0, size="160x90", fps=12, color="0x2040c0")
    still = tmp_path / "f.png"
    Image.new("RGB", (160, 90), (230, 40, 30)).save(still)
    out = methods.first_frame_clip(base, str(still), str(tmp_path / "ff.mp4"))
    start, later = frame(out, 0.1).astype(float).mean(axis=(0, 1)), frame(out, 2.0).astype(float).mean(axis=(0, 1))
    assert start[0] > 180 and start[2] < 80                          # düzenlenmiş kare (kırmızı)
    assert later[2] > 150 and later[0] < 80                          # sonra orijinal (mavi)


# ---------- dış çekim ----------

@pytest.fixture
def sample(api, api_project, fast, eager):
    def make(**cfg):
        pid = api_project(1000.0, **{**SMALL, **cfg})
        assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
        with session_scope() as s:
            ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        return pid, ids
    return make


def upload(api, url, path, content_type="video/mp4"):
    return api.post(url, content=open(path, "rb").read(), headers={"content-type": content_type})


def ledger(pid):
    with session_scope() as s:
        return s.execute(text("SELECT provider, kind FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}).all()


def test_an_external_shot_finishes_without_any_production_spend(api, sample, tmp_path, monkeypatch):
    pid, ids = sample(lint_strictness="block", require_board_approval=True)   # varlık ve kare kapıları dış çekimi durdurmaz
    sid = ids["S002_SH01"]
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)
    clip = make_video(tmp_path / "kovalama.mp4", seconds=3.0, size="320x180")
    r = upload(api, f"/api/scenes/{sid}/external?filename=kovalama.mp4", clip)
    assert r.status_code == 200, r.text
    assert (r.json()["method"], r.json()["stages"]) == ("external", ["LIPSYNC", "MUX"])
    sc = scene_row(sid)
    assert (sc.status, sc.method, sc.cut_tier) == ("done", "external", "final") and storage.exists(sc.final_video_key)
    assert sc.visual_hash == visual_fingerprint(sc, [], []) and sc.spent_usd == 0
    assert ledger(pid) == [] and calls == []                          # hiçbir rezervasyon, hiçbir üretim
    with session_scope() as s:
        winner = s.scalar(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1))
        assert (winner.params["source"], float(winner.cost_usd), winner.video_key) == ("external", 0.0, sc.external_key)
    assert plans(pid)["S002_SH01"] == Stage.NONE


def test_a_defective_external_clip_is_refused_and_changes_nothing(api, sample, tmp_path):
    pid, ids = sample()
    sid = ids["S001_SH01"]
    bad = make_mixed(tmp_path / "bad.mp4", [("black", 2.0), ("moving", 1.0)])
    r = upload(api, f"/api/scenes/{sid}/external?filename=bad.mp4", bad)
    assert r.status_code == 422 and "kalite kontrolünden geçemedi (black" in r.text
    assert (scene_row(sid).method, scene_row(sid).external_key) == ("generate", None)
    assert upload(api, f"/api/scenes/{sid}/external?filename=x.txt", bad).status_code == 422


def test_a_method_needs_its_file_and_switching_back_keeps_the_earlier_clips(api, sample, tmp_path):
    pid, ids = sample()
    sid = ids["S001_SH01"]
    assert api.post(f"/api/scenes/{sid}/method", json={"method": "external"}).status_code == 422
    assert api.post(f"/api/scenes/{sid}/method", json={"method": "previs_guide"}).status_code == 422
    assert api.post(f"/api/scenes/{sid}/render").json()["stages"][-1] == "MUX"            # önce üretilir
    generated = {v.id for v in variations(sid) if v.status == "refined"}
    upload(api, f"/api/scenes/{sid}/external?filename=c.mp4", make_video(tmp_path / "c.mp4", seconds=3.0))
    assert {v.id for v in variations(sid) if v.status == "passed"} >= generated              # havuz silinmedi
    r = api.post(f"/api/scenes/{sid}/method", json={"method": "generate"}).json()
    assert "VISUAL" in r["stages"] and scene_row(sid).method == "generate"


def variations(sid):
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.scene_id == sid)).all()
        s.expunge_all()
        return rows


# ---------- previs rehberi ----------

def test_a_previs_guide_is_graded_with_the_style_and_used_without_generation(api, sample, tmp_path, monkeypatch):
    pid, ids = sample()
    v = api.post(f"/api/projects/{pid}/style", json=EXAMPLE).json()["version"]
    api.post(f"/api/projects/{pid}/style/activate", json={"version": v})
    sid = ids["S002_SH01"]
    calls = []
    real = LocalProvider.generate
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or real(self, req))
    guide = str(tmp_path / "previs.mp4")                                     # orta tonlu, hareketli bir 3D taslak yerine
    render_clip(guide, 5, 320, 180, 3.0, 8, emotion="neutral", camera={"shot": "wide", "movement": "pan"},
                characters=["Kerem"])
    r = upload(api, f"/api/scenes/{sid}/guide?filename=previs.mp4", guide)
    assert r.status_code == 200 and "VISUAL" in r.json()["stages"], r.text
    sc = scene_row(sid)
    assert (sc.status, sc.method) == ("done", "previs_guide") and calls == []               # görüntü motoru çağrılmadı
    with session_scope() as s:
        winner = s.scalar(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1))
        assert winner.params["source"] == "previs_guide" and float(winner.cost_usd) > 0
        graded_key = winner.video_key
    assert ("v2v:720p:previs", "commit") in ledger(pid)                                     # sanal video-to-video bedeli
    graded = storage.download(graded_key, str(tmp_path / "g.mp4"))
    warmth = lambda img: img[..., 0].mean() - img[..., 2].mean()                            # noqa: E731
    g, o = frame(graded, 1.0).astype(float), frame(guide, 1.0).astype(float)
    assert warmth(g) - warmth(o) > 10                                                       # altın saat renk dili
    assert plans(pid)["S002_SH01"] == Stage.NONE


# ---------- elle düzeltme ----------

@pytest.fixture
def produced(api, sample):
    pid, ids = sample()
    sid = ids["S002_SH01"]
    assert "VISUAL" in api.post(f"/api/scenes/{sid}/render").json()["stages"]
    with session_scope() as s:
        pool = {v.refined_rank: str(v.id) for v in s.scalars(select(Variation).where(Variation.scene_id == sid,
                                                                                    Variation.refined_rank.is_not(None)))}
    return pid, sid, pool


def test_a_revision_of_the_winner_reruns_only_lipsync_and_mux(api, produced, tmp_path, monkeypatch):
    pid, sid, pool = produced
    winner = pool[1]
    before = scene_row(sid)
    original = next(v for v in variations(sid) if str(v.id) == winner).video_key
    calls = []
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req) or None)
    edited = make_video(tmp_path / "edit.mp4", seconds=3.0, size="160x90")
    r = upload(api, f"/api/variations/{winner}/revisions?filename=edit.mp4&note=kalem%20%C3%A7izgileri", edited)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["stages"] == ["LIPSYNC", "MUX"] and body["revision"]["version"] == 1 and calls == []
    rev_id = body["revision"]["id"]
    sc = scene_row(sid)
    assert f"{winner}_r{rev_id}_" in sc.lipsync_video_key and sc.final_video_key != before.final_video_key
    assert next(v for v in variations(sid) if str(v.id) == winner).video_key == original      # orijinal korundu
    again = upload(api, f"/api/variations/{winner}/revisions?filename=edit.mp4", edited).json()
    assert again["revision"]["id"] == rev_id and again["stages"] == []                         # aynı dosya: değişiklik yok
    second = upload(api, f"/api/variations/{winner}/revisions?filename=e2.mp4",
                    make_video(tmp_path / "e2.mp4", seconds=3.0, size="192x108")).json()
    assert second["revision"]["version"] == 2
    history = api.get(f"/api/variations/{winner}/revisions").json()
    assert [x["version"] for x in history["revisions"]] == [2, 1] and history["active"] == second["revision"]["id"]
    assert history["revisions"][1]["note"] == "kalem çizgileri"
    back = api.post(f"/api/variations/{winner}/revisions/active", json={"revision_id": None}).json()
    assert back["stages"] == ["LIPSYNC", "MUX"] and "_r" not in scene_row(sid).lipsync_video_key


def test_a_revision_of_a_runner_up_waits_until_it_is_chosen(api, produced, tmp_path):
    pid, sid, pool = produced
    r = upload(api, f"/api/variations/{pool[2]}/revisions?filename=e.mp4", make_video(tmp_path / "e.mp4", seconds=3.0))
    assert r.json()["stages"] == [] and "Bunu kullan" in r.json()["hint"]
    assert api.post(f"/api/scenes/{sid}/promote/{pool[2]}").json()["stages"] == ["LIPSYNC", "MUX"]
    assert f"{pool[2]}_r{r.json()['revision']['id']}_" in scene_row(sid).lipsync_video_key


def test_a_first_frame_revision_makes_a_new_clip_from_the_edited_frame(api, produced, tmp_path):
    pid, sid, pool = produced
    still = tmp_path / "f.png"
    Image.new("RGB", (160, 90), (240, 20, 20)).save(still)
    r = upload(api, f"/api/variations/{pool[1]}/revisions?kind=first_frame&filename=f.png", still, "image/png")
    assert r.status_code == 201 and r.json()["revision"]["kind"] == "first_frame", r.text
    with session_scope() as s:
        rev = s.get(Revision, r.json()["revision"]["id"])
        assert rev.source_key.endswith(".png") and rev.video_key.endswith("_ff.mp4")
        clip = storage.download(rev.video_key, str(tmp_path / "ff.mp4"))
    assert frame(clip, 0.1).astype(float).mean(axis=(0, 1))[0] > 180


def test_bad_revisions_are_refused(api, produced, tmp_path):
    pid, sid, pool = produced
    black = make_video(tmp_path / "black.mp4", seconds=3.0, color="black")
    r = upload(api, f"/api/variations/{pool[1]}/revisions?filename=black.mp4", black)
    assert r.status_code == 422 and "orijinal korunuyor" in r.text
    assert upload(api, f"/api/variations/{pool[1]}/revisions?kind=first_frame&filename=x.png", black).status_code == 422
    with session_scope() as s:                                                 # elenmiş: videosu yok
        s.execute(text("UPDATE variations SET status = 'purged', video_key = NULL WHERE id = :v"), {"v": pool[2]})
    assert upload(api, f"/api/variations/{pool[2]}/revisions?filename=e.mp4", black).status_code == 409
    assert api.post("/api/variations/nope/revisions?filename=e.mp4", content=b"x").status_code == 422
