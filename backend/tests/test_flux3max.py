"""FLUX 3 Max (fal.ai, yalnızca image-to-video): projeye özel motor. Referans görseli almaz; parça yönetmenin yüklediği
başlangıç karesinden (ya da sürekli geçişte önceki klibin son karesinden) üretilir. Hiçbir test fal.ai'ye gitmez;
gövdeler fal OpenAPI tanımıyla (tests/data/fal_openapi/, 2026-10-27) doğrulanır.
"""
import pytest

from app.budget import pricing
from app.config import project_settings, settings
from app.db import session_scope
from app.engine import fal_plan, providers
from app.models import Scene
from tests.fal_queue_fake import validate
from tests.test_reference_mode import _still, nora, plan_for, ref_defaults  # noqa: F401 — fikstür


def test_body_follows_the_schema_with_an_integer_duration_and_no_references():
    ep, body = fal_plan.body("flux3max", "ref", prompt="p", duration_s=3.2, resolution_="720p", audio_=False, seed=9,
                             image_url="https://x/a.png", end_image_url="https://x/b.png",
                             reference_urls=["https://x/r.png"], audio_urls=["https://x/s.wav"])
    assert ep == settings.fal_flux3max_i2v_endpoint == "blackforestlabs/flux-3-max/image-to-video"
    assert body["duration"] == 5 and body["generate_audio"] is False and body["seed"] == 9
    assert body["image_url"] == "https://x/a.png" and body["end_image_url"] == "https://x/b.png"
    assert not any(k in body for k in ("reference_image_urls", "image_urls", "reference_audio_urls"))
    assert validate(ep, body) == []


def test_duration_and_resolution_bounds():
    assert [fal_plan.request_seconds("flux3max", d) for d in (2.0, 7.0, 7.2, 40)] == [5, 7, 8, 20]
    assert [fal_plan.norm_resolution("flux3max", r) for r in ("480p", "720p", "1080p")] == ["720p", "720p", "1080p"]
    assert fal_plan.resolution("flux3max", None, {"flux3max_resolution": "1080p"}) == "1080p"
    assert fal_plan.resolution("flux3max") == "720p"


def test_price_is_per_generated_second():
    assert pricing.fal_video_cost("flux3max", "720p", 10)["usd"] == pytest.approx(0.425)
    assert pricing.fal_video_cost("flux3max", "1080p", 10)["usd"] == pytest.approx(0.725)
    assert fal_plan.cost("flux3max", "720p", 3.0)["usd"] == pytest.approx(5 * 0.0425)    # en kısa 5 sn faturalanır
    pricing.validate_table(open(settings.pricing_file).read())


def test_it_is_never_a_default_engine(ref_defaults):
    defaults = type(settings).model_fields
    assert (defaults["video_draft_engine"].default, defaults["video_final_engine"].default) == ("h3max", "seedance25")
    assert providers.default_engine(project_settings({})) == "h3max"
    assert providers.default_engine(project_settings({"draft_engine": "flux3max"})) == "flux3max"


def test_a_scene_starts_from_the_uploaded_keyframe_and_describes_the_cast(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    frame = _still(f"keyframes/{pid}/S001.png")
    with session_scope() as s:
        s.get(Scene, ids["S001_SH01"]).keyframe_key = frame
    plan = plan_for(ids["S001_SH02"], "flux3max")
    body = plan["body"]
    assert plan["part"] == ["S001_SH01", "S001_SH02", "S001_SH03"] and body["duration"] == 10
    assert plan["mode"] == "i2v" and plan["start_key"] == frame and not plan["blocked"]
    assert body["image_url"] == f"local:{frame}" and validate(plan["endpoint"], body) == []
    assert [u["field"] for u in plan["uploads"]] == ["image_url"]
    prompt = body["prompt"]
    assert "The video opens exactly on the provided first frame" in prompt
    assert "Nora: a slim teenage girl" in prompt and "Image 1" not in prompt and "@Image" not in prompt
    assert plan["cost"]["usd"] == pytest.approx(10 * 0.0425)


def test_without_a_keyframe_the_part_is_blocked(ref_defaults, mk_project, s3):
    pid, ids = nora(mk_project)
    plan = plan_for(ids["S001_SH01"], "flux3max")
    assert plan["blocked"] and plan["missing"][-1]["kind"] == "frame"
    assert any("başlangıç karesi" in w for w in plan["warnings"])


def test_project_setting_accepts_flux3max(api, api_project):
    pid = api_project(draft_engine="flux3max", flux3max_resolution="720p")
    cfg = api.get(f"/api/projects/{pid}").json()["settings"]
    assert (cfg["draft_engine"], cfg["flux3max_resolution"]) == ("flux3max", "720p")
    r = api.patch(f"/api/projects/{pid}", json={"settings": {"flux3max_resolution": "480p"}})
    assert r.status_code == 422
