"""Faz 13: stil kılavuzu, ışık dili ve hareket tarzı.

Kabul:
  - Aynı stil sürümünde iki farklı çekimin promptları aynı öneki taşır.
  - Stil değişince önizleme etkilenen çekimleri doğru listeler ve onay olmadan hiçbir şey render edilmez.
  - Prosedürel render "golden-hour" önekiyle görünür biçimde farklı bir palet üretir.
"""
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select, text

from app.budget import killswitch as ks
from app.core.hashing import visual_fingerprint
from app.core.rerender import ALL, Stage
from app.db import session_scope
from app.engine import procedural
from app.hermes import lint, rules
from app.budget import pricing
from app.models import Scene, Variation
from app.style import bible
from app.style.composer import STYLE_TEXT, compose_prompt
from tests.media_utils import frame
from tests.scene_utils import mark_finished, plans

GOLDEN = bible.EXAMPLE["lighting_prefix"]
CODES = ["S001_SH01", "S001_SH02", "S002_SH01", "S002_SH02"]


def _bible(**kw):
    base = dict(version=1, lighting_prefix=GOLDEN, motion_style=bible.EXAMPLE["motion_style"], rules={},
                negative="flicker", palette=[])
    return SimpleNamespace(**{**base, **kw})


def _asset(type_, name, description=None, render_style=None):
    return SimpleNamespace(id=uuid.uuid4(), type=type_, name=name, description=description,
                           render_style=render_style, version=1)


# ---------- besteci (veritabanı gerekmez) ----------

def test_two_different_shots_of_one_style_version_carry_the_same_prefix():
    b = _bible()
    first = compose_prompt({"prompt": "Dökümhane. Kerem içeri girer.", "camera": {"shot": "wide", "movement": "pan"},
                            "characters": ["Kerem"]}, [_asset("location", "Dökümhane")], b)
    second = compose_prompt({"prompt": "Köprü sarsılır.", "camera": {"shot": "close_up"}, "characters": []},
                            [_asset("location", "Köprü"), _asset("prop", "Halat")], b)
    assert first.prefix == second.prefix and first.prefix
    assert first.prompt.startswith(first.prefix) and second.prompt.startswith(second.prefix)
    assert first.prefix == f"{GOLDEN} {bible.EXAMPLE['motion_style']}"


def test_the_prompt_follows_the_documented_order():
    b = _bible(palette=["#E4A23E"])
    c = compose_prompt({"prompt": "Köprü sarsılır", "negative": "bulanık", "camera": {"shot": "wide", "movement": "tracking",
                        "angle": "low", "lens_mm": 24}, "characters": ["Kerem"]},
                       [_asset("prop", "Halat", "çelik halat"), _asset("character", "Kerem", "genç işçi"),
                        _asset("location", "Köprü")], b)
    order = [GOLDEN, "Palette: #E4A23E.", bible.EXAMPLE["motion_style"], "Köprü sarsılır.",
             "Camera: wide shot, tracking shot, low angle, 24 mm lens.", "Location Köprü: " + STYLE_TEXT["painterly"],
             "Character Kerem: genç işçi, " + STYLE_TEXT["clean_3d"], "Prop Halat: çelik halat, " + STYLE_TEXT["clean_3d"],
             lint.cast_constraint(["Kerem"])]
    positions = [c.prompt.index(part) for part in order]
    assert positions == sorted(positions)
    assert c.negative == "flicker, bulanık, " + lint.CAST_NEGATIVE


def test_composition_is_deterministic_and_ignores_the_order_assets_arrive_in():
    a, b = _asset("prop", "Halat"), _asset("location", "Köprü")
    shot = {"prompt": "p", "characters": ["Kerem"]}
    assert compose_prompt(shot, [a, b], _bible()) == compose_prompt(shot, [b, a], _bible())


def test_without_a_style_bible_only_the_shot_and_its_assets_are_described():
    c = compose_prompt({"prompt": "Boş yol"}, [_asset("location", "Yol")], None)
    assert c.prefix == "" and c.prompt == "Boş yol. Location Yol: " + STYLE_TEXT["painterly"] + ". " + lint.cast_constraint([])


