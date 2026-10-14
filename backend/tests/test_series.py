"""Paket 6: dizi hiyerarşisi (docs/dizi-hiyerarsisi.md). Dizi › Sezon › Bölüm; bölüm = proje.

Kabul:
  - Bölüm bir projedir; sezonda numarasıyla yer alır, aynı numara iki kez verilemez. Sezonu olmayan proje bağımsız filmdir.
  - Dizi kütüphanesindeki varlık bölüme kopyalanmaz, bağlanır; senaryodaki aynı ad kütüphanedeki varlığa bağlanır.
  - Dizi varlığının sürümü artınca görüntüsü üretilmiş bölüm eski sürüme sabitlenir: hiçbir çekim kendiliğinden yeniden
    üretilmek istemez; yeni sürüme geçiş önce etkisini (çekimler, tahmini maliyet) gösterir. Geride kalan varlıkla
    görüntü üretilmez.
  - Kabul kapısı onaylanınca bölüm kilitlenir ("sürüm sabit"): üretim, senaryo ve stil 423 döner; kilidi yalnızca yönetici
    onay ifadesiyle açar ve bu kürasyon kaydına düşer.
"""
import pytest
from sqlalchemy import select, text

from app.config import project_settings
from app.core import series as core
from app.core.hashing import visual_fingerprint
from app.core.state import load_assets, load_characters
from app.db import session_scope
from app.hermes import lint, rules
from app.models import Asset, CurationEvent, EpisodeAsset, Project, Scene, SceneCharacter, Character
from app.style import bible
from tests.test_client_review import _link, filmed  # noqa: F401  (fikstür)


@pytest.fixture
def mk_series(api, pg):
    created = []

    def make(title="pytest-dizi") -> dict:
        r = api.post("/api/series", json={"title": title})
        assert r.status_code == 201, r.text
        created.append(r.json()["id"])
        return r.json()

    yield make
    with session_scope() as s:
        for sid in created:
            s.execute(text("DELETE FROM series WHERE id = :i"), {"i": sid})


@pytest.fixture
def episodes(api, api_project, mk_series):
    """Bir dizi, ilk sezonu ve iki bölümü (api_project ile; teardown projeleri siler)."""
    q = mk_series()
    season = q["seasons"][0]["id"]
    eps = [api_project(100.0), api_project(100.0)]
    for no, pid in enumerate(eps, start=1):
        r = api.post(f"/api/projects/{pid}/episode", json={"season_id": season, "episode_no": no})
        assert r.status_code == 200, r.text
    return q, season, eps


def _actions(pid):
    with session_scope() as s:
        return [e.action for e in s.scalars(select(CurationEvent).where(CurationEvent.project_id == pid)
                                            .order_by(CurationEvent.id))]


def _stamp(pid):
    """Çekimlerin görüntüsü "üretilmiş" olsun: görsel özet bugünkü girdilerle yazılır."""
    with session_scope() as s:
        style = bible.active_version(s, pid)
        for sc in s.scalars(select(Scene).where(Scene.project_id == pid)):
            sc.visual_hash = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), style)


def _fresh(pid) -> dict:
    with session_scope() as s:
        style = bible.active_version(s, pid)
        return {sc.scene_code: visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), style) != sc.visual_hash
                for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}


def _kerem_scenes(pid) -> list[str]:
    with session_scope() as s:
        return sorted(s.scalars(select(Scene.scene_code).join(SceneCharacter, SceneCharacter.scene_id == Scene.id)
                                .join(Character, Character.id == SceneCharacter.character_id)
                                .where(Scene.project_id == pid, Character.name == "Kerem")))


