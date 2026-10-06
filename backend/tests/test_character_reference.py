"""Karakter referansıyla üretim: karakterin durağan görseli varsa fal yolunda (Seedance) ücretli ilk kare ve storyboard
karesi Flux Kontext ile o görselden çizilir; MiniMax H3 yolunda görseller Ref2VA sunucusuna referans olarak gider. Ayrıca
denemede ses yereldir ve dudak senkronu çağrılmaz (`paid_audio`); fal.ai anahtarı yoksa da çağrılmaz.

Sahte fal ile, gerçek veritabanı, nesne deposu ve FFmpeg ile. Hiçbir test gerçek servise istek göndermez.
"""
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from app import storage
from app.budget import history, pricing
from app.config import settings
from app.db import session_scope
from app.engine.iteration import loras_of, reference_of, run_tiered_generation
from app.engine.storyboard import make_board
from app.live import fal_video
from app.models import AudioTrack, Character, Scene
from app.post import pipeline
from tests.test_live_production import asset_named, commits, fake_scorer, go_paid, studio, use_fal, variations  # noqa: F401


def char(name, refs=(), lora=None):
    return SimpleNamespace(name=name, reference_images=list(refs), lora_url=lora, lora_scale=1.0, lora_trigger=None)


# ---------- referans seçimi (veritabanı gerekmez) ----------

def test_the_reference_is_the_first_character_still_and_a_lora_wins():
    still = "assets/a1/still/abc.png"
    assert reference_of([char("Kerem", ["assets/a0/frames/x_00.jpg"]), char("Elif", [still])]) == {"name": "Elif", "key": still}
    assert reference_of([char("Elif", [still], lora="https://lora")]) is None           # eğitilmiş karakter: LoRA
    assert reference_of([char("Kerem")]) is None
    assert "reference" not in loras_of([char("Kerem")]) and loras_of([char("Elif", [still])])["reference"]["name"] == "Elif"


def test_a_reference_frame_costs_the_per_image_kontext_price():
    table = pricing.table()
    assert pricing.estimate_frame(True) == table["image_per_image"]["flux_kontext"] == 0.04
    assert pricing.estimate_frame(False) == pricing.estimate_image(1.0)
    provider = fal_video.FalProvider("fal_wan")
    with_ref = SimpleNamespace(extra={"reference": {"name": "Elif", "key": "k"}}, image_url=None, resolution="480p",
                               duration_s=3.0)
    assert provider.estimate_frame(with_ref) == 0.04
    assert provider.estimate(with_ref) == round(0.04 + 3 * 0.04, 4)


def test_kontext_calls_are_described_with_the_kontext_model():
    d = history.describe("flux_kontext:first_frame:480p")
    assert (d.service, d.operation, d.models, d.resolution) == ("fal", "first_frame", (settings.fal_kontext_endpoint,), "480p")


def test_pricing_without_a_kontext_price_is_refused():
    import yaml
    table = {**pricing.table()}
    table.pop("image_per_image")
    with pytest.raises(pricing.PricingError, match="flux_kontext"):
        pricing.validate_table(yaml.safe_dump(table))


# ---------- uçtan uca (sahte fal) ----------

def give_still(api, pid, name, tmp_path):
    a = asset_named(pid, name)
    path = tmp_path / "still.png"
    Image.fromarray(np.random.default_rng(5).integers(0, 255, (120, 160, 3), dtype=np.uint8)).save(path)
    r = api.post(f"/api/assets/{a.id}/references?kind=still&filename=still.png", content=path.read_bytes())
    assert r.status_code == 201, r.text
    return r.json()["references"]["still"]["key"]


def test_a_still_upload_bumps_the_character_version_so_the_shot_is_redrawn(api, sample_scene, tmp_path):
    pid, _ = sample_scene()
    with session_scope() as s:
        before = s.scalar(select(Character.embedding_version).where(Character.project_id == pid, Character.name == "Kerem"))
    key = give_still(api, pid, "Kerem", tmp_path)
    with session_scope() as s:
        ch = s.scalar(select(Character).where(Character.project_id == pid, Character.name == "Kerem"))
        assert ch.embedding_version == before + 1 and key in ch.reference_images and "/still/" in key


def test_first_frames_are_drawn_from_the_character_still_with_kontext(api, sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0)
    sid = codes["S001_SH01"]                                       # Kerem içeri girer
    use_fal(sid)
    key = give_still(api, pid, "Kerem", tmp_path)
    studio.behaviour[fal_video.KONTEXT] = lambda args: {"images": [{"url": f"https://fal.test/frame/{args['seed']}.jpg"}]}
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    fal_video._reference_urls.clear()
    assert run_tiered_generation(pid, sid) == "refined"

    apps = [app for app, _, _ in studio.calls]
    assert fal_video.FLUX not in apps and apps.count(fal_video.KONTEXT) == 4
    kontext = [a for app, a, _ in studio.calls if app == fal_video.KONTEXT]
    assert {a["image_url"] for a in kontext} == {"https://fal.test/upload/1"} and len(studio.uploads) == 1   # bir kez yüklenir
    assert all(a["aspect_ratio"] == "16:9" and "Keep Kerem's face" in a["prompt"] for a in kontext)
    assert len({a["seed"] for a in kontext}) == 4
    videos = [a for app, a, _ in studio.calls if app == fal_video.SEEDANCE]
    assert sorted(a["image_url"] for a in videos) == sorted(f"https://fal.test/frame/{a['seed']}.jpg" for a in kontext)
    assert commits(pid, "flux_kontext:first_frame") == [0.04] * 4 and commits(pid, "flux_lora") == []
    assert storage.exists(key)


