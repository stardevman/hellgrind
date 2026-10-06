"""Faz 12: varlık kütüphanesi, referans eksiksizliği (lint), hareket testi kapısı.

Kabul:
  - Referansı eksik bir nesne içeren çekim `blocked` olur ve hiçbir harcama yapılmaz.
  - Varlık kilitlenip sahne yeniden render edilince üretim başlar.
  - Hareket testi onaylanmadan varlık kilitlenemez.
"""
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.budget import killswitch as ks
from app.core.hashing import visual_fingerprint
from app.db import session_scope
from app.engine import references
from app.engine.iteration import run_tiered_generation
from app.hermes import lint, rules
from app.hermes.ingest import ingest
from app.models import Asset, Character, Scene, SceneAsset, Variation
from app.style import bible
from app.style.composer import compose_prompt
from tests.media_utils import make_tone, make_video
from tests.scene_utils import plans

TAXI_SCRIPT = rules.SAMPLE_SCRIPT.replace(
    "Ayşe gölgelerden çıkar, elinde titreyen bir fener var.",
    "Ayşe gölgelerden çıkar, elinde titreyen bir fener var. [NESNE: Fener]")
CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]


# ---------- ayrıştırıcı: mekân ve nesneler (veritabanı gerekmez) ----------

def test_location_comes_from_the_heading_without_the_time_of_day():
    assert rules.location_name("İÇ. TERK EDİLMİŞ DÖKÜMHANE - GECE") == "Terk Edilmiş Dökümhane"
    assert rules.location_name("DIŞ. ÇÖKEN KÖPRÜ – ŞAFAK") == "Çöken Köprü"
    assert rules.location_name("INT. WAREHOUSE") == "Warehouse"
    assert rules.location_name("SAHNE") is None


def test_sample_script_shots_carry_their_location_and_no_props():
    shots = rules.parse(rules.SAMPLE_SCRIPT)["shots"]
    assert [s["location"] for s in shots] == ["Terk Edilmiş Dökümhane"] * 2 + ["Çöken Köprü"] * 2
    assert all(s["props"] == [] for s in shots)


def test_prop_markers_are_taken_out_of_the_text_and_normalized():
    assert rules.take_props("Kerem bir taksiye biner [NESNE: sarı TAKSİ, Bavul].") == (
        "Kerem bir taksiye biner.", ["Sarı Taksi", "Bavul"])
    assert rules.take_props("Hiç işaret yok.") == ("Hiç işaret yok.", [])


def test_a_marker_inside_a_paragraph_binds_to_that_shot_and_a_lone_marker_to_the_whole_scene():
    script = ("DIŞ. HAVALİMANI ÖNÜ - GÜN\n\n[NESNE: Bavul]\n\nKerem kaldırımda bekler.\n\n"
              "Bir taksi yanaşır. [NESNE: Taksi]\n\nDIŞ. YOL - GECE\n\nYol boştur.\n")
    shots = rules.parse(script)["shots"]
    assert [(s["scene_id"], s["props"]) for s in shots] == [
        ("S001_SH01", ["Bavul"]), ("S001_SH02", ["Taksi", "Bavul"]), ("S002_SH01", [])]
    assert shots[1]["description"] == "Bir taksi yanaşır."
    assert "[" not in shots[1]["visual_prompt"]


def test_a_marker_in_dialogue_is_not_spoken():
    shots = rules.parse("İÇ. ODA - GECE\n\nKEREM\nŞu haritaya bak. [NESNE: Harita]\n")["shots"]
    assert shots[0]["dialogue"][0]["line"] == "Şu haritaya bak." and shots[0]["props"] == ["Harita"]


def test_a_script_without_markers_parses_exactly_as_before():
    """Faz 12 öncesi senaryolar aynı açıklama ve istemleri üretir: görsel özetleri değişmez."""
    for shot in rules.parse(rules.SAMPLE_SCRIPT)["shots"]:
        assert "[" not in shot["description"] and shot["visual_prompt"].count(".") >= 1


# ---------- lint (veritabanı gerekmez) ----------

