"""Faz 14: storyboard onayı ve taslak geçiş (kaba kurgu).

Kabul:
  - Onay açıkken onaylanmamış çekim için hiçbir varyasyon üretilmez.
  - Storyboard PDF'i tüm çekimleri doğru sırada içerir.
  - Taslak geçişte tam tur hiç çalışmaz; iyileştirme geçişinde yalnızca işaretli çekimler tam tura girer.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.core.rerender import ALL, PRODUCTION, Stage, plan_rerender
from app.db import session_scope
from app.engine import storyboard
from app.engine.iteration import run_tiered_generation
from app.engine.providers import LocalProvider
from app.hermes import rules
from app.models import Project, Scene, Variation
from tests.scene_utils import mark_finished, scene_row

CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]
SMALL = dict(probe_n=2, full_n=4, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


# ---------- planlayıcı (veritabanı gerekmez) ----------

def _scene(**kw):
    base = dict(prompt_spec={"prompt": "p"}, camera={}, engine="fal_wan", duration_s=5.0, visual_hash=None,
                dialogue_hash=None, bed_hash=None, lipsync_video_key=None, final_video_key=None, board_key=None,
                board_hash=None)
    return SimpleNamespace(**{**base, **kw})


def test_a_new_shot_starts_with_its_storyboard_frame():
    assert plan_rerender(_scene(), [], [])[0] == ALL == Stage.STORYBOARD | PRODUCTION


def test_a_current_frame_is_not_drawn_again():
    fp = plan_rerender(_scene(), [], [])[1]
    assert plan_rerender(_scene(board_key="b.jpg", board_hash=fp.visual), [], [])[0] == PRODUCTION


def test_a_finished_shot_without_a_frame_plans_nothing():
    """Faz 14 öncesi bitmiş sahneler: kare yalnızca görüntü yeniden üretilecekse çizilir."""
    sc = _scene()
    _, fp = plan_rerender(sc, [], [])
    done = _scene(visual_hash=fp.visual, dialogue_hash=fp.dialogue, bed_hash=fp.bed, lipsync_video_key="l",
                  final_video_key="f")
    assert plan_rerender(done, [], [])[0] == Stage.NONE


# ---------- PDF düzeni (veritabanı gerekmez) ----------

def _rows(n_by_seq):
    rows, k = [], 0
    for code, n in n_by_seq:
        for _ in range(n):
            k += 1
            rows.append({"code": f"S{k:03d}_SH01", "sequence": (code, code.title()), "camera": {"shot": "wide"},
                         "duration": 4.0, "board_status": "pending", "note": None,
                         "dialogue": [("Kerem", "Burada kimse yok mu? " * 6)]})
    return rows


def test_pages_hold_six_frames_and_every_sequence_starts_a_new_page():
    pages = storyboard.layout(_rows([("SEQ01", 8), ("SEQ02", 2)]))
    assert [(p["sequence"][0], len(p["cells"]), p["continued"]) for p in pages] == [
        ("SEQ01", 6, False), ("SEQ01", 2, True), ("SEQ02", 2, False)]


def test_the_pdf_lists_every_shot_in_order(tmp_path):
    rows = _rows([("SEQ01", 7), ("SEQ02", 1)])
    frame = Image.new("RGB", (320, 180), (200, 120, 60))
    path = storyboard.build_pdf("Örnek film", rows, {r["code"]: frame if i % 2 else None for i, r in enumerate(rows)},
                                str(tmp_path / "s.pdf"))
    data = Path(path).read_bytes()
    assert data.startswith(b"%PDF")
    assert pdf_subject(data) == [r["code"] for r in rows]
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 3


def pdf_subject(data: bytes) -> list[str]:
    """PDF'in Subject alanı. Pillow parantezli dize yazar; ASCII dışı karakter varsa UTF-16BE ve BOM ile."""
    raw = re.search(rb"/Subject\s*\((.*?)(?<!\\)\)", data, re.S).group(1)
    raw = re.sub(rb"\\(.)", rb"\1", raw)                       # PDF dize kaçışları: \( \) \\
    value = raw[2:].decode("utf-16-be") if raw.startswith(b"\xfe\xff") else raw.decode("latin-1")
    return value.split(",")


