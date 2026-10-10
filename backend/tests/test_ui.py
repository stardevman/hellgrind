from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)  # lifespan çalışmaz: MinIO gerekmez


def test_root_redirects_to_ui():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"


def test_ui_assets_served():
    assert "Hell Grind Stüdyo" in client.get("/ui/").text
    for asset in ("app.js", "style.css"):
        assert client.get(f"/ui/{asset}").status_code == 200


def test_ui_settings_match_backend_defaults():
    import re
    from pathlib import Path

    from app.config import DEFAULT_PROJECT_SETTINGS
    js = (Path(__file__).resolve().parent.parent / "ui" / "app.js").read_text(encoding="utf-8")
    block = js.split("const DEFAULT_SETTINGS = {", 1)[1].split("};", 1)[0]
    ui = dict(re.findall(r"(\w+): \[([^,]+),", block))
    assert set(ui) == set(DEFAULT_PROJECT_SETTINGS)
    for k, v in ui.items():
        assert str(DEFAULT_PROJECT_SETTINGS[k]) == v.strip('"') or float(v) == DEFAULT_PROJECT_SETTINGS[k]
