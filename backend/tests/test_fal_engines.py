"""fal.ai video motorları (MiniMax H3 Max taslak, Seedance 2.5 final) ve fal.ai'de kare çizimi.

Hiçbir test fal.ai'ye gitmez: ücretli yol sahte kuyruk istemcisiyle (tests/fal_queue_fake.py) ve `no_network` altında
çalışır. Gönderilen her gövde fal'in OpenAPI tanımıyla (tests/data/fal_openapi/, 2026-10-12'de indirildi) doğrulanır.
Fikstür: yönetmenin ücretli karşılaştırma testindeki iki istek (tests/data/fal_test_requests.json).
"""
import json
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.budget import pricing
from app.config import settings
from app.db import session_scope
from app.engine import fal_plan
from app.models import Character, Scene, Variation
from app.style.composer import Composed
from tests.fal_queue_fake import FakeQueue, HTTPError, validate

REQUESTS = json.loads((pathlib.Path(__file__).parent / "data" / "fal_test_requests.json").read_text())
URLS = REQUESTS["seedance25_reference"]["body"]["image_urls"]


@pytest.fixture
def fal_defaults(monkeypatch):
    """fal.ai motorlarının varsayılanları (taslak H3 Max, final Seedance 2.5, kareler fal.ai'de, RunPod kapalı), ama
    kare yolu: VIDEO_INPUT_MODE=keyframe, SCENE_RENDER_MODE=per_shot (bu dosya o yolu sınar; referans modu ve tüm sahne
    tests/test_reference_mode.py'de). conftest eski testler için H3/RunPod'u sabitler."""
    for name in ("h3_platform", "keyframe_platform", "video_draft_engine", "video_final_engine"):
        monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)
    monkeypatch.setattr(settings, "video_input_mode", "keyframe")
    monkeypatch.setattr(settings, "scene_render_mode", "per_shot")


# ---------- referanslar, istem ve gövde (veritabanı yok) ----------

def _cast():
    """Testteki 5 referans: kız, görevli, polis (karakter), havalimanı, otoyol (mekân)."""
    chars = [SimpleNamespace(name=n, reference_images=[f"assets/{n}/still/a.png"], asset_id=None, description=d)
             for n, d in (("Nora", "a red-haired girl in a yellow raincoat"), ("Attendant", "an airport attendant"),
                          ("Officer", "a police officer"))]
    places = [SimpleNamespace(id=i, type="location", name=n, status="draft", description=d,
                              references={"still": {"key": f"assets/{n}/still/a.png"}})
              for i, (n, d) in enumerate((("Airport", "an airport terminal"), ("Highway", "a highway at dusk")))]
    scene = SimpleNamespace(prompt_spec={"prompt": "Nora walks to the security desk."}, description="", h3_mode=None,
                            keyframe_key=None, board_key=None, video_opts=None, duration_s=10.0)
    return scene, chars, places


def _plan(engine):
    scene, chars, places = _cast()
    refs = fal_plan.references(scene, chars, places, strict=False)
    composed = Composed(prompt="Shot 1 (0-3s): Nora walks to the security desk at the airport. Shot 2 (3-5s): the "
                               "attendant reaches out a hand.", negative="blurry, extra people", prefix="")
    style = SimpleNamespace(lighting_prefix="Late-afternoon golden-hour lighting", motion_style="cartoony motion")
    prompt, _ = fal_plan.build_prompt(engine, "ref", composed, refs, style, [c.name for c in chars])
    # ücretli karşılaştırma testindeki ayarlar (768P / 720p, taslak kapalı); varsayılanlar artık en ucuz (480P, taslak)
    endpoint, body = fal_plan.body(engine, "ref", prompt=prompt, duration_s=10.0,
                                   resolution_={"h3max": "768P", "seedance25": "720p"}[engine], audio_=True, seed=7,
                                   reference_urls=URLS)
    return refs, endpoint, body


def test_references_are_numbered_characters_props_locations_in_the_prompt_order():
    refs, _, _ = _plan("h3max")
    assert [(r.number, r.kind, r.name) for r in refs] == [
        (1, "character", "Nora"), (2, "character", "Attendant"), (3, "character", "Officer"),
        (4, "location", "Airport"), (5, "location", "Highway")]


