"""ÜCRETLİ üretim yolları (Faz 18), sahte fal ve sahte MiniMax H3 sunucusuyla ama gerçek veritabanı, nesne deposu ve
FFmpeg ile. Varsayılan motor H3'tür (kendi GPU sunucumuz); fal yolunu (önce görsel: Flux karesi, sonra video) sınayan
testler çekimi Seedance'e alır (`use_fal`): yönetmenin Seedance seçtiği çekimler bu yoldan geçer.

Sahte fal gerçek medya döndürür: Flux kareleri gerçek görseldir (istenen sayıda kare bilerek siyahtır), videolar
prosedürel motorla çizilir. Hiçbir test gerçek servise istek göndermez (`no_network`); kilit yalnızca bellekteki ayar
nesnesinde açılır. Üretim testlerinde gerçek kalite skorlayıcısının yerine seed'e bağlı sahte skor kullanılır (gerçek
skorlayıcının kendi testleri: test_scorer.py); skorlayıcı kullanılamıyorsa ücretli üretim para harcamadan durur.
"""
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select, text

from app import storage
from app.budget import pricing
from app.config import project_settings, settings
from app.core.state import load_assets, load_characters
from app.db import session_scope
from app.engine import methods, motion_test, references
from app.engine.iteration import compose_for_scene, run_tiered_generation
from app.engine.procedural import render_clip
from app.engine.storyboard import BOARD_H, BOARD_W, make_board
from app.live import PaidCallBlocked, fal_video
from app.models import Asset, Project, Revision, Scene, Variation
from app.quality import anomaly, gate
from app.style import bible
from app.style.bible import EXAMPLE
from app.style.composer import style_prefix
from tests.fal_fake import FakeFal

VIDEO_APPS = (fal_video.WAN, fal_video.SEEDANCE)


# ---------- tek kare kalite ölçütleri (veritabanı gerekmez) ----------

def _save(tmp_path, name, pixels):
    path = str(tmp_path / name)
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)).save(path)
    return path


def test_a_black_or_flat_frame_is_a_defect_and_a_real_picture_is_not(tmp_path):
    rng = np.random.default_rng(1)
    black = _save(tmp_path, "black.png", np.full((90, 160, 3), 8))
    flat = _save(tmp_path, "flat.png", np.full((90, 160, 3), 128))
    picture = _save(tmp_path, "pic.png", rng.integers(0, 255, (90, 160, 3)))
    (tmp_path / "bad.jpg").write_bytes(b"not an image")
    assert [a["type"] for a in anomaly.detect_frame_anomalies(black)] == ["black"]
    assert [a["type"] for a in anomaly.detect_frame_anomalies(flat)] == ["flat"]
    assert anomaly.detect_frame_anomalies(picture) == []
    assert [a["type"] for a in anomaly.detect_frame_anomalies(str(tmp_path / "bad.jpg"))] == ["unreadable"]


def test_a_frame_is_scored_against_the_same_threshold_as_a_video(tmp_path, monkeypatch):
    picture = _save(tmp_path, "pic.png", np.random.default_rng(2).integers(0, 255, (90, 160, 3)))
    monkeypatch.setattr(settings, "dry_run_score_sd", 0.0)
    monkeypatch.setattr(settings, "dry_run_score_mean", 0.80)
    low = gate.score_frame(picture, 9, {"clip_threshold": 0.85})
    assert not low.passed and [a["type"] for a in low.anomalies] == ["low_clip"]
    assert gate.score_frame(picture, 9, {"clip_threshold": 0.5}).passed


# ---------- sahte fal (gerçek medya) ----------