def test_missing_lists_everything_below_the_required_status_sorted_by_type():
    items = [("a1", "prop", "Taksi", "still_approved"), ("a2", "character", "Kerem", "locked"),
             ("a3", "location", "Köprü", "draft"), (None, "character", "Ayşe", None), ("a3", "location", "Köprü", "draft")]
    missing = lint.find_missing(items, "locked")
    assert [(m.type, m.name, m.status) for m in missing] == [
        ("character", "Ayşe", None), ("location", "Köprü", "draft"), ("prop", "Taksi", "still_approved")]
    assert [m.name for m in lint.find_missing(items, "still_approved")] == ["Ayşe", "Köprü"]


def test_the_lint_message_is_turkish_and_names_what_is_missing():
    msg = lint.describe(lint.find_missing([("a", "prop", "Taksi", "draft")], "locked"), "locked")
    assert msg == ("Üretim engellendi: onaylı referansı olmayan varlıklar var: Taksi (nesne, taslak). "
                   "Gereken en az durum: kilitli.")
    assert lint.describe([], "locked") == ""


def test_the_prompt_says_only_the_listed_characters_appear():
    c = compose_prompt({"prompt": "Köprü. Kerem koşar.", "negative": "bulanık", "characters": ["Kerem", "Ayşe"]}, [], None)
    assert c.prompt == "Köprü. Kerem koşar. Only the listed characters appear: Kerem, Ayşe. No other people or figures."
    assert c.negative == "bulanık, " + lint.CAST_NEGATIVE
    assert compose_prompt({"prompt": "Boş yol."}, [], None).prompt == "Boş yol. No people or characters appear in this shot."


# ---------- görsel özet (veritabanı gerekmez) ----------

def _scene():
    return SimpleNamespace(prompt_spec={"prompt": "p"}, camera={"shot": "wide"}, engine="fal_wan", duration_s=5.0)


def test_a_scene_without_assets_keeps_its_old_fingerprint():
    assert visual_fingerprint(_scene(), [], []) == visual_fingerprint(_scene(), [])


def test_the_fingerprint_follows_asset_versions_but_not_approval_status():
    a = SimpleNamespace(id=uuid.UUID(int=1), version=1, status="draft")
    base = visual_fingerprint(_scene(), [], [a])
    assert base != visual_fingerprint(_scene(), [])
    assert visual_fingerprint(_scene(), [], [SimpleNamespace(id=a.id, version=1, status="locked")]) == base
    assert visual_fingerprint(_scene(), [], [SimpleNamespace(id=a.id, version=2, status="draft")]) != base


# ---------- referans işleme (ffmpeg, veritabanı gerekmez) ----------