def test_seedance_body_uses_at_image_tokens_a_string_duration_and_validates_against_the_schema():
    refs, endpoint, body = _plan("seedance25")
    fixture = REQUESTS["seedance25_reference"]
    assert endpoint == fixture["endpoint"] == settings.fal_seedance25_ref_endpoint
    for i, r in enumerate(refs, start=1):
        assert f"{r.name}" in body["prompt"] and f"@Image{i}" in body["prompt"]
    assert "Nora is @Image1: a red-haired girl in a yellow raincoat." in body["prompt"]
    assert "The location Airport is @Image4: an airport terminal." in body["prompt"]
    assert "Use @Image4 only for the look of the place (architecture, materials, colors, light)" in body["prompt"]
    assert "[Image" not in body["prompt"] and "Image 1" not in body["prompt"]
    assert body["duration"] == "10" and isinstance(body["duration"], str)          # Seedance: METİN
    assert body["image_urls"] == URLS and body["resolution"] == "720p" and body["aspect_ratio"] == "16:9"
    assert set(body) <= set(fixture["body"]) | {"seed", "task"}                       # testteki alanlar + tohum, görev
    assert validate(endpoint, body) == [] and validate(endpoint, fixture["body"]) == []
    assert body["prompt"].startswith("Late-afternoon golden-hour lighting. cartoony motion. Render every environment "
                                     "in the same 3D animated style as the characters.")
    assert "Visual constraints:" in body["prompt"] and "blurry" not in body["prompt"]   # negatif istem alanı yok


def test_h3max_body_uses_plain_image_tokens_a_number_duration_and_the_required_expansion_mode():
    refs, endpoint, body = _plan("h3max")
    fixture = REQUESTS["h3max_reference"]
    assert endpoint == fixture["endpoint"] == settings.fal_h3max_ref_endpoint
    assert [f"Image {i}" in body["prompt"] for i in range(1, 6)] == [True] * 5
    assert "@Image" not in body["prompt"] and "[Image" not in body["prompt"]
    assert body["duration"] == 10.0 and isinstance(body["duration"], float)         # H3 Max: SAYI
    assert body["prompt_expansion_mode"] == "disabled" and body["resolution"] == "768P"
    assert body["reference_image_urls"] == URLS and body["aspect_ratio"] == "16:9"
    assert set(body) <= set(fixture["body"]) | {"seed"}
    assert validate(endpoint, body) == [] and validate(endpoint, fixture["body"]) == []


def test_bracket_and_at_tokens_written_by_the_director_are_accepted_and_normalized():
    text_ = "[Image1] meets @Image2 near Image 3."
    assert fal_plan.normalize_refs("seedance25", text_) == "@Image1 meets @Image2 near @Image3."
    assert fal_plan.normalize_refs("h3max", text_) == "Image 1 meets Image 2 near Image 3."


def test_image_to_video_bodies_follow_each_schema():
    kw = dict(prompt="p", duration_s=3.0, audio_=False, seed=5, image_url="https://x/a.png", end_image_url="https://x/b.png")
    ep, sd = fal_plan.body("seedance25", "i2v", resolution_="720p", **kw)
    assert ep == settings.fal_seedance25_i2v_endpoint and sd["aspect_ratio"] == "auto" and "seed" not in sd
    assert sd["duration"] == "4" and sd["end_image_url"] == "https://x/b.png" and sd["generate_audio"] is False
    ep2, h3 = fal_plan.body("h3max", "i2v", resolution_="768P", **kw)
    assert ep2 == settings.fal_h3max_i2v_endpoint and "aspect_ratio" not in h3 and h3["duration"] == 3.0
    assert (h3["image_url"], h3["end_image_url"], h3["seed"]) == ("https://x/a.png", "https://x/b.png", 5)
    assert validate(ep, sd) == [] and validate(ep2, h3) == []


def test_duration_types_and_bounds():
    assert [fal_plan.request_seconds("seedance25", d) for d in (3.0, 10.0, 10.2, 40)] == ["4", "10", "11", "30"]
    assert [fal_plan.request_seconds("h3max", d) for d in (0.5, 10.0, 20)] == [0.92, 10.0, 15.0]


