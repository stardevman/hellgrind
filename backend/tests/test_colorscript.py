"""Renk betiği: her sekansın baskın paleti (deterministik k-ortalamalar) ve stil kılavuzunun hedef paletine uzaklık."""
import numpy as np
import pytest
from sqlalchemy import select

from app.db import session_scope
from app.hermes import rules
from app.models import Scene
from app.post import colorscript
from app.style import bible

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


def test_the_palette_finds_the_dominant_colours_with_their_share():
    red, blue = np.tile([[220, 30, 40]], (750, 1)), np.tile([[30, 60, 200]], (250, 1))
    pal = colorscript.palette(np.concatenate([red, blue]).astype(np.float64), k=5)
    assert [p["hex"] for p in pal] == ["#DC1E28", "#1E3CC8"] and [p["weight"] for p in pal] == [0.75, 0.25]
    assert colorscript.palette(np.zeros((0, 3))) == []


def test_the_palette_is_deterministic():
    rng = np.random.default_rng(3)
    px = rng.integers(0, 255, (2000, 3)).astype(np.float64)
    assert colorscript.palette(px) == colorscript.palette(px.copy())


def test_distance_to_the_target_palette():
    pal = [{"hex": "#DC1E28", "weight": 1.0}]
    assert colorscript.distance(pal, ["#DC1E28"]) == 0.0
    assert colorscript.distance(pal, ["#1E3CC8"]) > 50
    assert colorscript.distance(pal, []) is None and colorscript.distance([], ["#000000"]) is None


@pytest.fixture
def produced(api, api_project, fast, eager):
    pid = api_project(1000.0, **SMALL)
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    with session_scope() as s:
        ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
    for code in ("S001_SH01", "S002_SH01"):
        assert "VISUAL" in api.post(f"/api/scenes/{ids[code]}/render").json()["stages"]
    return pid


def test_the_color_script_lists_each_sequence_and_the_style_target(api, produced):
    pid = produced
    v = api.post(f"/api/projects/{pid}/style", json={**bible.EXAMPLE_2D}).json()["version"]
    api.post(f"/api/projects/{pid}/style/activate", json={"version": v})
    r = api.get(f"/api/projects/{pid}/color-script")
    assert r.status_code == 200, r.text
    cs = r.json()
    assert [q["code"] for q in cs["sequences"]] == ["SEQ01", "SEQ02"] and cs["target"] == bible.EXAMPLE_2D["palette"]
    for q in cs["sequences"]:
        assert q["palette"] and abs(sum(c["weight"] for c in q["palette"]) - 1) < 0.01
        assert len(q["shots"]) == 1 and q["distance"] is not None
    again = api.get(f"/api/projects/{pid}/color-script").json()                     # önbellekten, aynı sonuç
    assert again["sequences"] == cs["sequences"]