def test_turnaround_frames_are_sampled_at_equal_intervals_from_the_start(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.work_dir", str(tmp_path))
    video = make_video(tmp_path / "t.mp4", seconds=4.0)
    frames = references.sample_frames(video)
    assert len(frames) == references.SAMPLE_FRAMES
    assert len(references.image_vector(frames[0])) == 3 * references.HIST_BINS
    vec = references.mean_vector([references.image_vector(f) for f in frames])
    assert abs(sum(x * x for x in vec) - 1) < 1e-3


# ---------- veritabanı ve API ----------

@pytest.fixture
def strict(api, api_project, dispatched):
    """Proje varsayılanlarıyla (lint_strictness=block, asset_min_status=locked) ve fenerli örnek senaryoyla."""
    def make(script=TAXI_SCRIPT, auto_render=True, **cfg):
        pid = api_project(**{"lint_strictness": "block", **cfg})
        r = api.post(f"/api/projects/{pid}/script", json={"text": script, "auto_render": auto_render})
        assert r.status_code == 200, r.text
        return pid, r.json()
    return make


def assets_of(api, pid) -> dict:
    return {(a["type"], a["name"]): a for a in api.get(f"/api/projects/{pid}/assets").json()}


def scene_ids(pid) -> dict:
    with session_scope() as s:
        return {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}


def drive_to_locked(api, asset_id):
    """Varlığı hattın tamamından geçirir: yerel taslak referans, durağan onay, hareket testi, test onayı, kilit."""
    r = api.post(f"/api/assets/{asset_id}/references/placeholder")
    assert r.status_code == 201, r.text
    assert api.post(f"/api/assets/{asset_id}/approve-still").json()["status"] == "still_approved"
    r = api.post(f"/api/assets/{asset_id}/motion-test")
    assert r.status_code == 202 and r.json()["asset"]["motion_test"]["status"] == "passed", r.text
    assert api.post(f"/api/assets/{asset_id}/approve-motion").json()["status"] == "motion_tested"
    r = api.post(f"/api/assets/{asset_id}/lock")
    assert r.status_code == 200 and r.json()["status"] == "locked", r.text
    return r.json()


def test_migration_adds_assets_without_dropping_the_database(pg):
    with session_scope() as s:
        assert s.execute(text("SELECT 1 FROM schema_migrations WHERE name = '003_assets.sql'")).scalar() == 1
        assert "blocked" in s.execute(text("SELECT enum_range(NULL::scene_status)::text")).scalar()
        cols = set(s.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'variations'")).scalars())
        assert "asset_id" in cols


def test_migrations_are_idempotent(pg):
    from app import migrate
    assert migrate.run() == []


def test_ingest_creates_draft_assets_and_links_them(mk_project):
    pid = mk_project()
    summary = ingest(pid, TAXI_SCRIPT)
    assert sorted(summary["assets_created"]) == sorted([
        "Kerem (character)", "Ayşe (character)", "Terk Edilmiş Dökümhane (location)", "Çöken Köprü (location)",
        "Fener (prop)"])
    with session_scope() as s:
        assets = {(a.type, a.name): a for a in s.scalars(select(Asset).where(Asset.project_id == pid))}
        assert {a.status for a in assets.values()} == {"draft"} and {a.version for a in assets.values()} == {1}
        assert {a.render_style for a in assets.values()} == {None}                 # stil kılavuzunun tür kuralını izler
        assert bible.effective_style(assets[("location", "Terk Edilmiş Dökümhane")], None) == "painterly"
        assert bible.effective_style(assets[("prop", "Fener")], None) == "clean_3d"
        chars = {c.name: c for c in s.scalars(select(Character).where(Character.project_id == pid))}
        assert chars["Kerem"].asset_id == assets[("character", "Kerem")].id
        links = {(sc.scene_code, a.name, l.role, l.source) for sc, a, l in s.execute(
            select(Scene, Asset, SceneAsset).join(SceneAsset, SceneAsset.scene_id == Scene.id)
            .join(Asset, Asset.id == SceneAsset.asset_id).where(Scene.project_id == pid))}
    assert ("S001_SH02", "Fener", "prop", "script") in links
    assert ("S002_SH01", "Çöken Köprü", "location", "script") in links
    assert not any(code == "S001_SH01" and name == "Fener" for code, name, _, _ in links)


def test_reingesting_the_same_script_creates_nothing_and_plans_nothing(mk_project):
    from tests.scene_utils import mark_finished
    pid = mk_project()
    ingest(pid, TAXI_SCRIPT)
    mark_finished(pid)
    assert ingest(pid, TAXI_SCRIPT)["assets_created"] == []
    assert all(not stages for stages in plans(pid).values())


def test_a_shot_with_an_unreferenced_prop_is_blocked_and_nothing_is_spent(strict, api, dispatched):
    pid, body = strict()
    assert dispatched == []                                               # hiçbir görev kuyruğa girmedi
    assert body["blocked"]["S001_SH02"] == ["Ayşe", "Terk Edilmiş Dökümhane", "Fener"]
    assert sorted(body["blocked"]) == CODES
    ov = api.get(f"/api/projects/{pid}/overview").json()
    shot = next(sc for sc in ov["scenes"] if sc["scene_code"] == "S001_SH02")
    assert shot["status"] == "blocked"
    assert "Fener (nesne, taslak)" in shot["last_error"] and shot["last_error"].startswith("Üretim engellendi")
    assert {m["name"] for m in shot["missing"]} == {"Ayşe", "Terk Edilmiş Dökümhane", "Fener"}
    assert ov["project"]["spent_usd"] == 0 and ov["project"]["reserved_usd"] == 0
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM cost_ledger WHERE project_id = :p"), {"p": pid}).scalar() == 0


def test_a_single_render_request_reports_the_block(strict, api):
    pid, _ = strict(auto_render=False)
    r = api.post(f"/api/scenes/{scene_ids(pid)['S001_SH02']}/render")
    assert r.status_code == 200 and r.json()["stages"] == []
    assert [m["name"] for m in r.json()["blocked"]["missing"]] == ["Ayşe", "Terk Edilmiş Dökümhane", "Fener"]


def test_the_worker_refuses_too_when_an_asset_is_unlocked_after_dispatch(sample_scene):
    pid, codes = sample_scene(lint_strictness="block")
    assert run_tiered_generation(pid, codes["S002_SH01"]) == "blocked"
    with session_scope() as s:
        sc = s.get(Scene, codes["S002_SH01"])
        assert sc.status == "blocked" and "Çöken Köprü" in sc.last_error
        assert s.scalars(select(Variation).where(Variation.scene_id == sc.id)).all() == []


def test_warn_mode_produces_and_writes_an_event(strict, api, dispatched):
    pid, body = strict(lint_strictness="warn")
    assert body["blocked"] == {} and len(dispatched) == 4
    kinds = [e["kind"] for e in api.get(f"/api/projects/{pid}/budget").json()["recent_events"]]
    assert kinds.count("lint_warning") == 4


def test_off_mode_lists_nothing_as_missing(strict, api):
    pid, body = strict(lint_strictness="off", auto_render=False)
    assert all(sc["missing"] == [] for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"])


def test_locking_the_assets_lets_the_scene_render(strict, api, dispatched, fast, eager):
    pid, _ = strict()
    assets = assets_of(api, pid)
    needed = [("character", "Kerem"), ("character", "Ayşe"), ("location", "Terk Edilmiş Dökümhane")]
    for key in needed:
        drive_to_locked(api, assets[key]["id"])
    sid = scene_ids(pid)["S001_SH01"]                                    # Kerem + dökümhane: hepsi kilitli
    r = api.post(f"/api/scenes/{sid}/render")
    assert r.json() == {"stages": ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"]}
    still = api.post(f"/api/scenes/{scene_ids(pid)['S001_SH02']}/render").json()   # fener hâlâ taslak
    assert still["stages"] == [] and [m["name"] for m in still["blocked"]["missing"]] == ["Fener"]
    unblocked = drive_to_locked(api, assets[("prop", "Fener")]["id"])["unblocked"]
    assert unblocked == ["S001_SH02"]
    assert api.post(f"/api/scenes/{scene_ids(pid)['S001_SH02']}/render").json()["stages"] == ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"]


def test_production_starts_end_to_end_once_assets_are_locked(api, api_project, fast, eager):
    pid = api_project(1000.0, lint_strictness="block", probe_n=2, full_n=2, wave_size=2, refined_k=1,
                      max_correction_rounds=1, max_spend_per_10min=10_000)
    script = "İÇ. ODA - GECE\n\nKerem pencereden bakar.\n\nKEREM\nSessiz bir gece.\n"
    assert api.post(f"/api/projects/{pid}/script", json={"text": script}).json()["blocked"] == {
        "S001_SH01": ["Kerem", "Oda"]}
    spent_before = api.get(f"/api/projects/{pid}/budget").json()["spent"]
    for a in api.get(f"/api/projects/{pid}/assets").json():
        drive_to_locked(api, a["id"])
    motion_spend = api.get(f"/api/projects/{pid}/budget").json()["spent"]
    assert motion_spend > spent_before                                   # referans ve testler sanal bütçeden düştü
    r = api.post(f"/api/projects/{pid}/render-all").json()
    assert r["planned"]["S001_SH01"] == ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"] and r["blocked"] == {}
    sc = next(x for x in api.get(f"/api/projects/{pid}/overview").json()["scenes"])
    assert sc["status"] == "done" and sc["missing"] == []
    with session_scope() as s:
        prompts = {v.params["prompt"] for v in s.scalars(select(Variation).where(Variation.scene_id == sc["id"]))}
    assert prompts and all(p.endswith("Only the listed characters appear: Kerem. No other people or figures.")
                           for p in prompts)


def test_new_projects_block_production_on_missing_references_by_default(api, api_project):
    pid = api_project(**{"lint_strictness": "block"})
    raw = api.post("/api/projects", json={"title": "pytest-varsayilan", "budget_limit_usd": 5}).json()
    try:
        assert raw["settings"]["lint_strictness"] == "block" and raw["settings"]["asset_min_status"] == "locked"
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": raw["id"]})
    r = api.post("/api/projects", json={"title": "pytest-x", "budget_limit_usd": 5, "settings": {"lint_strictness": "sıkı"}})
    assert r.status_code == 422 and "off, warn ya da block" in r.text
    assert pid


def test_still_approved_is_enough_when_the_project_says_so(strict, api, dispatched, fast):
    pid, _ = strict(auto_render=False, asset_min_status="still_approved")
    for a in api.get(f"/api/projects/{pid}/assets").json():
        assert api.post(f"/api/assets/{a['id']}/references/placeholder").status_code == 201
        api.post(f"/api/assets/{a['id']}/approve-still")
    assert all(stages == ["STORYBOARD", "VISUAL", "LIPSYNC", "MUX"]
               for stages in api.post(f"/api/projects/{pid}/render-all").json()["planned"].values())


# ---------- durum geçişleri ----------

@pytest.fixture
def prop(api, api_project, fast):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/assets", json={"type": "prop", "name": "  sarı   taksi ", "description": "Eski bir taksi"})
    assert r.status_code == 201, r.text
    return pid, r.json()


def test_creating_an_asset_normalizes_the_name_and_refuses_duplicates(api, prop):
    pid, a = prop
    assert (a["name"], a["status"], a["version"], a["render_style"], a["effective_style"]) == (
        "Sarı Taksi", "draft", 1, None, "clean_3d")
    r = api.post(f"/api/projects/{pid}/assets", json={"type": "prop", "name": "SARI TAKSİ"})
    assert r.status_code == 409


def test_a_still_cannot_be_approved_without_a_visual_reference(api, prop):
    _, a = prop
    r = api.post(f"/api/assets/{a['id']}/approve-still")
    assert r.status_code == 409 and "görsel referans" in r.json()["detail"]


def test_an_asset_cannot_be_locked_before_its_motion_test_is_approved(api, prop, eager):
    _, a = prop
    aid = a["id"]
    assert api.post(f"/api/assets/{aid}/lock").status_code == 409                       # taslak
    assert api.post(f"/api/assets/{aid}/motion-test").status_code == 409                # önce durağan onay
    api.post(f"/api/assets/{aid}/references/placeholder")
    api.post(f"/api/assets/{aid}/approve-still")
    r = api.post(f"/api/assets/{aid}/lock")
    assert r.status_code == 409 and r.json()["detail"] == "Sarı Taksi: hareket testi onaylanmadan varlık kilitlenemez."
    assert api.post(f"/api/assets/{aid}/approve-motion").json()["detail"] == "Sarı Taksi: önce hareket testi üret."
    assert api.post(f"/api/assets/{aid}/motion-test").status_code == 202
    assert api.post(f"/api/assets/{aid}/lock").status_code == 409                       # test var ama onaylanmadı
    assert api.post(f"/api/assets/{aid}/approve-motion").json()["status"] == "motion_tested"
    assert api.post(f"/api/assets/{aid}/lock").json()["status"] == "locked"


def test_a_defective_motion_test_cannot_be_approved(api, prop, eager, monkeypatch):
    _, a = prop
    aid = a["id"]
    api.post(f"/api/assets/{aid}/references/placeholder")
    api.post(f"/api/assets/{aid}/approve-still")
    monkeypatch.setattr("app.config.settings.dry_run_defect_rate", 1.0)
    test = api.post(f"/api/assets/{aid}/motion-test").json()["asset"]["motion_test"]
    assert test["status"] == "rejected" and test["anomalies"]
    r = api.post(f"/api/assets/{aid}/approve-motion")
    assert r.status_code == 409 and "kusur" in r.json()["detail"]


def test_a_new_reference_sends_the_asset_back_to_draft_and_voids_its_motion_test(api, prop, eager, tmp_path):
    _, a = prop
    aid = a["id"]
    version = drive_to_locked(api, aid)["version"]
    still = tmp_path / "still.png"
    Image.new("RGB", (64, 48), (200, 40, 40)).save(still)
    r = api.post(f"/api/assets/{aid}/references?kind=still&filename=still.png", content=still.read_bytes())
    assert r.status_code == 409 and "kilitli" in r.json()["detail"]                     # önce kilit açılır
    assert api.post(f"/api/assets/{aid}/unlock").json()["status"] == "motion_tested"
    r = api.post(f"/api/assets/{aid}/references?kind=still&filename=still.png", content=still.read_bytes())
    assert r.status_code == 201, r.text
    after = r.json()
    assert (after["status"], after["version"], after["motion_test"]) == ("draft", version + 1, None)
    again = api.post(f"/api/assets/{aid}/references?kind=still&filename=still.png", content=still.read_bytes()).json()
    assert again["version"] == version + 1                                                # aynı dosya: değişiklik yok


def test_an_old_motion_test_cannot_be_approved_after_the_description_changes(api, prop, eager):
    _, a = prop
    aid = a["id"]
    version = api.post(f"/api/assets/{aid}/references/placeholder").json()["version"]
    api.post(f"/api/assets/{aid}/approve-still")
    api.post(f"/api/assets/{aid}/motion-test")
    edited = api.patch(f"/api/assets/{aid}", json={"description": "Sarı, ezik bir taksi"}).json()
    assert (edited["version"], edited["status"], edited["motion_test"]) == (version + 1, "still_approved", None)
    assert api.post(f"/api/assets/{aid}/approve-motion").status_code == 409


def test_a_motion_test_is_refused_in_a_frozen_project_and_is_charged_otherwise(api, prop, eager):
    pid, a = prop
    aid = a["id"]
    api.post(f"/api/assets/{aid}/references/placeholder")
    api.post(f"/api/assets/{aid}/approve-still")
    ks.freeze(pid, "test")
    assert api.post(f"/api/assets/{aid}/motion-test").status_code == 423
    assert api.post(f"/api/assets/{aid}/references/placeholder").status_code == 423
    ks.resume(pid)
    test = api.post(f"/api/assets/{aid}/motion-test").json()["asset"]["motion_test"]
    assert test["cost_usd"] > 0 and test["video_url"] and test["thumb_url"]
    with session_scope() as s:
        rows = s.execute(text("SELECT provider, kind, scene_id FROM cost_ledger WHERE project_id = :p ORDER BY id"),
                         {"p": pid}).all()
    assert ("h3:480p:motion_test", "commit", None) in rows
    assert ("flux_lora:reference", "commit", None) in rows


# ---------- referans paketi ----------

@pytest.fixture
def kerem(api, api_project, s3):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/characters", json={"name": "Kerem", "voice_id": "ses-123"})
    assert r.status_code == 201, r.text
    return pid, r.json()["asset_id"]


def test_a_turnaround_video_gives_face_references_and_vectors(api, kerem, tmp_path):
    pid, aid = kerem
    video = make_video(tmp_path / "kerem.mp4", seconds=4.0)
    r = api.post(f"/api/assets/{aid}/references?kind=turnaround&filename=kerem.mp4",
                 content=open(video, "rb").read(), headers={"content-type": "video/mp4"})
    assert r.status_code == 201, r.text
    ref = r.json()["references"]["turnaround"]
    assert len(ref["frames"]) == references.SAMPLE_FRAMES and ref["face_ref"] == ref["frames"][0]
    assert ref["vector_dims"] == 48 and ref["vector_model"] == references.VECTOR_MODEL
    assert api.get(ref["frame_urls"][0]).headers["content-type"] == "image/jpeg"
    char = r.json()["character"]
    assert char["voice_id"] == "ses-123" and char["embedding_version"] == 2              # ses alanı korundu
    with session_scope() as s:
        assert s.scalar(select(Character.reference_images).where(Character.asset_id == aid)) == ref["frames"]


def test_voice_sample_and_expressions_belong_to_characters_and_do_not_touch_visual_versions(api, kerem, tmp_path):
    pid, aid = kerem
    tone = make_tone(tmp_path / "ses.wav", seconds=1.0)
    r = api.post(f"/api/assets/{aid}/references?kind=voice_sample&filename=ses.wav", content=open(tone, "rb").read())
    assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["references"]["voice_sample"]["duration_s"] > 0
    loc = api.post(f"/api/projects/{pid}/assets", json={"type": "location", "name": "Köprü"}).json()
    r = api.post(f"/api/assets/{loc['id']}/references?kind=expressions&filename=x.png", content=b"x")
    assert r.status_code == 422


@pytest.mark.parametrize("kind,filename,payload,needle", [
    ("still", "a.txt", b"hello", "uzantı"),
    ("still", "a.png", b"not an image", "okunamadı"),
    ("turnaround", "a.mp4", b"not a video", "görüntü"),
    ("hologram", "a.png", b"x", "Bilinmeyen"),
])
def test_bad_reference_files_are_refused_in_turkish(api, kerem, kind, filename, payload, needle):
    _, aid = kerem
    r = api.post(f"/api/assets/{aid}/references?kind={kind}&filename={filename}", content=payload)
    assert r.status_code == 422 and needle in r.json()["detail"]


def test_voice_id_is_kept_on_the_character(api, kerem):
    _, aid = kerem
    r = api.patch(f"/api/assets/{aid}", json={"voice_id": "yeni-ses"})
    assert r.json()["character"]["voice_id"] == "yeni-ses" and r.json()["version"] == 1


# ---------- sahne bağları ----------

def test_a_prop_linked_from_the_panel_survives_reingest_and_changes_the_plan(strict, api, dispatched):
    pid, _ = strict(script=rules.SAMPLE_SCRIPT, auto_render=False)
    sid = scene_ids(pid)["S002_SH01"]
    r = api.post(f"/api/scenes/{sid}/assets", json={"name": "halat"})
    assert r.status_code == 201 and r.json()["asset"]["name"] == "Halat"
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    ov = api.get(f"/api/projects/{pid}/overview").json()
    shot = next(sc for sc in ov["scenes"] if sc["id"] == sid)
    assert {a["name"] for a in shot["assets"]} >= {"Halat", "Çöken Köprü"}
    char = assets_of(api, pid)[("character", "Kerem")]["id"]
    assert api.post(f"/api/scenes/{sid}/assets", json={"asset_id": char}).status_code == 422
    assert api.delete(f"/api/scenes/{sid}/assets/{r.json()['asset']['id']}").json()["ok"] is True
    assert api.delete(f"/api/scenes/{sid}/assets/{r.json()['asset']['id']}").status_code == 404


def test_assets_can_be_filtered_and_report_where_they_are_used(strict, api):
    pid, _ = strict(auto_render=False)
    locations = api.get(f"/api/projects/{pid}/assets?type=location").json()
    assert [a["name"] for a in locations] == ["Terk Edilmiş Dökümhane", "Çöken Köprü"]
    assert assets_of(api, pid)[("character", "Kerem")]["scenes"] == ["S001_SH01", "S002_SH01", "S002_SH02"]
    assert assets_of(api, pid)[("prop", "Fener")]["scenes"] == ["S001_SH02"]
    assert api.get(f"/api/projects/{pid}/assets?status=locked").json() == []
    counts = api.get(f"/api/projects/{pid}/overview").json()["project"]["asset_counts"]
    assert counts == {"draft": 5}