def test_mode_is_image_to_video_with_an_approved_frame_and_reference_otherwise():
    scene, chars, places = _cast()
    refs = fal_plan.references(scene, chars, places, strict=False)
    mode, why = fal_plan.choose_mode(scene, "storyboard/x_clean.jpg", "board", refs)
    assert mode == "i2v" and "onaylı storyboard karesi" in why
    mode, why = fal_plan.choose_mode(scene, None, None, refs)
    assert mode == "ref" and "5 referans" in why
    scene.h3_mode = "ref2va"
    assert fal_plan.choose_mode(scene, "k", "upload", refs)[0] == "ref"
    scene.h3_mode = "fl2va"
    assert fal_plan.choose_mode(scene, None, None, refs)[0] == "i2v"


# ---------- maliyet ----------

def test_seedance_estimate_for_the_test_request_is_within_25_percent_of_the_bill():
    bill = REQUESTS["seedance25_reference"]["billed"]
    c = fal_plan.cost("seedance25", "720p", 10.0, images=5)
    assert c["tokens"] == bill["formula_tokens"] == 216000                         # görsel referansı ücretsiz
    assert bill["usd"] * 0.75 <= c["reserve_usd"] <= bill["usd"] * 1.25
    assert c["reserve_usd"] == round(216000 / 1000 * 0.0214 * 1.25, 4)             # %25 pay
    measured = pricing.seedance25_cost("720p", 10.08, size=(1280, 720), measured=True)
    assert abs(measured["usd"] - bill["usd"]) / bill["usd"] < 0.02                 # ölçülen çıktı × billing_factor


def test_seedance_rates_by_resolution_and_video_reference():
    assert pricing.seedance25_cost("1080p", 1)["per_1k_tokens_usd"] == 0.0234
    with_video = pricing.seedance25_cost("720p", 5, in_video_seconds=5)
    assert with_video["per_1k_tokens_usd"] == pytest.approx(0.0214 * 0.6) and with_video["tokens"] == 216000


def test_h3max_estimate_for_the_test_request_is_close_to_the_bill():
    c = fal_plan.cost("h3max", "768P", 10.0, images=5)
    assert 0.80 <= c["usd"] <= 0.90 and c["reserve_usd"] == c["usd"]
    assert c["reference_tokens"] == 5 * 1024 and c["extra_tokens"] == 1024        # 4.096 token dahil
    assert pricing.h3max_cost("480P", 10)["base_usd"] == 0.5 and pricing.h3max_cost("1080P", 10)["base_usd"] == 1.6


def test_tier_resolutions_map_to_each_engines_values():
    assert pricing.estimate_video("h3max", "480p", 4) == pricing.h3max_cost("480P", 4)["usd"]
    assert pricing.estimate_video("h3max", "720p", 4) == pricing.h3max_cost("768P", 4)["usd"]
    assert pricing.estimate_video("seedance25", "768P", 4) == pricing.seedance25_cost("720p", 4)["reserve_usd"]


def test_fal_frame_prices_come_from_the_model_pages(monkeypatch):
    monkeypatch.setattr(settings, "keyframe_platform", "fal")
    assert pricing.estimate_keyframe("h3", model="qwen") == 0.02                     # 1280x704 = 1 MP
    assert pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode="lightning") == 0.03
    assert pricing.estimate_keyframe("h3", model="qwen_edit", angles=True) == 0.035
    assert pricing.estimate_fal_image("qwen_image", 1328, 1328) == 0.04             # 1,76 MP -> 2 MP


# ---------- fal.ai kuyruğu (sahte istemci) ----------

@pytest.fixture
def falq(monkeypatch, tmp_path, paid, s3, fal_defaults):
    """Ücretli mod (yalnızca bellekte) + sahte fal kuyruğu. İndirme gerçek medya üretir (video: prosedürel klip,
    görsel: gürültü resmi); kalite skoru seed'e bağlı sahte skordur."""
    from app.engine.procedural import render_clip
    from app.live import fal_queue, fal_video
    from app.quality import gate
    fake = FakeQueue()
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    monkeypatch.setattr(fal_queue, "POLL_S", 0)
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    monkeypatch.setattr(gate, "ensure_scorer", lambda: None)
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.95)
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    fal_queue._local_uploads.clear()
    r = fal_queue._r()
    for key in r.scan_iter("hg:fal:*"):
        r.delete(key)
    fake.downloads = []

    def download(url, suffix=".mp4"):
        path = str(tmp_path / f"dl{len(fake.downloads)}{suffix}")
        fake.downloads.append(url)
        if suffix == ".mp4":
            render_clip(path, len(fake.downloads), 160, 90, 3.0, 8, emotion="neutral",
                        camera={"shot": "wide", "movement": "pan"}, characters=["Nora"])
        else:
            Image.fromarray(np.random.default_rng(3).integers(0, 255, (704, 1280, 3)).astype(np.uint8)).save(path)
        return path

    monkeypatch.setattr(fal_video, "_download", download)
    yield fake
    for key in r.scan_iter("hg:fal:*"):
        r.delete(key)