def test_render_style_comes_from_the_asset_then_the_bible_rule_then_the_default():
    b = _bible(rules={"prop": "painterly"})
    assert bible.effective_style(_asset("prop", "Taksi"), None) == "clean_3d"
    assert bible.effective_style(_asset("prop", "Taksi"), b) == "painterly"
    assert bible.effective_style(_asset("prop", "Taksi", render_style="stylized_2d"), b) == "stylized_2d"
    free = compose_prompt({"prompt": "p"}, [_asset("prop", "Taksi", render_style="claymation look")], None)
    assert "Prop Taksi: claymation look." in free.prompt


def test_the_active_style_version_enters_the_fingerprint_only_when_there_is_one():
    sc = SimpleNamespace(prompt_spec={"prompt": "p"}, camera={}, engine="fal_wan", duration_s=5.0)
    assert visual_fingerprint(sc, [], [], None) == visual_fingerprint(sc, [])
    assert visual_fingerprint(sc, [], [], 1) != visual_fingerprint(sc, [], [], None)
    assert visual_fingerprint(sc, [], [], 1) != visual_fingerprint(sc, [], [], 2)


# ---------- prosedürel ışık (ffmpeg, veritabanı gerekmez) ----------

def test_lighting_keywords_are_recognised():
    assert procedural.lighting_look(GOLDEN)["name"] == "golden_hour"
    assert procedural.lighting_look("Soft moonlight, night scene")["name"] == "night"
    assert procedural.lighting_look("Altın saat ışığı")["name"] == "golden_hour"
    assert procedural.lighting_look("high-key studio") is None and procedural.lighting_look("") is None


def _render(path, lighting):
    return procedural.render_clip(str(path), 42, 320, 180, 0.5, 8, emotion="tension",
                                  camera={"shot": "wide", "movement": "pan"}, characters=["Kerem"], lighting=lighting)


def test_golden_hour_gives_a_visibly_warmer_palette(tmp_path):
    base = frame(_render(tmp_path / "base.mp4", ""), 0.25).astype(float)
    gold = frame(_render(tmp_path / "gold.mp4", GOLDEN), 0.25).astype(float)
    warmth = lambda img: img[..., 0].mean() - img[..., 2].mean()        # noqa: E731
    assert warmth(gold) - warmth(base) > 25
    assert gold.mean() - base.mean() > 25                               # alçak, sıcak güneş: sahne belirgin aydınlık
    assert np.abs(gold - base).mean() > 30


def test_an_unrecognised_prefix_renders_exactly_like_no_prefix(tmp_path):
    """Işık görünümü rastgele sayı tüketmez: tanınmayan önek, Faz 13 öncesi render'la birebir aynı kareyi verir."""
    plain = frame(_render(tmp_path / "a.mp4", ""), 0.25)
    other = frame(_render(tmp_path / "b.mp4", "high-key studio lighting"), 0.25)
    assert np.array_equal(plain, other)


# ---------- API ----------

@pytest.fixture
def produced(api, api_project, dispatched):
    """Örnek senaryo yüklenmiş ve her çekimi (stil kılavuzu yokken) üretilmiş gibi işaretlenmiş proje."""
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    mark_finished(pid)
    dispatched.clear()
    return pid


def new_version(api, pid, **body):
    r = api.post(f"/api/projects/{pid}/style", json={**bible.EXAMPLE, **body})
    assert r.status_code == 201, r.text
    return r.json()["version"]


def spent(pid):
    with session_scope() as s:
        return s.execute(text("SELECT spent_usd, reserved_usd FROM projects WHERE id = :p"), {"p": pid}).one()


def test_migration_adds_versioned_style_bibles(pg):
    with session_scope() as s:
        assert s.execute(text("SELECT 1 FROM schema_migrations WHERE name = '004_style.sql'")).scalar() == 1


def test_saving_versions_activates_nothing(api, produced):
    pid = produced
    assert (new_version(api, pid), new_version(api, pid, lighting_prefix="Moonlight night")) == (1, 2)
    body = api.get(f"/api/projects/{pid}/style").json()
    assert body["active"] is None and [v["version"] for v in body["versions"]] == [2, 1]
    assert not any(v["is_active"] for v in body["versions"])
    assert body["versions"][1]["prefix"].startswith(GOLDEN)
    assert all(stages == Stage.NONE for stages in plans(pid).values())