def test_a_board_frame_is_a_mid_shot_frame_with_the_shot_written_on_it(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.work_dir", str(tmp_path))
    render = {"emotion": "tension", "camera": {"shot": "wide", "movement": "pan"}, "characters": ["Kerem"],
              "action": 0.2, "lighting": "golden-hour"}
    path = storyboard.render_board(str(tmp_path / "b.jpg"), 7, render, ["S001_SH01 · geniş, pan · 3,0 sn"])
    with Image.open(path) as im:
        assert im.size == (storyboard.BOARD_W, storyboard.BOARD_H)
        band = im.crop((0, im.height - 40, im.width, im.height)).convert("L")
        assert band.getextrema()[1] > 200                      # alt bantta açık renk yazı var


# ---------- STORYBOARD aşaması ----------

@pytest.fixture
def project(api, api_project):
    def make(**cfg):
        pid = api_project(1000.0, **{**SMALL, **cfg})
        assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
        with session_scope() as s:
            ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        return pid, ids
    return make


def ledger(pid, provider):
    with session_scope() as s:
        return s.execute(text("SELECT kind, amount_usd FROM cost_ledger WHERE project_id = :p AND provider = :v ORDER BY id"),
                         {"p": pid, "v": provider}).all()


def test_the_storyboard_stage_draws_a_charged_frame_once(project, fast):
    pid, ids = project()
    sid = ids["S001_SH01"]
    key = storyboard.make_board(pid, sid)
    sc = scene_row(sid)
    assert key == sc.board_key and key.startswith(f"storyboard/{sid}/") and storage.exists(key)
    assert sc.board_hash and sc.board_status == "pending"
    assert [k for k, _ in ledger(pid, "flux_lora:storyboard")] == ["reserve", "commit"]   # sanal Flux karesi
    storyboard.make_board(pid, sid)
    assert len(ledger(pid, "flux_lora:storyboard")) == 2                                  # güncel kare: yeniden çizilmez


def test_a_changed_shot_gets_a_new_frame_and_needs_approval_again(api, project, fast):
    pid, ids = project()
    sid = ids["S001_SH01"]
    first = storyboard.make_board(pid, sid)
    assert api.post(f"/api/scenes/{sid}/board", json={"status": "approved"}).json()["board_status"] == "approved"
    api.patch(f"/api/scenes/{sid}", json={"camera": {"shot": "close_up", "movement": "static"}})
    second = storyboard.make_board(pid, sid)
    sc = scene_row(sid)
    assert second != first and sc.board_status == "pending"


def test_preparing_the_storyboard_draws_every_stale_frame_and_leaves_shots_for_review(api, project, fast, eager):
    pid, ids = project()
    r = api.post(f"/api/projects/{pid}/storyboard").json()
    assert r["queued"] == CODES
    ov = api.get(f"/api/projects/{pid}/overview").json()
    assert all(sc["status"] == "board_review" and sc["board_url"] for sc in ov["scenes"])
    assert api.post(f"/api/projects/{pid}/storyboard").json() == {"queued": [], "current": CODES, "skipped": []}
    with session_scope() as s:
        assert s.scalar(text("SELECT count(*) FROM variations v JOIN scenes sc ON sc.id = v.scene_id "
                             "WHERE sc.project_id = :p"), {"p": pid}) == 0


def test_board_review_is_validated(api, project):
    pid, ids = project()
    sid = ids["S001_SH01"]
    r = api.post(f"/api/scenes/{sid}/board", json={"status": "approved"})
    assert r.status_code == 409 and "karesi yok" in r.json()["detail"]
    r = api.post(f"/api/scenes/{sid}/board", json={"status": "changes_requested", "note": "  Kerem daha yakın olsun "})
    assert r.json() == {"scene_id": sid, "board_status": "changes_requested", "board_note": "Kerem daha yakın olsun"}
    assert api.post(f"/api/scenes/{sid}/board", json={"status": "maybe"}).status_code == 422


# ---------- onay kapısı ----------

def test_with_approval_required_unapproved_shots_produce_no_variation(api, project, fast, eager):
    pid, ids = project(require_board_approval=True)
    r = api.post(f"/api/projects/{pid}/render-all").json()
    assert r["awaiting_board"] == CODES and all(st == ["STORYBOARD"] for st in r["planned"].values())
    api.post(f"/api/scenes/{ids['S002_SH01']}/board", json={"status": "approved"})
    r = api.post(f"/api/projects/{pid}/render-all").json()
    assert r["planned"]["S002_SH01"] == ["VISUAL", "LIPSYNC", "MUX"] and sorted(r["awaiting_board"]) == [
        "S001_SH01", "S001_SH02", "S002_SH02"]
    with session_scope() as s:
        produced = dict(s.execute(text("""SELECT sc.scene_code, count(v.id) FROM scenes sc LEFT JOIN variations v
                                          ON v.scene_id = sc.id WHERE sc.project_id = :p GROUP BY sc.scene_code"""),
                                  {"p": pid}).all())
    assert produced == {"S001_SH01": 0, "S001_SH02": 0, "S002_SH01": produced["S002_SH01"], "S002_SH02": 0}
    assert produced["S002_SH01"] > 0 and scene_row(ids["S002_SH01"]).status == "done"
    assert {scene_row(ids[c]).status for c in ("S001_SH01", "S001_SH02", "S002_SH02")} == {"board_review"}


def test_a_single_render_reports_that_the_frame_awaits_approval(api, project, dispatched):
    pid, ids = project(require_board_approval=True)
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/render").json()
    assert r == {"stages": ["STORYBOARD"], "awaiting_board": True}


def test_the_worker_refuses_an_unapproved_frame_too(sample_scene):
    pid, codes = sample_scene(require_board_approval=True)
    sid = codes["S002_SH01"]
    assert run_tiered_generation(pid, sid) == "board_review"
    with session_scope() as s:
        assert s.scalars(select(Variation).where(Variation.scene_id == sid)).all() == []


def test_project_settings_can_switch_approval_on(api, api_project):
    pid = api_project()
    r = api.patch(f"/api/projects/{pid}", json={"settings": {"require_board_approval": True}})
    assert r.status_code == 200 and r.json()["settings"]["require_board_approval"] is True
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"require_board_approval": "evet"}}).status_code == 422


