"""En ucuz varsayılanlar, uç noktaya göre fiyatlar, Seedance taslağını 1080p'ye tamamlama ve upscale. Ücretli çağrı yok:
fiyatlar ve gövdeler ağsız; akışlar ücretsiz modda (yerel 1080p) ya da sahte istemciyle; gövdeler fal OpenAPI tanımıyla
doğrulanır."""
import sys
import time

import pytest
from sqlalchemy import select

from app import storage
from app.budget import pricing
from app.config import settings
from app.db import session_scope
from app.engine import fal_plan, finishing
from app.models import Scene, Variation
from tests.fal_queue_fake import FakeQueue, validate
from tests.test_reference_mode import _winner, nora, plan_for, ref_defaults  # noqa: F401 — fikstür


# ---------- fiyatlar ----------

@pytest.mark.parametrize("res, i2v, ref", [("480P", 0.03, 0.05), ("768P", 0.048, 0.08), ("1080P", 0.096, 0.16)])
def test_h3max_prices_differ_by_endpoint(res, i2v, ref):
    assert pricing.h3max_cost(res, 10, mode="i2v")["base_usd"] == pytest.approx(10 * i2v)
    assert pricing.h3max_cost(res, 10, mode="ref")["base_usd"] == pytest.approx(10 * ref)
    assert pricing.h3max_cost(res, 10, images=5, mode="i2v")["extra_tokens"] == 0          # token fazlası yalnızca ref
    assert pricing.h3max_cost(res, 10, images=5, mode="ref")["base_usd"] == pytest.approx(10 * ref + 1024 / 1000 * 0.02,
                                                                                     abs=1e-4)


def test_the_plan_prices_h3max_by_its_endpoint():
    assert fal_plan.cost("h3max", "480P", 10, 1, "i2v")["usd"] == pytest.approx(0.3 * 1.05, abs=1e-4)
    assert fal_plan.cost("h3max", "480P", 10, 4, "ref")["usd"] == pytest.approx(0.5 * 1.05, abs=1e-4)


def test_a_seedance_draft_is_priced_at_480p_and_the_completion_at_1080p_with_20_percent():
    draft = fal_plan.cost("seedance25", "720p", 10, draft_=True)
    assert draft["tokens"] == 864 * 480 * 10 * 24 / 1024 == 97200 and draft["per_1k_tokens_usd"] == 0.0214
    full = fal_plan.cost("seedance25", "720p", 10)
    assert full["tokens"] == 216000 and full["reserve_usd"] > draft["reserve_usd"]
    complete = pricing.seedance25_complete_cost(10)
    assert complete["usd"] == pytest.approx(11.6, abs=0.05) and complete["usd"] / 10 == pytest.approx(1.164, abs=0.005)
    assert complete["reserve_usd"] == pytest.approx(complete["usd"] * 1.2, abs=1e-3) and complete["resolution"] == "1080p"


@pytest.mark.parametrize("endpoint, expected", [
    ("fal-ai/bytedance-upscaler/upscale/video", 0.072),
    ("fal-ai/kandinsky6-vsr", 0.00036 * 1920 * 1080 / 1e6 * 240),
    ("fal-ai/seedvr/upscale/video", 0.001 * 1920 * 1080 / 1e6 * 240),
    ("fal-ai/flashvsr/upscale/video", 0.0005 * 1920 * 1080 / 1e6 * 240),
    ("blackforestlabs/flux-video-upscale", 1.4)])
def test_upscaler_prices_for_ten_seconds_at_24_fps(endpoint, expected):
    assert pricing.upscale_cost(endpoint, 10, 24)["usd"] == pytest.approx(expected, abs=1e-3)


def test_bytedance_doubles_above_30_fps_and_pro_is_ten_times():
    assert pricing.upscale_cost("fal-ai/bytedance-upscaler/upscale/video", 10, 60)["usd"] == pytest.approx(0.144)
    assert pricing.upscale_cost("fal-ai/bytedance-upscaler/upscale/video", 10, 24, tier="pro")["usd"] == pytest.approx(0.72)
    assert "en çok 5 sn" in pricing.upscale_cost("fal-ai/kandinsky6-vsr", 10, 24)["warning"]