def test_the_preview_lists_the_affected_shots_and_renders_nothing(api, produced, dispatched):
    pid = produced
    version = new_version(api, pid)
    before = spent(pid)
    r = api.post(f"/api/projects/{pid}/style/preview", json={"version": version})
    assert r.status_code == 200, r.text
    impact = r.json()
    assert [a["scene_code"] for a in impact["affected"]] == CODES and impact["pending"] == []
    assert all(a["style_only"] for a in impact["affected"])
    assert 0 < impact["expected_usd"] <= impact["worst_usd"]
    assert impact["active_version"] is None
    assert dispatched == [] and spent(pid) == before                          # onay olmadan hiçbir şey üretilmez
    assert all(stages == Stage.NONE for stages in plans(pid).values())
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM variations v JOIN scenes sc ON sc.id = v.scene_id "
                              "WHERE sc.project_id = :p"), {"p": pid}).scalar() == 0


def test_the_cost_estimate_follows_the_production_settings(api, produced):
    pid = produced
    impact = api.post(f"/api/projects/{pid}/style/preview", json={"version": new_version(api, pid)}).json()
    with session_scope() as s:
        from app.config import project_settings
        from app.models import Project
        cfg = project_settings(s.get(Project, pid).settings)
        sc = s.scalar(select(Scene).where(Scene.project_id == pid, Scene.scene_code == "S002_SH01"))
        expected, worst = bible.visual_cost(cfg, sc.engine, sc.duration_s)
    row = next(a for a in impact["affected"] if a["scene_code"] == "S002_SH01")
    assert (row["expected_usd"], row["worst_usd"]) == (expected, worst)
    probe = pricing.estimate_video(sc.engine, cfg["probe_resolution"], sc.duration_s)
    full = pricing.estimate_video(sc.engine, cfg["full_resolution"], sc.duration_s)
    assert expected == pytest.approx(cfg["probe_n"] * probe + cfg["wave_size"] * full)
    assert worst == pytest.approx((cfg["max_correction_rounds"] + 1) * cfg["probe_n"] * probe + cfg["full_n"] * full)
    assert impact["expected_usd"] == pytest.approx(sum(a["expected_usd"] for a in impact["affected"]))


def test_unproduced_shots_are_listed_as_pending_not_as_rerenders(api, api_project, dispatched):
    pid = api_project()
    api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
    impact = api.post(f"/api/projects/{pid}/style/preview", json={"version": new_version(api, pid)}).json()
    assert impact["affected"] == [] and impact["pending"] == CODES and impact["expected_usd"] == 0


def test_activating_without_render_only_changes_the_plan(api, produced, dispatched):
    pid = produced
    version = new_version(api, pid)
    r = api.post(f"/api/projects/{pid}/style/activate", json={"version": version})
    assert r.status_code == 200 and r.json()["active_version"] == version and len(r.json()["impact"]["affected"]) == 4
    assert dispatched == []
    assert all(stages == ALL for stages in plans(pid).values())               # sonraki üretimde görüntü yeniden üretilir
    assert api.get(f"/api/projects/{pid}/style").json()["active"]["version"] == version
    mark_finished(pid)
    again = api.post(f"/api/projects/{pid}/style/preview", json={"version": version}).json()
    assert again["affected"] == [] and again["active_version"] == version     # etkin sürümün yeniden etkisi yok


def test_activating_with_render_queues_exactly_the_affected_shots(api, produced, dispatched):
    pid = produced
    r = api.post(f"/api/projects/{pid}/style/activate", json={"version": new_version(api, pid), "render": True})
    assert r.status_code == 200, r.text
    assert sorted(code for code, stages in r.json()["planned"].items() if stages == ["VISUAL", "LIPSYNC", "MUX"]) == CODES
    assert len(dispatched) == 4


def test_only_one_version_is_active_and_an_older_one_can_be_restored(api, produced):
    pid = produced
    v1, v2 = new_version(api, pid), new_version(api, pid, lighting_prefix="Moonlight night")
    api.post(f"/api/projects/{pid}/style/activate", json={"version": v2})
    api.post(f"/api/projects/{pid}/style/activate", json={"version": v1})
    versions = api.get(f"/api/projects/{pid}/style").json()["versions"]
    assert [(v["version"], v["is_active"]) for v in versions] == [(2, False), (1, True)]