# ---------- PDF ----------

def test_the_storyboard_pdf_contains_every_shot_in_order(api, project, fast, eager):
    pid, ids = project()
    api.post(f"/api/projects/{pid}/storyboard")
    api.post(f"/api/scenes/{ids['S001_SH02']}/board", json={"status": "changes_requested", "note": "Fener daha parlak"})
    r = api.get(f"/api/projects/{pid}/storyboard.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert pdf_subject(r.content) == CODES
    assert len(re.findall(rb"/Type\s*/Page\b", r.content)) == 2                          # her sekans yeni sayfada


# ---------- taslak ve iyileştirme geçişleri ----------

def tiers(pid):
    with session_scope() as s:
        return s.execute(text("""SELECT sc.scene_code, v.tier, v.status, v.refined_rank FROM variations v
                                 JOIN scenes sc ON sc.id = v.scene_id WHERE sc.project_id = :p"""), {"p": pid}).all()


@pytest.fixture
def drafted(api, project, fast, eager):
    pid, ids = project()
    r = api.post(f"/api/projects/{pid}/draft-pass").json()
    assert r["drafted"] == CODES, r
    return pid, ids


def test_the_draft_pass_never_runs_a_full_round_and_assembles_the_rough_cut(api, drafted):
    pid, ids = drafted
    rows = tiers(pid)
    assert {t for _, t, _, _ in rows} == {"probe"}                                      # tam tur hiç çalışmadı
    pools = {}
    for code, _, status, rank in rows:
        if rank:
            pools.setdefault(code, []).append(rank)
    assert pools == {code: [1] for code in CODES}                                       # geçen ilk varyasyon
    ov = api.get(f"/api/projects/{pid}/overview").json()
    assert all(sc["status"] == "done" and sc["cut_tier"] == "draft" and sc["draft_url"] for sc in ov["scenes"])
    rough = ov["draft_timeline"]
    assert rough and [c["scene_code"] for c in rough["cut"]["clips"]] == CODES and rough["cut"]["kind"] == "draft"
    assert {c["tier"] for c in rough["cut"]["clips"]} == {"draft"} and ov["timeline"] is None


def test_the_upgrade_pass_runs_the_full_round_only_for_flagged_shots_and_reuses_the_test_clips(api, drafted, monkeypatch):
    pid, ids = drafted
    flagged = ids["S002_SH01"]
    draft_clip = scene_row(flagged).draft_video_key
    assert api.post(f"/api/scenes/{flagged}/upgrade", json={"requested": True}).json()["upgrade_requested"] is True
    calls = []
    real = LocalProvider.generate
    monkeypatch.setattr(LocalProvider, "generate", lambda self, req: calls.append(req.resolution) or real(self, req))
    r = api.post(f"/api/projects/{pid}/upgrade-pass").json()
    assert r["planned"] == {"S002_SH01": ["VISUAL", "LIPSYNC", "MUX"]}
    assert calls and set(calls) == {"720p"}                                             # test turu yeniden üretilmedi
    full = {code for code, tier, _, _ in tiers(pid) if tier == "full"}
    assert full == {"S002_SH01"}
    sc = scene_row(flagged)
    assert (sc.status, sc.cut_tier, sc.upgrade_requested, sc.draft_video_key) == ("done", "final", False, draft_clip)
    assert sc.final_video_key != draft_clip
    assert all(scene_row(ids[c]).cut_tier == "draft" for c in CODES if c != "S002_SH01")


def test_the_upgrade_pass_skips_shots_that_are_not_drafts(api, project, dispatched):
    pid, ids = project()
    mark_finished(pid)
    with session_scope() as s:
        s.execute(text("UPDATE scenes SET cut_tier = 'final', upgrade_requested = true WHERE project_id = :p"), {"p": pid})
    r = api.post(f"/api/projects/{pid}/upgrade-pass").json()
    assert r["planned"] == {} and r["skipped"] == CODES and dispatched == []


def test_the_rough_cut_and_the_final_film_are_assembled_separately(api, drafted):
    pid, _ = drafted
    r = api.post(f"/api/projects/{pid}/assemble").json()
    assert r == {"queued": True, "scenes": 4}
    with session_scope() as s:
        p = s.get(Project, pid)
        assert p.timeline_key and p.draft_timeline_key and p.timeline_key != p.draft_timeline_key
    assert api.post(f"/api/projects/{pid}/assemble?kind=draft").json() == {"queued": True, "scenes": 4, "kind": "draft"}