@pytest.mark.parametrize("endpoint", finishing.UPSCALE_ENDPOINTS)
def test_upscale_bodies_follow_each_schema_and_keep_the_source_fps(endpoint):
    body = finishing.upscale_body(endpoint, "https://fal.test/v.mp4", 480, 24.0)
    assert validate(endpoint, body) == []
    if endpoint.startswith("fal-ai/bytedance"):
        assert body["target_fps"] == 24 and body["target_resolution"] == "1080p"     # gereksiz 60 fps yok


def test_the_completion_body_follows_its_schema():
    from app.live import fal_finish
    assert validate("bytedance/seedance-2.5/draft/complete", fal_finish.complete_body("d-1")) == []


# ---------- varsayılanlar ----------

def test_defaults_are_the_cheapest(ref_defaults, mk_project, s3):
    assert (settings.h3max_resolution, settings.seedance25_draft) == ("480P", True)
    pid, ids = nora(mk_project)
    h3 = plan_for(ids["S001_SH01"])
    assert h3["body"]["resolution"] == "480P"
    sd = plan_for(ids["S001_SH01"], "seedance25")
    assert sd["body"]["draft"] is True and sd["body"]["resolution"] == "480p" and sd["draft"] is True
    assert validate(sd["endpoint"], sd["body"]) == []


def test_a_project_can_raise_its_defaults(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project, h3max_resolution="768P", seedance25_draft=False, seedance25_resolution="1080p")
    assert plan_for(ids["S001_SH01"])["body"]["resolution"] == "768P"
    sd = plan_for(ids["S001_SH01"], "seedance25")
    assert "draft" not in sd["body"] and sd["body"]["resolution"] == "1080p"


# ---------- Seedance taslağını tamamlama ----------

@pytest.fixture
def seedance_draft(ref_defaults, mk_project, fast, dispatched, monkeypatch):
    """Nora sahnesi Seedance 2.5 taslağıyla (ücretsiz mod) üretilmiş; tamamlama ve upscale görevleri eşzamanlı."""
    from app.engine.iteration import run_tiered_generation
    from app.worker import tasks
    pid, ids = nora(mk_project)
    with session_scope() as s:
        for sid in ids.values():
            s.get(Scene, sid).engine = "seedance25"
    for sid in ids.values():
        assert run_tiered_generation(pid, sid) == "refined"
    monkeypatch.setattr(tasks.complete_draft, "delay", lambda p, s_: finishing.run_complete(p, s_))
    monkeypatch.setattr(tasks.upscale_clip, "delay",
                        lambda p, s_, v, e=None, b=False: finishing.run_upscale(p, v, e, b))
    return pid, ids


def test_a_seedance_draft_records_its_id_and_seven_day_validity(seedance_draft, api):
    pid, ids = seedance_draft
    with session_scope() as s:
        infos = [s.get(Scene, sid).seedance_draft for sid in ids.values()]
    assert len({i["draft_id"] for i in infos}) == 1 and infos[0]["part"] == list(ids)
    assert infos[0]["expires_at"] - infos[0]["created_at"] == pytest.approx(7 * 24 * 3600)
    view = api.get(f"/api/projects/{pid}/overview").json()["scenes"][0]["seedance_draft"]
    assert 0 < view["remaining_s"] <= 7 * 24 * 3600 and view["expired"] is False


def test_completion_shows_the_estimate_and_a_cheaper_upscale(seedance_draft, api):
    pid, ids = seedance_draft
    preview = api.get(f"/api/scenes/{ids['S001_SH02']}/draft").json()
    assert preview["available"] and preview["label"] == "≈ 11,6 $" and preview["seconds"] == 10.0
    assert preview["cheaper"]["label"] == "Upscale ile 1080p (≈ 0,07 $)"
    assert preview["cost"]["reserve_usd"] == pytest.approx(preview["cost"]["usd"] * 1.2, abs=1e-3)


def test_completion_is_blocked_over_budget_or_when_the_estimate_rose(seedance_draft, api):
    from sqlalchemy import text
    pid, ids = seedance_draft
    stale = api.post(f"/api/scenes/{ids['S001_SH01']}/seedance/complete?expected_usd=1")
    assert stale.status_code == 409 and "Tahmin değişti" in stale.json()["detail"]
    with session_scope() as s:
        s.execute(text("UPDATE projects SET budget_limit_usd = spent_usd + 5 WHERE id = :p"), {"p": pid})
    poor = api.post(f"/api/scenes/{ids['S001_SH01']}/seedance/complete?expected_usd=11.7")
    assert poor.status_code == 409 and "Bütçe yetmiyor" in poor.json()["detail"]