def test_a_pending_request_is_resumed_from_redis_and_from_the_scene(falq, mk_project, mk_scene):
    from app.live import fal_queue
    pid = mk_project()
    sid = mk_scene(pid)
    client = fal_queue.client()
    endpoint, args = "minimax/h3-max/reference-to-video", {"prompt": "x", "prompt_expansion_mode": "disabled"}
    job = fal_queue.job_key(endpoint, args)
    fal_queue.remember(job, "fal-req-old", endpoint, sid)              # önceki çalıştırma gönderdi, worker yeniden başladı
    out = fal_queue.run(client, endpoint, args, timeout_s=5, scene_id=sid)
    assert out.resumed and out.request_id == "fal-req-old" and falq.submits == []
    with session_scope() as s:
        assert s.get(Scene, sid).fal_request["status"] == "completed"
    # Redis boşaltıldı: kayıt çekimden okunur
    with session_scope() as s:
        s.get(Scene, sid).fal_request = {"job": job, "request_id": "fal-req-2", "status": "submitted"}
    out = fal_queue.run(client, endpoint, args, timeout_s=5, scene_id=sid)
    assert out.resumed and out.request_id == "fal-req-2" and falq.submits == []


def test_a_422_is_never_retried_and_its_message_is_kept(falq):
    from app.live import fal_queue
    detail = [{"loc": ["body", "duration"], "msg": "Input should be 'auto', '4', '5'", "type": "enum"}]
    falq.behaviour["bytedance/seedance-2.5/reference-to-video"] = [HTTPError(422, detail)]
    with pytest.raises(fal_queue.FalRejected) as e:
        fal_queue.run(fal_queue.client(), "bytedance/seedance-2.5/reference-to-video", {"prompt": "x"}, timeout_s=5)
    assert len(falq.submits) == 1 and e.value.status == 422
    assert "Input should be 'auto', '4', '5'" in str(e.value) and "HTTP 422" in str(e.value)


def test_a_5xx_is_retried_once_and_a_timeout_waits_once_more_then_cancels(falq):
    from app.live import fal_queue
    ep = "minimax/h3-max/image-to-video"
    falq.behaviour[ep] = [HTTPError(503), {"video": {"url": "https://fal.test/v.mp4"}}]
    out = fal_queue.run(fal_queue.client(), ep, {"prompt": "a"}, timeout_s=5)
    assert len(falq.submits) == 2 and out.result["video"]["url"].endswith("v.mp4")
    falq.submits.clear()
    falq.behaviour[ep] = [HTTPError(500), HTTPError(502)]
    with pytest.raises(fal_queue.FalError, match="yeniden deneme de başarısız"):
        fal_queue.run(fal_queue.client(), ep, {"prompt": "b"}, timeout_s=5)
    assert len(falq.submits) == 2                                       # en çok bir yeniden gönderim
    falq.submits.clear()
    falq.states[ep] = ["Queued"] * 1000
    with pytest.raises(fal_queue.FalTimeout):
        fal_queue.run(fal_queue.client(), ep, {"prompt": "c"}, timeout_s=0)
    assert len(falq.submits) == 1 and len(falq.cancels) == 1            # aynı iş beklendi, yeniden gönderilmedi


def test_a_polling_network_error_does_not_resubmit_the_paid_job(falq):
    import httpx
    from app.live import fal_queue
    ep = "minimax/h3-max/image-to-video"
    falq.states[ep] = [httpx.ConnectError("blip"), "InProgress", "Completed"]
    out = fal_queue.run(fal_queue.client(), ep, {"prompt": "d"}, timeout_s=5)
    assert len(falq.submits) == 1 and out.result["video"]


