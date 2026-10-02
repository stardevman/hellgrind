"""Gerçek kalite skorlayıcısı: OpenCLIP stil ve istem uyumu, YuNet + SFace yüz kimliği.

İlk bölüm model gerektirmez (hesap, kip seçimi, kapı). Gerçek modelli bölüm, paketler ve model dosyaları yoksa (CI)
kendiliğinden atlanır; Docker imajında modeller hazırdır. Yüz görselleri kamu malıdır (tests/data/faces/KAYNAK.md).
"""
import pathlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.config import DEFAULT_PROJECT_SETTINGS, settings
from app.engine.procedural import render_clip, render_defect
from app.quality import gate, scorer

CFG = dict(DEFAULT_PROJECT_SETTINGS)
FACES = pathlib.Path(__file__).resolve().parent / "data" / "faces"
BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ---------- hesap (model gerekmez) ----------

def test_identity_is_one_half_at_the_same_person_threshold_and_saturates_around_it():
    assert scorer.identity(scorer.FACE_SAME) == pytest.approx(0.5)
    assert scorer.identity(0.55) > 0.95 and scorer.identity(0.15) < 0.02
    assert scorer.identity(0.40) > scorer.identity(0.35)


def test_alignment_is_the_softmax_of_each_positive_text_against_the_negatives():
    a, b, neg = np.eye(3)
    images = np.stack([a, a])                                   # iki kare, ikisi de ilk olumlu metinle aynı yönde
    texts = np.stack([a, b, neg])                               # iki olumlu, bir olumsuz metin
    first, second = scorer.alignment(images, texts, 2, 100.0)
    assert first > 0.999 and second == pytest.approx(0.5)       # ikinci metin olumsuzla eşit uzaklıkta
    bad = scorer.alignment(np.stack([neg]), texts, 2, 100.0)
    assert max(bad) < 0.001


def test_the_composite_uses_the_face_only_when_a_face_was_measured():
    assert scorer.combine(0.9, None, 0.4) == 0.9
    assert scorer.combine(0.9, 0.5, 0.4) == pytest.approx(0.74)


def test_positive_texts_are_the_style_and_the_shot_and_never_empty():
    assert scorer.positives(scorer.Target(style="  Golden   hour ", content="Köprü çöker.")) == \
        {"style": "Golden hour", "content": "Köprü çöker."}
    assert scorer.positives(scorer.Target()) == {"generic": scorer.GENERIC}


def test_the_target_comes_from_the_style_guide_the_shot_and_the_characters_references():
    scene = SimpleNamespace(prompt_spec={"prompt": "Kerem koşar."})
    chars = [SimpleNamespace(name="Kerem", reference_images=["assets/k/frames/a.jpg"]),
             SimpleNamespace(name="Ayşe", reference_images=[])]
    style = SimpleNamespace(lighting_prefix="Golden hour light.", motion_style="Cartoony motion.")
    t = scorer.target_for(scene, chars, style)
    assert t == scorer.Target(style="Golden hour light. Cartoony motion.", content="Kerem koşar.",
                              faces=(("Kerem", ("assets/k/frames/a.jpg",)),))
    assert scorer.target_for(scene, [], None).style == ""


def test_the_model_files_are_the_ones_the_build_downloads():
    sys.path.insert(0, str(BACKEND / "scripts"))
    try:
        import fetch_models
    finally:
        sys.path.pop(0)
    assert fetch_models.CLIP_MODEL == scorer.CLIP_MODEL
    assert {fetch_models.CLIP_FILE, fetch_models.YUNET_FILE, fetch_models.SFACE_FILE} == \
        {scorer.CLIP_FILE, scorer.YUNET_FILE, scorer.SFACE_FILE} == set(fetch_models.MODELS)
    assert all(len(sha) == 64 for _, sha in fetch_models.MODELS.values())
    assert all("/resolve/main/" not in url and "/raw/main/" not in url for url, _ in fetch_models.MODELS.values())