def test_completion_makes_1080p_clips_for_the_whole_part_and_keeps_the_drafts(seedance_draft, api, dispatched):
    pid, ids = seedance_draft
    with session_scope() as s:
        drafts = {sid: s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1))
                  for sid in ids.values()}
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/seedance/complete?expected_usd=11.7")
    assert r.status_code == 200, r.text
    with session_scope() as s:
        for sid in ids.values():
            win = s.scalar(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1))
            old = s.get(Variation, drafts[sid])
            assert win.params["resolution"] == "1080p" and win.params["completed_from"] == str(drafts[sid])
            assert old.status == "passed" and storage.exists(old.video_key)          # taslak kesimi korunur
            assert finishing.probe(storage.download(win.video_key, f"/tmp/{win.id}.mp4"))["height"] == 1080
            sc = s.get(Scene, sid)
            assert sc.cut_tier == "final" and sc.seedance_draft["completed_at"]
    assert all(set(st) == {"LIPSYNC", "MUX"} for _, sid, st in dispatched if sid in ids.values())
    again = api.post(f"/api/scenes/{ids['S001_SH01']}/seedance/complete")
    assert again.status_code == 409 and "zaten" in again.json()["detail"]


def test_an_expired_draft_cannot_be_completed(seedance_draft, api):
    pid, ids = seedance_draft
    with session_scope() as s:
        sc = s.get(Scene, ids["S001_SH01"])
        sc.seedance_draft = {**sc.seedance_draft, "expires_at": time.time() - 1}
    r = api.post(f"/api/scenes/{ids['S001_SH01']}/seedance/complete")
    assert r.status_code == 409 and "7 günlük süresi doldu" in r.json()["detail"]


# ---------- upscale ----------

def test_upscale_is_a_new_variation_and_colour_matching_is_redone_after_it(seedance_draft, api, dispatched):
    pid, ids = seedance_draft
    sid = ids["S001_SH02"]
    with session_scope() as s:
        sc = s.get(Scene, sid)
        sc.grade = {"exposure": 0.2}                             # düşük çözünürlükte otomatik eşleşme yapılmıştı
        sc.grade_auto = {"applied": True, "previous": {}, "reference": "S001_SH01"}
        orig = s.scalar(select(Variation.id).where(Variation.scene_id == sid, Variation.refined_rank == 1))
    plan = api.post(f"/api/scenes/{sid}/upscale?dry_run=true").json()
    assert plan["dry_run"] and plan["items"][0]["variation_id"] == str(orig) and plan["total_usd"] > 0
    assert set(plan["alternatives"]) == set(finishing.UPSCALE_ENDPOINTS)
    r = api.post(f"/api/scenes/{sid}/upscale?expected_usd={plan['total_usd']}")
    assert r.status_code == 200, r.text
    with session_scope() as s:
        win = s.scalar(select(Variation).where(Variation.scene_id == sid, Variation.refined_rank == 1))
        assert win.params["upscaled_from"] == str(orig) and win.params["resolution"] == "1080p"
        assert win.params["size"] == [1920, 1080] and win.params["fps"] == pytest.approx(settings.local_render_fps)
        assert s.get(Variation, orig).status == "passed"                          # orijinal korunur
        sc = s.get(Scene, sid)
        assert sc.grade_auto is None and sc.grade == {}                           # eşleme upscale'den sonra yeniden
    chain = [st for _, s_, st in dispatched if s_ == sid][-1]
    assert "VISUAL" not in chain and chain[-1] == "MUX" and "LIPSYNC" in chain


def test_whole_film_upscale_reassembles_only_after_the_last_mux(seedance_draft, api, monkeypatch):
    from app.worker import tasks
    pid, ids = seedance_draft
    assembled = []
    monkeypatch.setattr(tasks.assemble_timeline, "delay", lambda p, kind="final": assembled.append((p, kind)))
    plan = api.post(f"/api/projects/{pid}/upscale?dry_run=true").json()
    assert len(plan["items"]) == 3
    finishing.start_batch(pid, [i["scene_id"] for i in plan["items"]])
    assert finishing.upscale_done(pid, ids["S001_SH01"]) is False and assembled == []
    assert finishing.upscale_done(pid, ids["S001_SH02"]) is False and assembled == []
    assert finishing.upscale_done(pid, ids["S001_SH03"]) is True and assembled == [(pid, "final")]