def test_a_frozen_project_cannot_activate_and_render(api, produced, dispatched):
    pid = produced
    version = new_version(api, pid)
    ks.freeze(pid, "test")
    r = api.post(f"/api/projects/{pid}/style/activate", json={"version": version, "render": True})
    assert r.status_code == 423 and dispatched == []
    assert api.get(f"/api/projects/{pid}/style").json()["active"] is None


@pytest.mark.parametrize("body,needle", [
    ({"palette": ["red"]}, "#RRGGBB"),
    ({"rules": {"vehicle": "clean_3d"}}, "Bilinmeyen varlık türü"),
    ({"rules": {"prop": "  "}}, "boş olamaz"),
])
def test_style_versions_are_validated_in_turkish(api, api_project, body, needle):
    r = api.post(f"/api/projects/{api_project()}/style", json=body)
    assert r.status_code == 422 and needle in r.text


def test_unknown_versions_are_404(api, api_project):
    pid = api_project()
    assert api.post(f"/api/projects/{pid}/style/preview", json={"version": 9}).status_code == 404
    assert api.post(f"/api/projects/{pid}/style/activate", json={"version": 9}).status_code == 404


def test_style_cannot_be_written_per_shot(api, produced):
    with session_scope() as s:
        sid = str(s.scalar(select(Scene.id).where(Scene.project_id == produced)))
    r = api.patch(f"/api/scenes/{sid}", json={"prompt_spec": {"prompt": "x", "lighting": "golden hour"}})
    assert r.status_code == 422 and "stil kılavuzunu kullan" in r.text


def test_asset_render_styles_follow_the_active_bible(api, produced):
    pid = produced
    api.post(f"/api/projects/{pid}/style/activate",
             json={"version": new_version(api, pid, rules={"location": "stylized_2d"})})
    assets = {a["name"]: a for a in api.get(f"/api/projects/{pid}/assets").json()}
    assert assets["Çöken Köprü"]["effective_style"] == "stylized_2d" and assets["Kerem"]["effective_style"] == "clean_3d"
    aid = assets["Çöken Köprü"]["id"]
    one = api.patch(f"/api/assets/{aid}", json={"render_style": "painterly"}).json()
    assert (one["render_style"], one["effective_style"], one["version"]) == ("painterly", "painterly", 2)
    back = api.patch(f"/api/assets/{aid}", json={"render_style": ""}).json()
    assert (back["render_style"], back["effective_style"], back["version"]) == (None, "stylized_2d", 3)


# ---------- uçtan uca ----------

def test_produced_prompts_carry_the_style_prefix_and_the_render_uses_its_light(api, api_project, fast, eager):
    pid = api_project(1000.0, probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=1,
                      max_spend_per_10min=10_000)
    version = new_version(api, pid)
    api.post(f"/api/projects/{pid}/style/activate", json={"version": version})
    r = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT})
    assert r.status_code == 200, r.text
    prefix = api.get(f"/api/projects/{pid}/style").json()["active"]["prefix"]
    with session_scope() as s:
        rows = s.scalars(select(Variation.params).join(Scene, Scene.id == Variation.scene_id)
                         .where(Scene.project_id == pid)).all()
    prompts = {p["prompt"] for p in rows}
    assert len(prompts) >= 4 and all(p.startswith(prefix) for p in prompts)
    assert all(sc["status"] == "done" for sc in api.get(f"/api/projects/{pid}/overview").json()["scenes"])
    assert all(stages == Stage.NONE for stages in plans(pid).values())


def test_motion_tests_are_composed_with_the_active_style(api, api_project, fast, eager):
    pid = api_project()
    version = new_version(api, pid)
    api.post(f"/api/projects/{pid}/style/activate", json={"version": version})
    aid = api.post(f"/api/projects/{pid}/assets", json={"type": "prop", "name": "Fener"}).json()["id"]
    api.post(f"/api/assets/{aid}/references/placeholder")
    api.post(f"/api/assets/{aid}/approve-still")
    vid = api.post(f"/api/assets/{aid}/motion-test").json()["variation_id"]
    with session_scope() as s:
        params = s.get(Variation, vid).params
    assert params["prompt"].startswith(GOLDEN) and params["style_version"] == version and params["lighting"] == GOLDEN
    assert "Prop Fener: " + STYLE_TEXT["clean_3d"] in params["prompt"]
