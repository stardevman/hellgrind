"""Model karışımı ve parça bölme (karma kurgu testi bulgusu): bir whole_scene parçasının çekimleri yalnızca AYNI
üretimden gelebilir; sahne içi model değişimi per_shot ve son kareyle bağlanır; parça klibi gerçek kesmelerden bölünür.
Ücretli çağrı yok (ücretsiz mod, yerel render)."""
import subprocess

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.engine import part_render, scene_parts
from app.models import Scene, Variation
from tests.fal_queue_fake import validate
from tests.test_reference_mode import nora, plan_for, ref_defaults  # noqa: F401 — fikstür


# ---------- gerçek kesmeden bölme ----------

def _two_colours(path, first=3.4, second=2.6):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=red:size=160x90:rate=24:d={first}",
                    "-f", "lavfi", "-i", f"color=c=blue:size=160x90:rate=24:d={second}", "-filter_complex",
                    "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return str(path)


def test_a_real_cut_is_detected_and_used_instead_of_the_planned_second(tmp_path):
    clip = _two_colours(tmp_path / "cut.mp4")
    cuts = part_render.detect_cuts(clip)
    assert len(cuts) == 1 and cuts[0] == pytest.approx(3.4, abs=1 / 24 + 1e-3)
    segments, sources, warning = part_render.boundaries([("SH01", 0.0, 3.0), ("SH02", 3.0, 6.0)], 6.0, cuts)
    assert sources == ["cut"] and segments[0][2] == pytest.approx(3.4, abs=1 / 24 + 1e-3) and warning is None
    assert segments[1][1] == segments[0][2] and segments[1][2] == 6.0


def test_a_missing_cut_falls_back_to_the_nearest_planned_boundary_with_a_warning():
    segments, sources, warning = part_render.boundaries(
        [("SH01", 0.0, 3.0), ("SH02", 3.0, 6.0), ("SH03", 6.0, 10.0)], 10.7, [3.3])
    assert sources == ["cut", "planned"]
    assert segments[1][2] == pytest.approx(6.0 * 10.7 / 10, abs=1e-3)       # klip uzunsa planlı sınır ölçeklenir
    assert "1 kesme bulundu, senaryoda 2 çekim sınırı" in warning and "SH03 en yakın planlı sınırdan" in warning


def test_a_cut_too_far_from_the_plan_is_not_used():
    _, sources, warning = part_render.boundaries([("SH01", 0.0, 3.0), ("SH02", 3.0, 6.0)], 6.0, [0.5])
    assert sources == ["planned"] and warning


# ---------- aynı üretim kuralı ----------

def _two_generations(mk_project):
    """Nora sahnesi önce H3 Max, sonra Seedance 2.5 ile bütün olarak üretildi (iki üretim, kazanan Seedance)."""
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    with session_scope() as s:
        g1 = {code: str(s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1)))
              for code, sid in ids.items()}
        for sid in ids.values():
            s.get(Scene, sid).engine = "seedance25"
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    return pid, ids, g1


def test_picking_a_clip_from_another_generation_warns_and_adopting_switches_the_whole_scene(
        ref_defaults, mk_project, fast, api, dispatched):
    pid, ids, g1 = _two_generations(mk_project)
    r = api.post(f"/api/scenes/{ids['S001_SH02']}/promote/{g1['S001_SH02']}?override=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warning"] == ("Bu çekim başka bir üretimden; geçiş tutarsız olabilir. Bütün sahneyi bu motorla değiştir "
                               "ya da bu çekimi ayrı üret.")
    assert [o["action"] for o in body["options"]] == ["adopt", "shot"]
    overview = {sc["scene_code"]: sc for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert all(overview[c]["generation"] and not overview[c]["generation"]["consistent"] for c in ids)
    with session_scope() as s:
        before = s.scalar(select(Variation.id).where(Variation.scene_id == ids["S001_SH01"]).order_by(
            Variation.created_at.desc()).limit(1))
        count = len(s.scalars(select(Variation.id)).all())
    adopt = api.post(f"/api/scenes/{ids['S001_SH02']}/generation/adopt?variation_id={g1['S001_SH02']}")
    assert adopt.status_code == 200 and adopt.json()["engine"] == "h3max" and adopt.json()["usd"] == 0.0
    with session_scope() as s:
        winners = {code: str(s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1)))
                   for code, sid in ids.items()}
        assert {s.get(Scene, sid).engine for sid in ids.values()} == {"h3max"}
        assert len(s.scalars(select(Variation.id)).all()) == count               # yeniden üretim yok
        assert all((s.get(Scene, sid).grade_auto or {}).get("pending") for sid in ids.values())   # renk eşleme sonra
    assert winners == g1
    overview = {sc["scene_code"]: sc for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"]}
    assert all(overview[c]["generation"] is None for c in ids)                   # artık tutarlı
    assert before is not None


# ---------- sahne içi model değişimi: per_shot ve son kare ----------

def test_a_mid_scene_model_switch_is_its_own_request_linked_by_last_frames(ref_defaults, mk_project, fast, api,
                                                                         dispatched):
    from app.engine.iteration import run_tiered_generation
    pid, ids = nora(mk_project)
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    r = api.post(f"/api/scenes/{ids['S001_SH02']}/shot-engine?engine=seedance25")
    assert r.status_code == 200 and r.json()["relinked"] == ["S001_SH02", "S001_SH03"]
    with session_scope() as s:
        sh01 = s.scalar(select(Variation.id).where(Variation.scene_id == ids["S001_SH01"], Variation.refined_rank == 1))
    sd = plan_for(ids["S001_SH02"])
    assert sd["part"] == ["S001_SH02"] and sd["continuity"]["model_switch"] is True       # ayrı istek
    assert sd["body"]["image_urls"][0] == f"local:seam/{sh01}.jpg"                         # Seedance: başa
    assert "starts exactly from @Image1" in sd["body"]["prompt"] and validate(sd["endpoint"], sd["body"]) == []
    assert any(w.startswith("Sahne içi model değişimi") for w in sd["warnings"])
    h3 = plan_for(ids["S001_SH03"])
    assert h3["part"] == ["S001_SH03"] and h3["continuity"]["model_switch"] is True       # H3'e dönüş de bağlanır
    assert run_tiered_generation(pid, ids["S001_SH03"]) == "refined"    # önce SH02 (Seedance) üretilir, sonra SH03
    with session_scope() as s:
        w2 = s.scalar(select(Variation).where(Variation.scene_id == ids["S001_SH02"], Variation.refined_rank == 1))
        w3 = s.scalar(select(Variation).where(Variation.scene_id == ids["S001_SH03"], Variation.refined_rank == 1))
        assert w2.engine == "seedance25" and w2.params["part_codes"] == ["S001_SH02"]
        assert w2.params["continuity"]["frame_key"] == f"seam/{sh01}.jpg"
        assert w3.engine == "h3max" and w3.params["continuity"]["frame_key"] == f"seam/{w2.id}.jpg"


def test_plain_cuts_inside_one_engine_still_get_no_frame(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH02"])
    assert plan["part"] == list(ids) and plan["continuity"]["model_switch"] is False