def test_an_episode_is_a_project_numbered_inside_its_season(api, api_project, mk_series):
    q = mk_series("Döküm Hanesi")
    assert [z["number"] for z in q["seasons"]] == [1]               # ilk sezon hazır gelir
    season = q["seasons"][0]["id"]
    r = api.post(f"/api/seasons/{season}/episodes", json={"title": "Pilot", "budget_limit_usd": 50, "settings": {}})
    assert r.status_code == 201 and r.json()["episode_no"] == 1
    first = r.json()["project_id"]
    try:
        loose = api_project()
        assert api.post(f"/api/projects/{loose}/episode", json={"season_id": season}).json()["episode_no"] == 2
        assert api.post(f"/api/projects/{loose}/episode", json={"season_id": season, "episode_no": 1}).status_code == 409
        two = api.post(f"/api/series/{q['id']}/seasons", json={})
        assert two.status_code == 201 and two.json()["number"] == 2
        assert api.post(f"/api/series/{q['id']}/seasons", json={"number": 2}).status_code == 409

        tree = next(x for x in api.get("/api/series").json() if x["id"] == q["id"])
        assert [(e["episode_no"], e["title"]) for e in tree["seasons"][0]["episodes"]][0] == (1, "Pilot")
        info = api.get(f"/api/projects/{first}").json()["episode"]
        assert info["series"]["title"] == "Döküm Hanesi" and info["season"]["number"] == 1 and info["episode_no"] == 1
        assert info["locked"] is None
        listed = {p["id"]: p for p in api.get("/api/projects").json()}
        assert listed[first]["season_id"] == season and listed[first]["episode_no"] == 1

        # bağımsız filme dönüş: dizi bilgisi boşalır
        assert api.post(f"/api/projects/{loose}/episode", json={"season_id": None}).status_code == 200
        assert api.get(f"/api/projects/{loose}").json()["episode"] == {"series": None, "season": None, "episode_no": None,
                                                                       "locked": None}
        assert "episode_create" in _actions(first) and "episode_place" in _actions(loose)
    finally:
        with session_scope() as s:
            s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": first})


def test_the_series_library_is_linked_not_copied_and_the_script_links_matching_names(api, episodes):
    q, _, (ep1, ep2) = episodes
    r = api.post(f"/api/projects/{ep1}/series-library", json={"type": "character", "name": "kerem", "link": False})
    assert r.status_code == 201, r.text
    kerem = r.json()["asset_id"]
    assert r.json()["series_id"] == q["id"] and r.json()["project_id"] is None
    assert api.post(f"/api/projects/{ep1}/series-library", json={"type": "character", "name": "Kerem"}).status_code == 409

    for pid in (ep1, ep2):
        out = api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False})
        assert out.status_code == 200, out.text
        assert "Kerem (character)" in out.json()["assets_linked"]
        assert not any(x.startswith("Kerem ") for x in out.json()["assets_created"])
    with session_scope() as s:
        assert s.scalar(select(text("count(*)")).select_from(Asset).where(Asset.name == "Kerem", Asset.type == "character",
                                                                       Asset.project_id.in_([ep1, ep2]))) == 0
        assert {str(e.project_id) for e in s.scalars(select(EpisodeAsset).where(EpisodeAsset.asset_id == kerem))} == {ep1, ep2}
    names = {a["name"]: a for a in api.get(f"/api/projects/{ep1}/assets").json()}
    assert names["Kerem"]["series_id"] == q["id"] and names["Kerem"]["scenes"]          # bölüm listesinde, çekimleriyle
    assert names["Ayşe"]["series_id"] is None                                          # bölüme özel
    lib = api.get(f"/api/projects/{ep2}/series-library").json()
    assert [a["name"] for a in lib["assets"]] == ["Kerem"] and lib["assets"][0]["link"]["pinned"] is None

    # kullanılan varlığın bağı kaldırılamaz; kullanılmayan bağlanıp çözülebilir
    assert api.delete(f"/api/projects/{ep1}/series-assets/{kerem}").status_code == 409
    taxi = api.post(f"/api/projects/{ep1}/series-library", json={"type": "prop", "name": "Taksi", "link": False}).json()
    assert api.post(f"/api/projects/{ep2}/series-assets/{taxi['asset_id']}").status_code == 201
    assert api.delete(f"/api/projects/{ep2}/series-assets/{taxi['asset_id']}").status_code == 200
    assert {"series_asset_create", "series_asset_link", "series_asset_unlink"} <= set(_actions(ep2)) | set(_actions(ep1))