@pytest.fixture
def studio(monkeypatch, tmp_path, no_network):
    """Sahte fal: Flux karesinin adresi seed'i taşır; ilk `black_frames` kare siyahtır, gerisi gerçek bir resimdir.
    Video, video-to-video ve dudak senkronu prosedürel klip döndürür."""
    fake = FakeFal()
    fake.black_frames = 0
    fake.frames = []
    fake.behaviour[fal_video.FLUX] = lambda args: {"images": [{"url": f"https://fal.test/frame/{args['seed']}.jpg"}]}
    monkeypatch.setitem(sys.modules, "fal_client", fake.module())
    fake.downloads = []

    def download(url, suffix=".mp4"):
        n = len(fake.downloads)
        fake.downloads.append(url)
        path = str(tmp_path / f"fal{n}{suffix}")
        if "/frame/" in url:
            fake.frames.append(url)
            if len(fake.frames) <= fake.black_frames:
                pixels = np.zeros((90, 160, 3))
            else:
                pixels = np.random.default_rng(n).integers(0, 255, (90, 160, 3))
            Image.fromarray(np.asarray(pixels, dtype=np.uint8)).save(path, "JPEG")
        else:
            render_clip(path, n, 160, 90, 3.0, 8, emotion="neutral", camera={"shot": "wide", "movement": "pan"},
                        characters=["Kerem"])
        return path

    monkeypatch.setattr(fal_video, "_download", download)
    return fake


def use_fal(*sids, engine="seedance"):
    """Çekimi fal.ai yoluna alır, yönetmenin panelde Seedance seçmesi gibi."""
    with session_scope() as s:
        for sid in sids:
            s.get(Scene, sid).engine = engine


def go_paid(monkeypatch):
    """Kilidi yalnızca bellekte açar (ortam DRY_RUN=1 kalır); `studio` genel internete bağlantıyı zaten yasaklar."""
    monkeypatch.setattr(settings, "dry_run", False)
    for name in ("fal_key", "anthropic_api_key", "elevenlabs_api_key"):
        monkeypatch.setattr(settings, name, f"test-{name}")


def fake_scorer(monkeypatch):
    """Gerçek skorlayıcının yerine seed'e bağlı sahte skor (yalnızca testte; uygulama ücretli modda bunu reddeder)."""
    monkeypatch.setattr(settings, "quality_scorer", "dry")
    monkeypatch.setattr(gate, "ensure_scorer", lambda: None)


def ledger(pid):
    with session_scope() as s:
        rows = s.execute(text("SELECT provider, kind, amount_usd FROM cost_ledger WHERE project_id = :p ORDER BY id"),
                         {"p": pid}).all()
    return [(p, k, float(a)) for p, k, a in rows]


def commits(pid, prefix):
    return [a for p, k, a in ledger(pid) if k == "commit" and p.startswith(prefix)]


def variations(sid):
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.scene_id == sid)).all()
        s.expunge_all()
        return rows


def activate_example_style(pid):
    with session_scope() as s:
        v = bible.create(s, pid, **EXAMPLE).version
        bible.activate(s, pid, v)
    with session_scope() as s:
        return style_prefix(bible.active(s, pid))


def composed_for(pid, sid):
    with session_scope() as s:
        sc = s.get(Scene, sid)
        return compose_for_scene(sc, load_characters(s, sid), load_assets(s, sid), bible.active(s, pid))


# ---------- önce görsel, sonra video ----------

