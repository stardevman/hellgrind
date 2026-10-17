"""Görsel stüdyosu: yönetmen varlık görselini dışarıya çıkmadan üretir ve bir varlığa referans olarak atar.

  - Ücretsiz modda yerel taslak çizilir; ücretli karşılığının (fal.ai Qwen-Image) fiyatı sanal olarak bütçeden düşer.
  - Aynı girdi aynı anahtarı üretir: dosya varsa iş atlanır, bedel yazılmaz.
  - Atama, referans yüklemesiyle aynı yoldan geçer: sürüm artar, varlık taslağa döner.
  - Ücretli modda referans modunda da çalışır (hat kendiliğinden görsel üretmez; stüdyo yalnızca elle).
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.budget import killswitch as ks
from app.config import settings
from app.db import session_scope
from app.engine import studio
from app.models import CurationEvent, StudioImage
from tests.fal_queue_fake import validate
from tests.test_fal_engines import fal_defaults, falq  # noqa: F401 — fikstürler


@pytest.fixture
def small(monkeypatch, fast):
    monkeypatch.setattr(studio, "LOCAL_SIDE", 160)


def _bible(**kw):
    base = {"lighting_prefix": "", "palette": [], "rules": {}, "negative": "", "motion_style": "", "medium": None}
    return SimpleNamespace(**{**base, **kw})


def _events(pid) -> list[str]:
    with session_scope() as s:
        return list(s.scalars(select(CurationEvent.action).where(CurationEvent.project_id == pid)
                              .order_by(CurationEvent.id)))


# ---------- istem (veritabanı yok) ----------

def test_a_free_prompt_is_passed_through_without_a_cast_constraint():
    p = studio.compose("A red vintage taxi parked in the rain", "free", None, None)
    assert p.prompt == "A red vintage taxi parked in the rain." and p.negative == ""
    assert "No people" not in p.prompt


def test_the_style_bible_adds_lighting_palette_and_the_kind_rule_but_not_the_motion_style():
    b = _bible(lighting_prefix="Golden-hour lighting, long shadows", palette=["amber", "teal"],
               rules={"prop": "clean_3d"}, negative="blurry, text", motion_style="squash and stretch")
    p = studio.compose("A brass lantern", "prop", None, b)
    assert p.prompt.startswith("Golden-hour lighting, long shadows. Palette: amber, teal. A brass lantern.")
    assert "Rendered in" in p.prompt and "squash" not in p.prompt and p.negative == "blurry, text"


def test_an_asset_with_an_empty_prompt_gets_the_reference_sheet_prompt():
    a = SimpleNamespace(id="a1", type="character", name="Kerem", description="tall welder in a leather apron",
                        render_style=None, invariants=[], scale=None)
    p = studio.compose("", "free", a, None)
    assert "Character reference sheet" in p.prompt and "Kerem" in p.prompt
    assert "Only the listed characters appear: Kerem" in p.prompt


def test_the_estimate_is_the_real_fal_price_per_megapixel():
    assert studio.estimate("text", "square") == 0.04          # 1328x1328 = 1,76 MP -> 2 MP x 0,02
    assert studio.estimate("edit", "landscape") == 0.06        # 1664x928 = 1,54 MP -> 2 MP x 0,03
    assert studio.local_size(1664, 928) == (1024, 570)


def test_the_studio_entry_point_is_locked_in_free_mode():
    from app.live import PaidCallBlocked, fal_qwen
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        fal_qwen.studio_image("x", 1, 1328, 1328)


# ---------- ücretsiz mod: uçtan uca ----------

def test_free_mode_draws_local_drafts_and_charges_the_virtual_price(api, api_project, eager, small):
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/images", json={"prompt": "Paslı bir döküm kazanı", "count": 2, "kind": "prop"})
    assert r.status_code == 202, r.text
    assert r.json()["estimate_usd"] == 0.08
    imgs = api.get(f"/api/projects/{pid}/images").json()
    assert len(imgs) == 2 and {i["status"] for i in imgs} == {"done"}
    assert all(i["url"] and i["thumb_url"] and i["engine"] == "local" and i["cost_usd"] == 0.04 for i in imgs)
    assert imgs[0]["seed"] != imgs[1]["seed"] and imgs[0]["url"] != imgs[1]["url"]
    full, thumb = api.get(imgs[0]["url"]), api.get(imgs[0]["thumb_url"])          # panel tam boyu ve küçüğü açabilir
    assert full.status_code == 200 and full.headers["content-type"] == "image/png" and full.content[:4] == b"\x89PNG"
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert api.get(f"/api/projects/{pid}/budget").json()["spent"] == pytest.approx(0.08)
    with session_scope() as s:
        providers = s.execute(text("SELECT provider FROM cost_ledger WHERE project_id = :p AND kind = 'commit'"),
                              {"p": pid}).scalars().all()
    assert providers == ["fal_qwen:studio", "fal_qwen:studio"]
    assert _events(pid)[-1] == "image_generate"


def test_the_same_input_reuses_the_file_and_charges_nothing(api, api_project, eager, small):
    pid = api_project()
    body = {"prompt": "A lighthouse at dusk", "seed": 7}
    first = api.post(f"/api/projects/{pid}/images", json=body).json()["images"][0]
    spent = api.get(f"/api/projects/{pid}/budget").json()["spent"]
    again = api.post(f"/api/projects/{pid}/images", json=body).json()["images"][0]
    done = {i["id"]: i for i in api.get(f"/api/projects/{pid}/images").json()}
    assert done[again["id"]]["url"] == done[first["id"]]["url"] and done[again["id"]]["cost_usd"] == 0
    assert api.get(f"/api/projects/{pid}/budget").json()["spent"] == spent


def test_an_image_becomes_the_reference_of_an_asset(api, api_project, eager, small):
    pid = api_project()
    a = api.post(f"/api/projects/{pid}/assets", json={"type": "prop", "name": "Fener"}).json()
    img = api.post(f"/api/projects/{pid}/images", json={"prompt": "", "asset_id": a["id"]}).json()["images"][0]
    assert img["target_asset_id"] == a["id"] and img["kind"] == "prop"
    r = api.post(f"/api/images/{img['id']}/assign", json={"asset_id": a["id"], "kind": "still"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["asset"]["references"]["still"]["url"] and out["asset"]["version"] == a["version"] + 1
    assert out["asset"]["status"] == "draft" and out["assignments"][0]["name"] == "Fener"
    assert api.post(f"/api/assets/{a['id']}/approve-still").json()["status"] == "still_approved"
    assert _events(pid)[-2:] == ["image_assign", "asset_approve_still"]
    bad = api.post(f"/api/images/{img['id']}/assign", json={"asset_id": a["id"], "kind": "expressions"})
    assert bad.status_code == 422                               # ifade sayfası yalnızca karakterde


def test_edit_mode_takes_sources_from_assets_and_earlier_images(api, api_project, eager, small):
    pid = api_project()
    a = api.post(f"/api/projects/{pid}/assets", json={"type": "prop", "name": "Fener"}).json()
    assert api.post(f"/api/projects/{pid}/images", json={"prompt": "x", "mode": "edit"}).status_code == 422
    nostill = api.post(f"/api/projects/{pid}/images",
                       json={"prompt": "x", "mode": "edit", "sources": [{"asset_id": a["id"]}]})
    assert nostill.status_code == 422 and "durağan görsel yok" in nostill.json()["detail"]
    base = api.post(f"/api/projects/{pid}/images", json={"prompt": "A lantern"}).json()["images"][0]
    api.post(f"/api/images/{base['id']}/assign", json={"asset_id": a["id"]})
    r = api.post(f"/api/projects/{pid}/images", json={"prompt": "image 1 on a wooden table", "mode": "edit",
                                                     "sources": [{"asset_id": a["id"]}, {"image_id": base["id"]}]})
    assert r.status_code == 202, r.text
    img = r.json()["images"][0]
    assert [x["label"] for x in img["sources"]] == ["Fener · Durağan görsel", f"Görsel {base['id'][:4]}"]
    other = api_project()
    foreign = api.post(f"/api/projects/{other}/images",
                       json={"prompt": "x", "mode": "edit", "sources": [{"image_id": base["id"]}]})
    assert foreign.status_code == 422


def test_refusals_start_nothing(api, api_project, eager, small):
    pid = api_project(limit=0.05)
    assert api.post(f"/api/projects/{pid}/images", json={"prompt": "  "}).status_code == 422
    r = api.post(f"/api/projects/{pid}/images", json={"prompt": "A taxi", "count": 2})
    assert r.status_code == 409 and r.json()["detail"].startswith("Bütçe yetmez")
    ks.freeze(pid, "test")
    assert api.post(f"/api/projects/{pid}/images", json={"prompt": "A taxi"}).status_code == 423
    assert api.get(f"/api/projects/{pid}/images").json() == []


def test_favorite_and_hide_are_curation_decisions(api, api_project, eager, small):
    pid = api_project()
    img = api.post(f"/api/projects/{pid}/images", json={"prompt": "A bridge"}).json()["images"][0]
    assert api.patch(f"/api/images/{img['id']}", json={"favorite": True}).json()["favorite"] is True
    assert api.patch(f"/api/images/{img['id']}", json={"hidden": True}).json()["hidden"] is True
    assert api.get(f"/api/projects/{pid}/images").json() == []
    assert [i["id"] for i in api.get(f"/api/projects/{pid}/images?hidden=true").json()] == [img["id"]]
    assert _events(pid)[-2:] == ["image_favorite", "image_hide"]


def test_a_stuck_job_is_shown_as_failed(api, api_project, pg):
    pid = api_project()
    with session_scope() as s:
        s.execute(text("""INSERT INTO studio_images (project_id, batch_id, width, height, seed, input_hash, status,
                          created_at) VALUES (:p, gen_random_uuid(), 10, 10, 1, 'h', 'queued', now() - interval '1 hour')"""),
                  {"p": pid})
    imgs = api.get(f"/api/projects/{pid}/images").json()
    assert imgs[0]["status"] == "failed" and "Worker yanıt vermedi" in imgs[0]["error"]


def test_options_list_sizes_prices_and_the_engine(api, pg):
    o = api.get("/api/images/options").json()
    assert [x["id"] for x in o["aspects"]][:3] == ["square", "landscape", "portrait"]
    assert o["estimates"]["text"]["square"] == 0.04 and o["engine"] == "local" and o["ready"] is True


# ---------- ücretli mod (sahte fal kuyruğu; ağa çıkılmaz) ----------

def test_paid_mode_calls_qwen_image_even_in_reference_mode(api, api_project, eager, falq, monkeypatch):
    monkeypatch.setattr(settings, "video_input_mode", "reference")      # hat görsel üretmez; stüdyo elle çalışır
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/images",
                 json={"prompt": "A brass lantern on a workbench", "aspect": "landscape", "apply_style": False})
    assert r.status_code == 202, r.text
    img = api.get(f"/api/projects/{pid}/images").json()[0]
    assert img["status"] == "done" and img["engine"] == "fal_qwen", img["error"]
    endpoint, args = falq.submits[-1][0], falq.submits[-1][1]
    assert endpoint == settings.fal_qwen_image_endpoint and validate(endpoint, args) == []
    assert args["image_size"] == {"width": 1664, "height": 928} and args["prompt"] == "A brass lantern on a workbench."
    edit = api.post(f"/api/projects/{pid}/images", json={"prompt": "image 1 at night", "mode": "edit",
                                                        "apply_style": False, "sources": [{"image_id": img["id"]}]})
    assert edit.status_code == 202, edit.text
    endpoint, args = falq.submits[-1][0], falq.submits[-1][1]
    assert endpoint == settings.fal_qwen_edit_endpoint and validate(endpoint, args) == []
    assert len(args["image_urls"]) == 1 and args["image_size"] == {"width": 1328, "height": 1328}
    with session_scope() as s:
        rows = s.scalars(select(StudioImage).where(StudioImage.project_id == pid)).all()
    assert sum(float(x.cost_usd) for x in rows) == pytest.approx(0.04 + 0.06)


def test_paid_mode_without_a_fal_key_refuses_before_queueing(api, api_project, paid, monkeypatch):
    monkeypatch.setattr(settings, "fal_key", "")
    pid = api_project()
    r = api.post(f"/api/projects/{pid}/images", json={"prompt": "A taxi"})
    assert r.status_code == 409 and "fal.ai anahtarı" in r.json()["detail"]
    assert api.get("/api/images/options").json()["ready"] is False