def test_a_new_version_pins_produced_episodes_and_the_upgrade_shows_its_impact_first(api, episodes, dispatched):
    _, _, (ep1, ep2) = episodes
    kerem = api.post(f"/api/projects/{ep1}/series-library", json={"type": "character", "name": "Kerem", "link": False}).json()
    for pid in (ep1, ep2):
        assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
    _stamp(ep1)                                       # 1. bölüm üretildi; 2. bölümün hiç görüntüsü yok

    r = api.patch(f"/api/assets/{kerem['asset_id']}", json={"description": "Yeni tasarım: kırmızı tulum"})
    assert r.status_code == 200 and r.json()["version"] == 2
    with session_scope() as s:
        links = {str(e.project_id): e.pinned_version for e in s.scalars(select(EpisodeAsset)
                                                                         .where(EpisodeAsset.asset_id == kerem["asset_id"]))}
    assert links == {ep1: 1, ep2: None}               # üretilmiş bölüm eski sürümde; üretilmemiş son sürümü izler
    assert not any(_fresh(ep1).values())               # hiçbir çekim kendiliğinden "değişmiş" görünmez
    assert [u["name"] for u in api.get(f"/api/projects/{ep1}/series-library").json()["updates"]] == ["Kerem"]

    # geride kalan varlıkla görüntü üretilmez
    with session_scope() as s:
        sc = s.scalar(select(Scene).where(Scene.project_id == ep1, Scene.scene_code == _kerem_scenes(ep1)[0]))
        result = lint.check_scene(s, sc.id, project_settings(s.get(Project, ep1).settings))
    assert result.blocks and "yeni sürümü olan" in result.message()

    preview = api.get(f"/api/projects/{ep1}/series-assets/{kerem['asset_id']}/upgrade").json()
    assert (preview["pinned"], preview["current"]) == (1, 2)
    assert preview["affected"] == _kerem_scenes(ep1) and preview["expected_usd"] > 0
    assert not any(_fresh(ep1).values())               # önizleme hiçbir şey yazmaz
    applied = api.post(f"/api/projects/{ep1}/series-assets/{kerem['asset_id']}/upgrade", json={"render": False})
    assert applied.status_code == 200 and dispatched == []
    fresh = _fresh(ep1)
    assert sorted(code for code, changed in fresh.items() if changed) == _kerem_scenes(ep1)
    assert "series_asset_upgrade" in _actions(ep1)


def test_acceptance_locks_the_episode_and_only_an_admin_unlocks_it(api, filmed, mk_series, dispatched):  # noqa: F811
    pid, codes = filmed
    q = mk_series()
    assert api.post(f"/api/projects/{pid}/episode", json={"season_id": q["seasons"][0]["id"]}).status_code == 200
    kerem = api.post(f"/api/projects/{pid}/series-library", json={"type": "prop", "name": "Taksi"}).json()

    v, link = _link(api, pid, gate="acceptance")
    assert api.post(f"/api/review/{link['token']}/decision", json={"decision": "approve"}).status_code == 200
    info = api.get(f"/api/projects/{pid}").json()["episode"]
    assert info["locked"]["label"] == v["label"]
    with session_scope() as s:
        assert s.get(EpisodeAsset, (s.get(Project, pid).id, s.get(Asset, kerem["asset_id"]).id)).pinned_version == 1

    with session_scope() as s:
        sid = str(s.scalar(select(Scene.id).where(Scene.project_id == pid, Scene.scene_code == codes[0])))
    r = api.post(f"/api/scenes/{sid}/render?force_visual=true")
    assert r.status_code == 423 and "sürümü sabit" in r.json()["detail"] and dispatched == []
    assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 423
    assert api.post(f"/api/projects/{pid}/render-all").status_code == 423
    local = next(a for a in api.get(f"/api/projects/{pid}/assets").json() if a["series_id"] is None)
    assert api.patch(f"/api/assets/{local['id']}", json={"description": "x"}).status_code == 423
    # dizi varlığı değişebilir: kilitli bölüm sabit sürümde kalır
    assert api.patch(f"/api/assets/{kerem['asset_id']}", json={"description": "yeni"}).status_code == 200
    with session_scope() as s:
        assert core.updates(s, pid)[0]["pinned"] == 1

    assert api.post(f"/api/projects/{pid}/unlock", json={"confirm": "evet"}).status_code == 422
    assert api.post(f"/api/projects/{pid}/unlock", json={"confirm": "Kilidi aç"}).status_code == 200
    assert api.get(f"/api/projects/{pid}").json()["episode"]["locked"] is None
    assert "episode_unlock" in _actions(pid)


def test_the_panel_gate_also_locks_and_the_unlock_needs_the_admin_cookie(api, api_project, monkeypatch):
    pid = api_project()
    style = api.post(f"/api/projects/{pid}/style", json={"lighting_prefix": "Golden hour"}).json()["version"]
    r = api.post(f"/api/projects/{pid}/gates/acceptance", json={"status": "approved"})
    assert r.status_code == 200 and r.json()["locked"] is True
    locked = api.get(f"/api/projects/{pid}").json()["episode"]["locked"]
    assert locked["at"] and locked["label"] is None                 # panelden: henüz gönderilmiş sürüm yok
    assert api.post(f"/api/projects/{pid}/style/activate", json={"version": style}).status_code == 423
    from app import admin
    monkeypatch.setattr(admin, "password", lambda: "gizli")        # şifre tanımlı: çerezsiz istek yönetici değildir
    assert api.post(f"/api/projects/{pid}/unlock", json={"confirm": "kilidi aç"}).status_code == 401
    assert api.get(f"/api/projects/{pid}").json()["episode"]["locked"] is not None