def test_paid_generation_judges_each_first_frame_before_paying_for_its_video(sample_scene, studio, monkeypatch):
    pid, codes = sample_scene(probe_n=4, full_n=4, wave_size=4, refined_k=2, max_correction_rounds=0)
    sid = codes["S001_SH01"]
    use_fal(sid)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    studio.black_frames = 2
    assert run_tiered_generation(pid, sid) == "refined"

    rows = variations(sid)
    rejected = [v for v in rows if v.params.get("stage") == "first_frame"]
    made = [v for v in rows if v.params.get("stage") != "first_frame"]
    assert len(rejected) == 2 and len(made) == len(rows) - 2
    for v in rejected:                                             # siyah kare: video hiç istenmedi
        assert (v.status, v.params["purge_reason"], v.video_key) == ("purged", "quality_gate", None)
        assert [a["type"] for a in v.anomalies] == ["black"] and storage.exists(v.params["quarantine_key"])
        assert float(v.cost_usd) == fal_video.image_estimate() and v.keyframe_key
    video_calls = [a for app, a, _ in studio.calls if app in VIDEO_APPS]
    assert len(video_calls) == len(made)
    assert sorted(a["image_url"] for a in video_calls) == sorted(f"https://fal.test/frame/{v.seed}.jpg" for v in made)
    engine = made[0].engine
    provider = fal_video.FalProvider(engine)
    for v in made:                                                 # bedel: kare + yalnızca video
        video = provider.estimate(SimpleNamespace(image_url="x", resolution=v.params["resolution"],
                                                  duration_s=v.params["duration_s"]))
        assert float(v.cost_usd) == round(fal_video.image_estimate() + video, 4)
        assert storage.exists(v.params["first_frame_key"])
    assert len(commits(pid, "flux_lora:first_frame")) == len(rows)
    assert len(commits(pid, f"{engine}:")) == len(made)
    with session_scope() as s:
        p = s.get(Project, pid)
        assert float(p.reserved_usd) == 0 and abs(float(p.spent_usd) - sum(float(v.cost_usd) for v in rows)) < 1e-6


def test_a_video_that_keeps_failing_returns_its_reservations_but_keeps_the_paid_frame(sample_scene, studio, monkeypatch):
    from tests.fal_fake import HTTPError
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0)
    sid = codes["S001_SH01"]
    use_fal(sid)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    studio.behaviour[fal_video.WAN] = studio.behaviour[fal_video.SEEDANCE] = lambda args: HTTPError(503)
    assert run_tiered_generation(pid, sid) == "needs_review"
    rows = variations(sid)
    assert len(rows) == 2 and all(v.status == "failed" and float(v.cost_usd) == fal_video.image_estimate() for v in rows)
    engine = rows[0].engine
    kinds = [k for p, k, _ in ledger(pid) if p.startswith(f"{engine}:")]
    assert kinds.count("commit") == 0 and kinds.count("reserve") == kinds.count("release") == 2 * 3
    assert len(commits(pid, "flux_lora:first_frame")) == 2
    with session_scope() as s:
        p = s.get(Project, pid)
        assert float(p.reserved_usd) == 0 and float(p.spent_usd) == round(2 * fal_video.image_estimate(), 4)


def test_without_a_real_scorer_paid_generation_stops_before_any_spend(sample_scene, studio, monkeypatch):
    from app.quality import scorer
    pid, codes = sample_scene()
    go_paid(monkeypatch)
    monkeypatch.setattr(scorer, "unavailable", lambda: "model dosyası yok")
    with pytest.raises(NotImplementedError, match="kalite skoru"):
        run_tiered_generation(pid, codes["S001_SH01"])
    assert studio.calls == [] and ledger(pid) == [] and variations(codes["S001_SH01"]) == []


def test_with_the_real_scorer_noise_frames_are_rejected_before_any_video_is_paid_for(sample_scene, studio, monkeypatch):
    from app.quality import scorer
    if scorer.unavailable():
        pytest.skip(f"gerçek skorlayıcı yok: {scorer.unavailable()}")
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0)
    sid = codes["S001_SH01"]
    use_fal(sid)
    go_paid(monkeypatch)                                          # QUALITY_SCORER=auto: ücretli modda gerçek skor
    assert run_tiered_generation(pid, sid) == "needs_review"     # sahte fal'ın kareleri gürültü
    rows = variations(sid)
    assert len(rows) == 2 and all(v.params.get("stage") == "first_frame" for v in rows)
    assert all("low_clip" in [a["type"] for a in v.anomalies] for v in rows)
    assert all(v.params["frame_score_detail"]["scorer"] == scorer.SCORER_ID for v in rows)
    assert not [app for app, _, _ in studio.calls if app in VIDEO_APPS]       # hiçbir video ödenmedi
    assert {p for p, k, _ in ledger(pid) if k == "commit"} == {"flux_lora:first_frame:480p"}