def test_a_missing_model_file_makes_the_scorer_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "quality_model_dir", str(tmp_path))
    reason = scorer.unavailable()
    assert reason and ("model dosyası yok" in reason or "paketi kurulu değil" in reason)
    with pytest.raises(NotImplementedError, match="Gerçek kalite skoru kullanılamıyor"):
        scorer.ensure_ready()


# ---------- kip seçimi ve kapı ----------

@pytest.mark.parametrize("mode, dry_run, expected", [("auto", True, "dry"), ("auto", False, "real"), ("dry", False, "dry"),
                                                    ("real", True, "real")])
def test_the_scorer_mode_follows_the_setting_and_the_free_mode(monkeypatch, mode, dry_run, expected):
    monkeypatch.setattr(settings, "quality_scorer", mode)
    monkeypatch.setattr(settings, "dry_run", dry_run)
    assert gate.scorer_mode() == expected


def test_an_unknown_scorer_mode_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "quality_scorer", "clip")
    with pytest.raises(ValueError, match="QUALITY_SCORER"):
        gate.scorer_mode()


def test_the_real_scorer_is_ready_before_any_money_is_spent(monkeypatch):
    loaded = []
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(scorer, "unavailable", lambda: None)
    monkeypatch.setattr(scorer, "models", lambda: loaded.append(1))
    gate.ensure_scorer()
    assert loaded == [1]                                        # modeller de yüklenebildi


@pytest.fixture
def measured(monkeypatch):
    """Gerçek kip, ölçüm sahte: kapının birleşik skoru nasıl kullandığı sınanır."""
    calls = []
    result = {"value": scorer.Result(clip=0.7, face=0.95, composite=0.8, detail={"scorer": "test"})}
    monkeypatch.setattr(settings, "quality_scorer", "real")
    monkeypatch.setattr(scorer, "score_video", lambda path, target: calls.append(target) or result["value"])
    monkeypatch.setattr(scorer, "score_image", lambda path, target: calls.append(target) or result["value"])
    return SimpleNamespace(calls=calls, result=result)


def test_the_gate_judges_the_composite_of_the_real_measurement(measured, tmp_path):
    path = render_clip(str(tmp_path / "ok.mp4"), 1, 160, 90, 3.0, 12)
    target = scorer.Target(content="x")
    passed = gate.score_video(path, 1, {**CFG, "clip_threshold": 0.75}, target)
    assert passed.passed and (passed.clip, passed.face, passed.composite) == (0.7, 0.95, 0.8)
    assert passed.detail == {"scorer": "test"} and measured.calls == [target]
    failed = gate.score_video(path, 1, CFG)
    assert not failed.passed and failed.anomalies == [{"type": "low_clip", "score": 0.8, "threshold": 0.85}]


def test_a_real_measurement_cannot_save_a_black_clip(measured, tmp_path):
    measured.result["value"] = scorer.Result(clip=0.99, face=None, composite=0.99)
    path = render_defect(str(tmp_path / "b.mp4"), "black", 1, 160, 90, 3.0, 12)
    assert "black" in [a["type"] for a in gate.score_video(path, 1, CFG).anomalies]


def test_an_unreadable_clip_scores_zero_with_the_real_scorer(measured, tmp_path):
    measured.result["value"] = None
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    score = gate.score_video(str(bad), 1, CFG)
    assert score.composite == 0.0 and {"unreadable", "low_clip"} <= {a["type"] for a in score.anomalies}


def test_a_first_frame_is_judged_by_the_same_real_scorer(measured, tmp_path):
    path = str(tmp_path / "f.png")
    Image.fromarray(np.random.default_rng(3).integers(0, 255, (90, 160, 3), dtype=np.uint8)).save(path)
    score = gate.score_frame(path, 1, {**CFG, "clip_threshold": 0.75}, scorer.Target(style="s"))
    assert score.passed and score.composite == 0.8 and measured.calls == [scorer.Target(style="s")]