def test_uploads_are_cached_by_sha256_across_processes(falq, tmp_path):
    from app.live import fal_queue
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    client = fal_queue.client()
    url1, sha, reused1 = fal_queue.upload_cached(client, str(a))
    url2, _, reused2 = fal_queue.upload_cached(client, str(b))
    fal_queue._local_uploads.clear()                                    # başka bir süreç: Redis'ten
    url3, _, reused3 = fal_queue.upload_cached(client, str(a))
    assert url1 == url2 == url3 and (reused1, reused2, reused3) == (False, True, True) and len(falq.uploads) == 1
    (tmp_path / "c.png").write_bytes(b"other")
    fal_queue.upload_cached(client, str(tmp_path / "c.png"))
    assert len(falq.uploads) == 2 and len(sha) == 64


# ---------- üretim: taslak H3 Max, "Seedance 2.5 ile yükselt" ----------

def _still(key: str) -> str:
    path = f"/tmp/{key.replace('/', '_')}"
    Image.fromarray(np.random.default_rng(len(key)).integers(0, 255, (64, 64, 3)).astype(np.uint8)).save(path, "PNG")
    storage.put_file(path, key, "image/png")
    return key


def _shot(mk_project, **cfg):
    from app.hermes import rules
    from app.hermes.ingest import ingest
    # ücretli modda zincirli projede kare onayı varsayılan olarak zorunlu olur (ingest); burada reference-to-video sınanır
    pid = mk_project(500.0, **{"lint_strictness": "off", "require_board_approval": False, **cfg})
    ingest(pid, rules.SAMPLE_SCRIPT, auto_music=False)
    with session_scope() as s:
        sc = s.scalars(select(Scene).where(Scene.project_id == pid).order_by(Scene.seq_order)).all()[1]
        for ch in s.scalars(select(Character).where(Character.project_id == pid)):
            ch.reference_images = [_still(f"assets/{pid}/{ch.name}/still/ref.png")]
        return pid, str(sc.id), sc.engine


def _variations(sid):
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.scene_id == sid).order_by(Variation.created_at)).all()
        s.expunge_all()
        return rows


def test_draft_on_h3max_then_upgrade_to_seedance_keeps_the_references_and_the_old_clip(falq, mk_project, api,
                                                                                       dispatched, fast):
    from app.engine.iteration import run_tiered_generation
    pid, sid, engine = _shot(mk_project)
    assert engine == "h3max"                                           # yeni çekimin motoru: taslak
    assert run_tiered_generation(pid, sid, "draft") == "refined", _error(sid)
    assert falq.endpoints() == ["minimax/h3-max/reference-to-video"]
    body = falq.submits[0][1]
    assert validate(falq.endpoints()[0], body) == []
    assert body["reference_image_urls"] and all(u.startswith("https://v3b.fal.media/") for u in body["reference_image_urls"])
    assert "Image 1" in body["prompt"] and body["duration"] == pytest.approx(float(body["duration"]))
    [draft] = _variations(sid)
    assert (draft.engine, draft.tier, draft.status, draft.refined_rank) == ("h3max", "probe", "refined", 1)
    assert storage.exists(draft.video_key) and draft.video_key == f"variations/{sid}/{draft.id}.mp4"   # indirildi, depoda
    meta = draft.params["provider_meta"]
    assert meta["request_id"] == draft.external_request_id and meta["billed"]["units"] > 0
    assert "expanded_prompt" in meta and meta["seed"] == body["seed"] and draft.params["prompt"] == body["prompt"]
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert sc.cut_tier == "draft" and sc.fal_request["status"] == "completed"
        assert sc.fal_request["request_id"] == draft.external_request_id and sc.fal_request["result"]["billed"]
        commit = s.execute(text("SELECT amount_usd FROM cost_ledger WHERE scene_id = :s AND kind = 'commit'"),
                           {"s": sid}).scalars().all()
    assert [float(c) for c in commit] == [meta["billed"]["usd"]]

    uploads = len(falq.uploads)
    r = api.post(f"/api/scenes/{sid}/upgrade-final")
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "seedance25" and "VISUAL" in r.json()["stages"]
    assert run_tiered_generation(pid, sid, "full") == "refined"
    assert falq.endpoints()[-1] == "bytedance/seedance-2.5/reference-to-video"
    final_body = falq.submits[-1][1]
    assert validate(falq.endpoints()[-1], final_body) == []
    assert final_body["image_urls"] == body["reference_image_urls"] and len(falq.uploads) == uploads   # aynı referanslar
    assert "@Image1" in final_body["prompt"] and final_body["duration"] == str(int(final_body["duration"]))
    old, new = _variations(sid)
    assert (old.id, old.status, old.refined_rank) == (draft.id, "passed", None) and storage.exists(old.video_key)
    assert (new.engine, new.tier, new.refined_rank) == ("seedance25", "full", 1)
    with session_scope() as s:
        assert s.get(Scene, sid).cut_tier == "final"
    assert api.post(f"/api/scenes/{sid}/upgrade-final").status_code == 409          # zaten final