# ---------- storyboard ----------

def test_the_paid_storyboard_frame_is_one_flux_image_of_the_composed_prompt(sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    sid = codes["S002_SH01"]
    prefix = activate_example_style(pid)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)                                      # kare, videoyla aynı kalite kapısından geçer
    key = make_board(pid, sid, standalone=True)
    flux = [a for app, a, _ in studio.calls if app == fal_video.FLUX]
    assert len(flux) == 1 and flux[0]["prompt"] == composed_for(pid, sid).prompt and flux[0]["prompt"].startswith(prefix)
    board = np.asarray(Image.open(storage.download(key, str(tmp_path / "b.jpg"))).convert("L"), dtype=float)
    assert board.shape == (BOARD_H, BOARD_W) and board[-10:].mean() < board[:200].mean()      # alt bantta çekim bilgisi
    assert commits(pid, "flux_lora:storyboard") == [pricing.estimate_image(1.0)]
    make_board(pid, sid, standalone=True)                                                    # içerik-adresli: yeniden ödenmez
    assert len(studio.calls) == 1


def test_without_a_fal_key_the_paid_storyboard_is_drawn_locally_for_free(sample_scene, studio, monkeypatch):
    pid, codes = sample_scene()
    go_paid(monkeypatch)
    monkeypatch.setattr(settings, "fal_key", "")                 # yalnızca MiniMax H3: fal.ai isteğe bağlı
    key = make_board(pid, codes["S001_SH01"], standalone=True)
    assert storage.exists(key) and studio.calls == [] and ledger(pid) == []


# ---------- previs rehberi ----------