# ---------- gerçek modeller (imajda; CI'da atlanır) ----------

real = pytest.mark.skipif(scorer.unavailable() is not None, reason=f"gerçek skorlayıcı yok: {scorer.unavailable()}")
GOLDEN = "Late-afternoon golden-hour lighting, low warm sun, long dramatic shadows, warm highlights."
PORTRAIT = "Mavi tulumlu, gülümseyen kısa saçlı bir kadın astronotun portresi."
FOUNDRY = "TERK EDİLMİŞ DÖKÜMHANE - GECE. Kızıl bir ışık paslı kazanların arasından süzülüyor. Kerem yavaşça içeri girer."


def face(name):
    return Image.open(FACES / name).convert("RGB")


def still(tmp_path, lighting="", emotion="tension", seed=3):
    from app.engine.methods import _ffmpeg
    clip = render_clip(str(tmp_path / f"c{seed}{emotion}{len(lighting)}.mp4"), seed, 854, 480, 2.0, 8, emotion=emotion,
                       camera={"shot": "wide", "movement": "pan"}, characters=["Kerem"], label="", lighting=lighting)
    out = tmp_path / f"s{seed}{emotion}{len(lighting)}.png"
    _ffmpeg("-ss", "1", "-i", clip, "-frames:v", "1", str(out))
    return Image.open(out).convert("RGB")


@real
def test_the_style_guide_is_measured_golden_hour_light_scores_far_above_another_palette(tmp_path):
    from app.style.bible import EXAMPLE
    style = SimpleNamespace(lighting_prefix=EXAMPLE["lighting_prefix"], motion_style=EXAMPLE["motion_style"])
    target = scorer.target_for(SimpleNamespace(prompt_spec={}), [], style)      # uygulamanın kullandığı stil metni
    golden, other = [], []
    for emotion in ("tension", "joy", "fear"):
        golden.append(scorer.score_images([still(tmp_path, EXAMPLE["lighting_prefix"], emotion)], target).clip)
        other.append(scorer.score_images([still(tmp_path, "", emotion)], target).clip)
    assert min(golden) > 0.75 and float(np.mean(other)) < 0.3, (golden, other)


@real
def test_a_turkish_shot_description_matches_its_picture_and_not_an_unrelated_one():
    portrait = face("ride_1984.jpg")
    assert scorer.score_images([portrait], scorer.Target(content=PORTRAIT)).clip > 0.9
    assert scorer.score_images([portrait], scorer.Target(content=FOUNDRY)).clip < 0.1   # temiz ama ilgisiz


@real
@pytest.mark.parametrize("pixels", [np.full((480, 854, 3), 4), np.full((480, 854, 3), 128),
                                    np.random.default_rng(1).integers(0, 255, (480, 854, 3))])
def test_black_flat_and_noise_frames_score_near_zero(pixels):
    image = Image.fromarray(np.asarray(pixels, dtype=np.uint8))
    assert scorer.score_images([image], scorer.Target(style=GOLDEN, content=FOUNDRY)).clip < 0.05


@real
def test_the_same_person_in_another_photo_is_recognised_and_another_person_is_not():
    m = scorer.models()
    ride84, ride79, jemison = (m.faces(face(n))[0][1] for n in ("ride_1984.jpg", "ride_1979.jpg", "jemison.jpg"))
    assert scorer.identity(float(ride84 @ ride79)) > 0.95
    assert scorer.identity(float(ride84 @ jemison)) < 0.05
    assert m.faces(Image.new("RGB", (640, 360), (90, 90, 90))) == []