def test_a_rejected_request_stops_the_shot_with_the_message_and_spends_nothing(falq, mk_project, fast):
    from app.engine.iteration import run_tiered_generation
    pid, sid, _ = _shot(mk_project)
    falq.behaviour["minimax/h3-max/reference-to-video"] = [HTTPError(422, "reference_image_urls: too many files")]
    assert run_tiered_generation(pid, sid) == "failed"                    # 4xx: hemen failed, yeniden deneme yok
    assert len(falq.submits) == 1
    with session_scope() as s:
        sc = s.get(Scene, sid)
        assert "fal.ai isteği reddetti (HTTP 422" in sc.last_error and "too many files" in sc.last_error
        spent = s.execute(text("SELECT COUNT(*) FROM cost_ledger WHERE scene_id = :s AND kind = 'commit'"),
                          {"s": sid}).scalar()
    assert spent == 0


# ---------- kare çizimi fal.ai'de (Qwen) ----------

def test_qwen_bodies_for_text_chain_compose_and_angles_follow_the_schemas(fal_defaults):
    from app.core.chain import angle_tokens
    from app.engine import cutin
    from app.engine.storyboard import ONCE, chain_prompt, looks_text
    from app.live import fal_qwen
    ep, body = fal_qwen.qwen_image_body("A wide shot of a foundry", 11)
    assert ep == "fal-ai/qwen-image-2512" and body["image_size"] == {"width": 1280, "height": 704}
    assert (body["num_inference_steps"], body["acceleration"]) == (50, "none") and validate(ep, body) == []
    refs = [{"name": "SH01", "key": "k1", "kind": "frame"}, {"name": "Nora", "key": "k2", "kind": "character"}]
    chain = chain_prompt("Nora sits down.", refs, "continuous")                # zincir: Kn, K(n-1)'den
    ep, body = fal_qwen.edit_body(chain, 12, ["https://u/1", "https://u/2"], "blurry", "full")
    assert ep == "fal-ai/qwen-image-edit-2511" and body["image_urls"] == ["https://u/1", "https://u/2"]
    assert body["prompt"] == chain and body["negative_prompt"] == "blurry" and validate(ep, body) == []
    compose = cutin.compose_prompt("Nora at the desk.", refs[1:], looks_text(refs[1:]), ONCE, "")
    ep, body = fal_qwen.edit_body(compose, 13, ["https://u/2"], "", "lightning")
    assert (body["num_inference_steps"], body["acceleration"]) == (10, "high") and validate(ep, body) == []
    tokens = angle_tokens({"shot": "wide", "side": 0}, {"shot": "close_up", "side": -45, "angle": "low"})
    prompt = cutin.angles_prompt(tokens, ["Nora"], [], "Nora looks up.", ONCE)
    ep, body = fal_qwen.edit_body(prompt, 14, ["https://u/1", "https://u/2"], "", "full",
                                  angles={"lora": "x.safetensors", "strength": 0.9})
    assert ep == "fal-ai/qwen-image-edit-2511/lora" and body["prompt"].startswith("<sks> ")
    assert body["loras"] == [{"path": settings.qwen_edit_angles_lora_url, "scale": 0.9}]
    assert body["loras"][0]["path"].startswith("https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA/")
    assert validate(ep, body) == []