def test_a_paid_previs_guide_is_restyled_with_the_composed_prompt(sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    activate_example_style(pid)
    guide = render_clip(str(tmp_path / "previs.mp4"), 4, 320, 180, 3.0, 8, emotion="neutral",
                        camera={"shot": "wide", "movement": "tracking"}, characters=["Kerem"])
    storage.put_file(guide, f"guides/{sid}/previs.mp4")
    with session_scope() as s:
        sc = s.get(Scene, sid)
        sc.method, sc.guide_key, sc.guide_hash = "previs_guide", f"guides/{sid}/previs.mp4", "abc"
        style = bible.active(s, pid)
        s.expunge(style)
    go_paid(monkeypatch)
    assert methods.run_guide(pid, sid, "vh", style) == "refined"
    (app, args, _), = studio.calls
    assert app == fal_video.WAN_V2V and args["prompt"] == composed_for(pid, sid).prompt
    assert (args["strength"], args["num_frames"], args["video_url"]) == (fal_video.V2V_STRENGTH, 48, "https://fal.test/upload/1")
    assert commits(pid, "v2v:") == [methods.restyle_estimate(project_settings({})["full_resolution"], 3.0)]
    winner = next(v for v in variations(sid) if v.refined_rank == 1)
    assert winner.params["source"] == "previs_guide" and winner.external_request_id == "req-1"
    assert float(winner.cost_usd) == commits(pid, "v2v:")[0] and storage.exists(winner.video_key)


# ---------- referanslar ve hareket testi ----------

def asset_named(pid, name):
    with session_scope() as s:
        a = s.scalar(select(Asset).where(Asset.project_id == pid, Asset.name == name))
        s.expunge(a)
        return a


def test_a_paid_character_reference_is_an_h3_turnaround(api, sample_scene, studio, h3, monkeypatch):
    pid, _ = sample_scene()
    kerem = asset_named(pid, "Kerem")
    r = api.post(f"/api/assets/{kerem.id}/references/placeholder")
    assert r.status_code == 201, r.text
    assert studio.calls == []                                                          # fal.ai çağrılmadı
    (job,) = h3.submits
    assert job["task"] == "t2va" and "orbits 360 degrees" in job["prompt"]
    assert (job["target"]["short_edge"], job["seconds"]) == (480, 4)
    assert commits(pid, "h3:480p:reference") == [pricing.estimate_gpu("h3", h3.inference_time_s)]   # ölçülen GPU süresi
    turnaround = asset_named(pid, "Kerem").references["turnaround"]
    assert len(turnaround["frames"]) == references.SAMPLE_FRAMES and turnaround["key"].endswith(".mp4")


def test_a_character_with_a_still_turns_around_from_that_still(api, sample_scene, h3, monkeypatch, tmp_path):
    pid, _ = sample_scene()
    kerem = asset_named(pid, "Kerem")
    still = tmp_path / "still.png"
    Image.new("RGB", (320, 180), (30, 140, 200)).save(still)
    assert api.post(f"/api/assets/{kerem.id}/references?kind=still&filename=still.png", content=still.read_bytes()).status_code == 201
    assert api.post(f"/api/assets/{kerem.id}/references/placeholder").status_code == 201
    (job,) = h3.submits
    assert job["task"] == "fl2va" and job["conditions"][0]["role"] == "keyframe"
    assert job["conditions"][0]["uri"].startswith("data:image/jpeg;base64,")


def test_a_paid_location_reference_is_a_single_flux_still(api, sample_scene, studio, monkeypatch):
    pid, _ = sample_scene()
    place = asset_named(pid, "Çöken Köprü")
    go_paid(monkeypatch)
    assert api.post(f"/api/assets/{place.id}/references/placeholder").status_code == 201
    assert [app for app, _, _ in studio.calls] == [fal_video.FLUX] and "no people" in studio.calls[0][1]["prompt"]
    assert commits(pid, "flux_lora:reference") == [pricing.estimate_image(1.0)]
    assert asset_named(pid, "Çöken Köprü").references["still"]["key"].endswith(".jpg")


def test_a_blocked_paid_reference_answers_409_and_spends_nothing(api, sample_scene, studio, monkeypatch):
    pid, _ = sample_scene()
    go_paid(monkeypatch)                                          # H3 anahtarı yok: karakter dönüşü üretilemez
    r = api.post(f"/api/assets/{asset_named(pid, 'Kerem').id}/references/placeholder")
    assert r.status_code == 409 and "RUNPOD_API_KEY" in r.text and studio.calls == [] and ledger(pid) == []
    monkeypatch.setattr(settings, "fal_key", "")                 # mekân görseli Flux'tan: fal anahtarı gerekir
    r = api.post(f"/api/assets/{asset_named(pid, 'Çöken Köprü').id}/references/placeholder")
    assert r.status_code == 409 and "FAL_KEY" in r.text and studio.calls == [] and ledger(pid) == []


def test_a_paid_motion_test_starts_from_the_approved_reference(api, sample_scene, studio, h3, monkeypatch):
    pid, _ = sample_scene()
    place = asset_named(pid, "Çöken Köprü")
    monkeypatch.setattr(settings, "dry_run", True)
    assert api.post(f"/api/assets/{place.id}/references/placeholder").status_code == 201        # ücretsiz taslak
    monkeypatch.setattr(settings, "dry_run", False)
    with session_scope() as s:
        a = s.get(Asset, place.id)
        vid = str(motion_test.create(s, a, project_settings(s.get(Project, pid).settings)).id)
    assert motion_test.run(pid, vid) == "passed"
    assert studio.calls == []                                                                    # Flux çağrılmadı
    (job,) = h3.submits                                                                          # onaylı görselden başlar
    assert job["task"] == "fl2va" and job["conditions"][0]["uri"].startswith("data:image/jpeg;base64,")
    assert commits(pid, "h3:") == [pricing.estimate_gpu("h3", h3.inference_time_s)]


# ---------- elle düzeltme: düzenlenmiş ilk kare ----------

def test_a_paid_first_frame_revision_regenerates_the_clip_from_the_edited_frame(api, sample_scene, studio, monkeypatch,
                                                                               tmp_path):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    use_fal(sid)
    assert run_tiered_generation(pid, sid) == "refined"                    # ücretsiz modda üretildi
    winner = next(v for v in variations(sid) if v.refined_rank == 1)
    monkeypatch.setattr("app.api.plan.build_chain", lambda *a, **k: SimpleNamespace(apply_async=lambda: None))
    go_paid(monkeypatch)
    still = tmp_path / "f.png"
    Image.new("RGB", (160, 90), (240, 20, 20)).save(still)
    r = api.post(f"/api/variations/{winner.id}/revisions?kind=first_frame&filename=f.png", content=still.read_bytes(),
                 headers={"content-type": "image/png"})
    assert r.status_code == 201, r.text
    assert r.json()["stages"] == ["LIPSYNC", "MUX"] and r.json()["revision"]["kind"] == "first_frame"
    (app, args, _), = studio.calls
    assert app in VIDEO_APPS and args["image_url"] == "https://fal.test/upload/1" and args["prompt"] == winner.params["prompt"]
    assert args.get("seed", winner.seed) == winner.seed
    with session_scope() as s:
        rev = s.get(Revision, r.json()["revision"]["id"])
        assert storage.exists(rev.video_key) and storage.exists(rev.source_key)
    provider = fal_video.FalProvider(winner.engine)
    assert commits(pid, f"{winner.engine}:{winner.params['resolution']}:revision") == [provider.estimate(SimpleNamespace(
        image_url="x", resolution=winner.params["resolution"], duration_s=winner.params["duration_s"]))]
    again = api.post(f"/api/variations/{winner.id}/revisions?kind=first_frame&filename=f.png", content=still.read_bytes(),
                     headers={"content-type": "image/png"})
    assert again.json()["revision"]["id"] == r.json()["revision"]["id"] and len(studio.calls) == 1   # aynı kare: yeniden ödenmez


def test_a_paid_first_frame_revision_without_a_key_answers_409(api, sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    run_tiered_generation(pid, sid)
    winner = next(v for v in variations(sid) if v.refined_rank == 1)
    spent = len(ledger(pid))
    go_paid(monkeypatch)
    monkeypatch.setattr(settings, "fal_key", "")
    still = tmp_path / "f.png"
    Image.new("RGB", (160, 90), (240, 20, 20)).save(still)
    r = api.post(f"/api/variations/{winner.id}/revisions?kind=first_frame&filename=f.png", content=still.read_bytes(),
                 headers={"content-type": "image/png"})
    assert r.status_code == 409 and studio.calls == [] and len(ledger(pid)) == spent
    with session_scope() as s:
        assert s.scalar(select(Revision).where(Revision.variation_id == winner.id)) is None


def test_a_paid_first_frame_revision_of_an_h3_clip_is_an_fl2va_job(api, sample_scene, h3, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    sid = codes["S001_SH01"]
    monkeypatch.setattr(settings, "dry_run", True)
    assert run_tiered_generation(pid, sid) == "refined"                    # ücretsiz modda, H3 motoruyla
    monkeypatch.setattr(settings, "dry_run", False)
    winner = next(v for v in variations(sid) if v.refined_rank == 1)
    assert winner.engine == "h3"
    monkeypatch.setattr("app.api.plan.build_chain", lambda *a, **k: SimpleNamespace(apply_async=lambda: None))
    still = tmp_path / "f.png"
    Image.new("RGB", (160, 90), (240, 20, 20)).save(still)
    r = api.post(f"/api/variations/{winner.id}/revisions?kind=first_frame&filename=f.png", content=still.read_bytes(),
                 headers={"content-type": "image/png"})
    assert r.status_code == 201, r.text
    (job,) = h3.submits
    assert job["task"] == "fl2va" and job["prompt"].startswith(winner.params["prompt"]) and job["seed"] == winner.seed
    assert commits(pid, f"h3:{winner.params['resolution']}:revision") == [pricing.estimate_gpu("h3", h3.inference_time_s)]