def test_the_paid_storyboard_frame_uses_the_still_too(api, sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    give_still(api, pid, "Kerem", tmp_path)
    studio.behaviour[fal_video.KONTEXT] = lambda args: {"images": [{"url": f"https://fal.test/frame/{args['seed']}.jpg"}]}
    go_paid(monkeypatch)
    fal_video._reference_urls.clear()
    make_board(pid, codes["S001_SH01"], standalone=True)
    (app, args, _), = studio.calls
    assert app == fal_video.KONTEXT and args["image_url"] == "https://fal.test/upload/1"
    assert commits(pid, "flux_kontext:storyboard") == [0.04]


def test_a_shot_without_the_character_still_uses_flux(api, sample_scene, studio, monkeypatch, tmp_path):
    pid, codes = sample_scene()
    give_still(api, pid, "Kerem", tmp_path)
    go_paid(monkeypatch)
    make_board(pid, codes["S001_SH02"], standalone=True)          # yalnızca Ayşe: referansı yok
    assert [app for app, _, _ in studio.calls] == [fal_video.FLUX]


# ---------- fal.ai denemesi: ses yerel, dudak senkronu yok ----------

def refine(pid, sid):
    run_tiered_generation(pid, sid)
    with session_scope() as s:
        assert s.get(Scene, sid).status == "refined"


def test_without_paid_audio_a_dialogue_shot_is_voiced_locally_and_never_lip_synced(sample_scene, studio, monkeypatch):
    pid, codes = sample_scene(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0, paid_audio=False)
    sid = codes["S002_SH01"]                                       # Kerem: "Atla! Şimdi!"
    use_fal(sid)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    monkeypatch.setattr(settings, "tts_engine", "elevenlabs")
    called = []
    monkeypatch.setattr(pipeline, "_paid_tts", lambda *a, **k: called.append("tts"))
    refine(pid, sid)
    key = pipeline.voice_and_lipsync(sid)
    assert called == [] and fal_video.LIPSYNC not in [app for app, _, _ in studio.calls]
    assert key.endswith("_unsynced.mp4") and storage.exists(key)
    with session_scope() as s:
        tracks = s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sid, AudioTrack.kind == "dialogue")).all()
        assert tracks and all(t.file_key.startswith("audio/espeak/") for t in tracks)
    assert commits(pid, "fal:lipsync") == [] and commits(pid, "elevenlabs") == []


def test_with_paid_audio_the_same_shot_is_lip_synced_on_fal(sample_scene, studio, monkeypatch):
    pid, codes = sample_scene(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0)
    sid = codes["S002_SH01"]
    use_fal(sid)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    refine(pid, sid)
    key = pipeline.voice_and_lipsync(sid)
    assert not key.endswith("_unsynced.mp4") and fal_video.LIPSYNC in [app for app, _, _ in studio.calls]


def test_an_h3_shot_without_a_fal_key_is_never_lip_synced(sample_scene, studio, h3, monkeypatch):
    pid, codes = sample_scene(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0)
    sid = codes["S002_SH01"]
    monkeypatch.setattr(settings, "fal_key", "")                  # yalnızca MiniMax H3: fal.ai isteğe bağlı
    fake_scorer(monkeypatch)
    refine(pid, sid)
    key = pipeline.voice_and_lipsync(sid)
    assert key.endswith("_unsynced.mp4") and storage.exists(key) and studio.calls == []
    assert len(h3.submits) == 2 and commits(pid, "fal:") == []


def test_h3_sends_every_character_still_to_the_ref2va_server(api, sample_scene, h3, monkeypatch, tmp_path):
    pid, codes = sample_scene(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0)
    sid = codes["S002_SH01"]                                       # köprü: Kerem ve Ayşe
    monkeypatch.setattr(settings, "dry_run", True)
    give_still(api, pid, "Kerem", tmp_path)
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, "h3_ref_endpoint_url", "https://h3-ref.test")
    fake_scorer(monkeypatch)
    refine(pid, sid)
    assert h3.hosts == ["https://h3-ref.test"] * 2 and {j["task"] for j in h3.submits} == {"ref2va"}
    for job in h3.submits:
        (ref,) = job["conditions"]
        assert ref["role"] == "reference" and ref["uri"].startswith("data:image/jpeg;base64,")
        assert job["prompt"].startswith("Character references: <Picture 1> is Kerem.")


def test_paid_audio_is_a_validated_project_setting(api):
    r = api.post("/api/projects", json={"title": "pytest-paid-audio", "budget_limit_usd": 1, "settings": {"paid_audio": "no"}})
    assert r.status_code == 422 and "paid_audio" in r.text
    meta = api.get("/api/meta").json()
    assert meta["presets"]["trial"]["paid_audio"] is False