def test_a_storyboard_edit_frame_is_drawn_on_fal_with_uploaded_references(falq, mk_project):
    from app.engine import frames
    from app.engine.storyboard import board_source
    pid = mk_project()
    key = _still(f"assets/{pid}/Nora/still/ref.png")
    assert frames.platform() == "fal" and board_source("fl2va", False, "qwen_edit") == "qwen_edit"
    made = frames.edit_keyframe_image("Image 1 is Nora.", 21, [{"name": "Nora", "key": key}], "", pid, "medium")
    assert falq.endpoints() == ["fal-ai/qwen-image-edit-2511"] and len(falq.uploads) == 1
    assert falq.submits[0][1]["image_urls"] == ["https://v3b.fal.media/files/test/1"]
    assert made.cost_usd == 0.03 and pathlib.Path(made.local_path).exists()


# ---------- RunPod kapalı ----------

def test_with_runpod_off_no_runpod_call_is_ever_made(paid, fal_defaults, monkeypatch, mk_project, mk_scene, s3, api):
    from app.budget import killswitch
    from app.engine import providers
    from app.engine.iteration import run_tiered_generation
    from app.live import probe, runpod_h3
    from app.worker import tasks
    calls = []
    for name in ("tick", "balance", "health", "endpoint_config", "cancel_project", "cancel_scenes", "get_live_provider",
                 "keyframe_image", "edit_keyframe_image", "apply_config", "preflight"):
        monkeypatch.setattr(runpod_h3, name, lambda *a, _n=name, **k: calls.append(_n))
    assert settings.h3_platform == "kapali"
    assert tasks.runpod_tick() == "skip" and tasks.h3_idle() == "skip"
    pid = mk_project()
    sid = mk_scene(pid)
    with session_scope() as s:
        s.get(Scene, sid).engine = "h3"
    killswitch.freeze(pid, "test")
    killswitch.resume(pid)
    assert probe.check("runpod", "test-key")["message"].startswith("RunPod: kapalı (endpoint silindi)")
    with pytest.raises(providers.H3Disabled):
        providers.get_provider("h3")
    assert run_tiered_generation(pid, sid) == "needs_review"
    with session_scope() as s:
        assert s.get(Scene, sid).last_error == providers.H3_DISABLED
    assert api.post("/api/settings/runpod", json={"action": "refresh"}).status_code == 409
    card = next(x for x in api.get("/api/settings").json()["services"] if x["id"] == "runpod")
    assert card["needed"] is False and card["not_needed_reason"].startswith("RunPod: kapalı (endpoint silindi)")
    overview = api.get(f"/api/projects/{pid}/overview").json()
    assert next(x for x in overview["scenes"] if x["id"] == sid)["engine_disabled"] is True
    assert calls == []


def test_h3_shots_still_render_locally_in_free_mode(fal_defaults, mk_project, mk_scene, fast):
    from app.engine.iteration import run_tiered_generation
    pid = mk_project(probe_n=1, full_n=1, wave_size=1, refined_k=1)
    sid = mk_scene(pid, status="pending")
    with session_scope() as s:
        s.get(Scene, sid).engine = "h3"
    assert run_tiered_generation(pid, sid) == "refined"


# ---------- kuru çalıştırma ----------

def test_dry_run_returns_the_full_body_without_calling_fal_or_queueing(fal_defaults, mk_project, api, dispatched,
                                                                       no_network, s3):
    pid, sid, _ = _shot(mk_project)
    r = api.post(f"/api/scenes/{sid}/render?dry_run=true")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["dry_run"] is True and plan["network"] is False and dispatched == []
    assert plan["endpoint"] == "minimax/h3-max/reference-to-video" and plan["mode"] == "ref"
    assert "reference-to-video" in plan["reason"] and plan["body"]["prompt_expansion_mode"] == "disabled"
    urls = plan["body"]["reference_image_urls"]
    assert urls and all(u.startswith(f"local:assets/{pid}/") for u in urls)
    assert [x["token"] for x in plan["references"]] == [f"Image {i}" for i in range(1, len(urls) + 1)]
    assert all(len(u["sha256"]) == 64 and u["upload"].startswith("yüklenmedi") for u in plan["uploads"])
    assert plan["cost"]["usd"] > 0 and plan["upgrade"]["engine"] == "seedance25"
    assert plan["upgrade"]["available"] is False and "taslak" in plan["upgrade"]["reason"]   # önce taslak üretilmeli
    with session_scope() as s:
        events = s.execute(text("SELECT COUNT(*) FROM curation_events WHERE scene_id = :s"), {"s": sid}).scalar()
    assert events == 0                                                    # kuru çalıştırma karar değildir
    whole = api.get(f"/api/projects/{pid}/render-plan?dry_run=true").json()
    assert len(whole["scenes"]) == 4 and whole["total_usd"] == pytest.approx(
        sum(x["cost"]["reserve_usd"] for x in whole["scenes"]), abs=1e-3)
    assert whole["engines"] == {"draft": "h3max", "final": "seedance25"} and whole["requests"] == 4