def test_upscale_is_refused_over_the_remaining_budget(seedance_draft, api):
    from sqlalchemy import text
    pid, ids = seedance_draft
    with session_scope() as s:
        s.execute(text("UPDATE projects SET budget_limit_usd = spent_usd + 0.001 WHERE id = :p"), {"p": pid})
    r = api.post(f"/api/projects/{pid}/upscale")
    assert r.status_code == 409 and "Bütçe yetmiyor" in r.json()["detail"]


def test_the_film_is_1080p_only_when_every_clip_is(tmp_path):
    from tests.media_utils import make_video
    hd = make_video(tmp_path / "hd.mp4", seconds=0.5, size="1920x1080")
    sd = make_video(tmp_path / "sd.mp4", seconds=0.5, size="854x480")
    assert finishing.film_size([hd, hd]) == (1920, 1080) and finishing.film_size([hd, sd]) == (1280, 720)


def test_paid_upscale_uploads_the_clip_and_calls_the_chosen_endpoint(seedance_draft, paid, monkeypatch, tmp_path):
    from app.live import fal_queue, fal_video
    from tests.media_utils import make_video
    pid, ids = seedance_draft
    fake = FakeQueue()
    fake.behaviour["fal-ai/seedvr/upscale/video"] = [{"video": {"url": "https://fal.test/up.mp4"}}]
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(fal_queue, "POLL_S", 0)
    monkeypatch.setattr(fal_video, "_download", lambda url, suffix=".mp4": make_video(
        tmp_path / "up.mp4", seconds=3.0, size="1920x1080", fps=24))
    fal_queue._local_uploads.clear()
    for key in fal_queue._r().scan_iter("hg:fal:*"):
        fal_queue._r().delete(key)
    with session_scope() as s:
        vid = s.scalar(select(Variation.id).where(Variation.scene_id == ids["S001_SH01"], Variation.refined_rank == 1))
    assert finishing.run_upscale(pid, str(vid), "fal-ai/seedvr/upscale/video") == "upscaled"
    assert fake.endpoints() == ["fal-ai/seedvr/upscale/video"] and len(fake.uploads) == 1
    body = fake.submits[0][1]
    assert validate("fal-ai/seedvr/upscale/video", body) == [] and body["target_resolution"] == "1080p"


# ---------- tüm sahne istemi: kadro ve kamera ----------

def test_the_scene_cast_includes_living_props_and_shot_rosters_stay_on_their_line(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    prompt = plan_for(ids["S001_SH01"])["body"]["prompt"]
    assert "The cast of this scene: Nora, Pip and the Hens." in prompt
    assert "unpopulated" not in prompt and "only this scene's cast (Nora, Pip and the Hens) is present" in prompt
    shot3 = prompt[prompt.index("Shot 3"):prompt.index("Visual constraints")]
    assert "Only Nora and Pip in frame; the Hens are out of frame." in shot3
    shot1 = prompt[prompt.index("Shot 1"):prompt.index("Shot 2")]
    assert "the Hens are out of frame" not in shot1                       # sürü 1. çekimde bastırılmaz
    assert "°" not in prompt and "main axis" not in prompt


def test_degrees_become_plain_camera_words_and_a_repeated_camera_sentence_is_dropped():
    from app.engine.scene_parts import plain_camera
    assert plain_camera({"shot": "close_up", "angle": "low", "side": -45, "lens_mm": 50}) == \
        "close-up, low at ground level, from slightly left, 50 mm lens"
    assert plain_camera({"shot": "medium", "side": 90}) == "medium shot, from the right side"
    assert plain_camera({"shot": "over_shoulder", "side": 180}) == "over-the-shoulder shot, from behind"


def test_a_too_long_prompt_trims_constraints_never_the_shots(monkeypatch):
    from app.engine import scene_parts
    monkeypatch.setitem(scene_parts.PROMPT_LIMIT, "h3max", 80)
    shots = ["Shot 1 (0-3s): Nora feeds the hens in the farmyard at dawn."]
    prompt, notes = scene_parts.fit_length("h3max", ["Style.", "Style."], shots, [], ["sharp focus", "steady motion"])
    assert shots[0] in prompt and prompt.count("Style.") == 1 and "Visual constraints" not in prompt and notes