@real
def test_face_identity_joins_the_composite_against_the_characters_reference(s3, monkeypatch, tmp_path):
    from app import storage
    monkeypatch.setattr(settings, "work_dir", str(tmp_path))
    key = "pytest/scorer/ride_1984.jpg"
    storage.put_file(str(FACES / "ride_1984.jpg"), key, "image/jpeg")
    target = scorer.Target(content=PORTRAIT, faces=(("Sally", (key,)),))
    same = scorer.score_images([face("ride_1979.jpg")], target)
    stranger = scorer.score_images([face("jemison.jpg")], target)
    empty = scorer.score_images([Image.new("RGB", (640, 360), (90, 90, 90))], target)
    assert same.face > 0.95 and same.detail["identity"]["Sally"] > scorer.FACE_SAME and same.detail["faces_seen"] == 1
    assert stranger.face < 0.05 and stranger.composite < same.composite - 0.3       # başka biri: birleşik skor düşer
    assert empty.face is None and empty.detail["faces_seen"] == 0                    # yüz yok: yüz bileşeni yok
    assert same.composite == pytest.approx(0.6 * same.clip + 0.4 * same.face, abs=1e-3)


@real
def test_a_video_is_scored_on_frames_sampled_through_it(tmp_path):
    path = render_clip(str(tmp_path / "v.mp4"), 3, 640, 360, 2.0, 8, emotion="tension",
                       camera={"shot": "wide", "movement": "pan"}, lighting=GOLDEN)
    assert len(scorer.video_frames(path)) == scorer.SAMPLES
    result = scorer.score_video(path, scorer.Target(style=GOLDEN))
    assert result.detail["scorer"] == scorer.SCORER_ID and 0.0 <= result.clip <= 1.0


@real
def test_a_production_run_with_the_real_scorer_records_what_it_measured(sample_scene, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.engine.iteration import run_tiered_generation
    from app.models import Variation
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0,
                              clip_threshold=0.0, excellent_threshold=0.0)
    monkeypatch.setattr(settings, "quality_scorer", "real")          # ücretsiz modda da gerçek skor denenebilir
    sid = codes["S001_SH01"]
    assert run_tiered_generation(pid, sid) == "refined"
    with session_scope() as s:
        rows = s.scalars(select(Variation).where(Variation.scene_id == sid)).all()
        assert rows and all(v.params["score_detail"]["scorer"] == scorer.SCORER_ID for v in rows)
        assert all(set(v.params["score_detail"]["alignment"]) == {"content"} for v in rows)   # stil kılavuzu yok
        assert all(v.composite_score == v.clip_score != 0.95 and v.face_score is None for v in rows)   # sahte skor değil


# ---------- panel ----------

def test_the_panel_meta_says_which_scorer_judges_the_clips(api, monkeypatch):
    assert api.get("/api/meta").json()["quality"] == {"mode": "dry", "ready": True, "reason": None}
    monkeypatch.setattr(settings, "quality_scorer", "real")
    monkeypatch.setattr(scorer, "unavailable", lambda: "torch paketi kurulu değil")
    assert api.get("/api/meta").json()["quality"] == {"mode": "real", "ready": False, "reason": "torch paketi kurulu değil"}


def test_the_overview_summarises_a_real_measurement_for_the_panel():
    from app.api.panel import measure_of
    detail = {"scorer": scorer.SCORER_ID, "alignment": {"style": 0.9, "content": 0.4}, "faces_seen": 1,
              "identity": {"Kerem": 0.61}}
    assert measure_of({"score_detail": detail}) == {"alignment": {"style": 0.9, "content": 0.4},
                                                    "identity": {"Kerem": 0.61}, "faces_seen": 1}
    assert measure_of({"stage": "first_frame", "frame_score_detail": detail, "score_detail": None})["faces_seen"] == 1
    assert measure_of({}) is None and measure_of(None) is None
    html = (BACKEND / "app" / "panel" / "index.html").read_text()
    assert 'id="score-chip"' in html and "Kalite skoru: gerçek (OpenCLIP + SFace)" in html and "yüzü" in html