def test_an_approved_board_switches_the_dry_run_to_image_to_video_with_an_end_frame_field(fal_defaults, mk_project,
                                                                                            api, s3):
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.storyboard import clean_key
    pid, sid, _ = _shot(mk_project)
    with session_scope() as s:
        sc = s.get(Scene, sid)
        fp = visual_fingerprint(sc, load_characters(s, sid), load_assets(s, sid), None)
        sc.board_key, sc.board_hash, sc.board_status = f"storyboard/{sid}/{fp}.jpg", fp, "approved"
    _still(clean_key(f"storyboard/{sid}/{fp}.jpg"))
    plan = api.post(f"/api/scenes/{sid}/render?dry_run=true").json()
    assert plan["mode"] == "i2v" and plan["endpoint"] == "minimax/h3-max/image-to-video"
    assert plan["body"]["image_url"] == f"local:{clean_key(f'storyboard/{sid}/{fp}.jpg')}"
    assert "onaylı storyboard karesi" in plan["reason"] and "reference_image_urls" not in plan["body"]


def test_free_mode_draft_and_upgrade_with_the_default_engines(fal_defaults, mk_project, api, dispatched, fast):
    from app.engine.iteration import run_tiered_generation
    pid, sid, engine = _shot(mk_project)
    assert engine == "h3max"
    assert run_tiered_generation(pid, sid, "draft") == "refined"
    [draft] = _variations(sid)
    assert draft.engine == "h3max" and draft.params["resolution"] == "480P"              # en ucuz varsayılan
    # sanal bedel: aynı isteğin fal tahmini (referans görselleri dahil)
    assert float(draft.cost_usd) == fal_plan.cost("h3max", "480P", draft.params["duration_s"], _images(sid))["reserve_usd"]
    assert api.post(f"/api/scenes/{sid}/upgrade-final").status_code == 200
    assert run_tiered_generation(pid, sid, "full") == "refined"
    old, new = _variations(sid)
    assert (old.status, new.engine, new.params["resolution"], new.refined_rank) == ("passed", "seedance25", "480p", 1)


def _images(sid) -> int:
    from app.core.state import load_assets, load_characters
    with session_scope() as s:
        sc = s.get(Scene, sid)
        return len(fal_plan.references(sc, load_characters(s, sid), load_assets(s, sid), strict=False))


def _error(sid) -> str:
    with session_scope() as s:
        sc = s.get(Scene, sid)
        from app.budget import killswitch
        return f"{sc.status}: {sc.last_error} {sc.h3_mode} {killswitch.r().lrange(killswitch.EVENTS.format(sc.project_id), 0, 5)}"


def test_reference_descriptions_are_shortened_without_repeating_the_name():
    long = ("Nora, a slim teenage girl with long straight copper-red hair, big blue eyes, freckles and rosy cheeks, "
            "always in a blue hoodie and red sneakers with white laces. She wears headphones.")
    short = fal_plan.short_description("Nora", long)
    assert not short.startswith("Nora") and len(short) <= fal_plan.SHORT_CHARS and "She wears" not in short
    assert short.startswith("a slim teenage girl with long straight copper-red hair")


def test_project_settings_accept_the_engine_choices_and_refuse_others(api, api_project):
    pid = api_project(draft_engine="seedance25", final_engine=None, fal_clip_attempts=3)
    cfg = api.get(f"/api/projects/{pid}").json()["settings"]
    assert (cfg["draft_engine"], cfg["final_engine"], cfg["fal_clip_attempts"]) == ("seedance25", None, 3)
    r = api.patch(f"/api/projects/{pid}", json={"settings": {"draft_engine": "kling"}})
    assert r.status_code == 422 and "draft_engine" in r.text
    assert api.patch(f"/api/projects/{pid}", json={"settings": {"draft_engine": None}}).status_code == 200
