"""Servis geçmişi: her ücretli (ücretsiz modda sanal) çağrı tek satır; servis, işlem, model, istek kimliği, tutar, hata.

Ücretli yollar sahte istemcilerle sınanır (gerçek servise istek gitmez); kilit yalnızca bellekteki ayarda açılır.
"""
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.budget import history, killswitch, pricing
from app.config import settings
from app.db import session_scope
from tests.test_live_production import asset_named, fake_scorer, go_paid, studio, variations  # noqa: F401 (fikstür)

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "010_service_calls.sql"


def calls(pid) -> list[dict]:
    with session_scope() as s:
        return [dict(r) for r in s.execute(text("SELECT * FROM service_calls WHERE project_id = :p ORDER BY id"),
                                           {"p": pid}).mappings()]


def ledger_kinds(pid) -> list[str]:
    with session_scope() as s:
        return list(s.scalars(text("SELECT kind::text FROM cost_ledger WHERE project_id = :p ORDER BY id"), {"p": pid}))


# ---------- etiketler (veritabanı gerekmez) ----------

@pytest.mark.parametrize("label, service, operation, models, resolution", [
    ("fal_wan:720p", "fal", "video", ("fal_wan_endpoint",), "720p"),
    ("seedance:720p", "fal", "video", ("fal_seedance_endpoint",), "720p"),
    ("flux_lora:first_frame:480p", "fal", "first_frame", ("fal_flux_endpoint",), "480p"),
    ("flux_lora:storyboard", "fal", "storyboard", ("fal_flux_endpoint",), None),
    ("flux_lora:reference", "fal", "reference", ("fal_flux_endpoint",), None),
    ("fal_wan:480p:reference", "fal", "reference", ("fal_flux_endpoint", "fal_wan_endpoint"), "480p"),
    ("fal_wan:480p:motion_test", "fal", "motion_test", ("fal_wan_endpoint",), "480p"),
    ("v2v:720p:previs", "fal", "previs", ("fal_wan_v2v_endpoint",), "720p"),
    ("seedance:720p:revision", "fal", "revision", ("fal_seedance_endpoint",), "720p"),
    ("fal:lipsync", "fal", "lipsync", ("fal_lipsync_endpoint",), None),
    ("elevenlabs", "elevenlabs", "tts", ("elevenlabs_model",), None),
    ("anthropic", "anthropic", "script_parse", ("hermes_model",), None),
])
def test_every_ledger_label_names_its_service_operation_and_model(label, service, operation, models, resolution):
    d = history.describe(label)
    assert (d.service, d.operation, d.resolution) == (service, operation, resolution)
    assert d.models == tuple(getattr(settings, m) for m in models)
    assert operation in history.OPERATIONS


def test_the_model_follows_the_settings_page(monkeypatch):
    monkeypatch.setattr(settings, "fal_flux_endpoint", "fal-ai/flux-lora-yeni")
    assert history.describe("flux_lora:storyboard").model == "fal-ai/flux-lora-yeni"


def test_an_unknown_label_is_recorded_as_other():
    assert history.describe("deneme").operation == "other" and history.describe("deneme").model is None


def test_a_step_outside_a_budgeted_call_is_ignored():
    history.step("fal-ai/flux-lora", "req-x")                     # hata yok, kayıt yok


def test_the_mode_of_an_old_call_follows_the_mode_switches(monkeypatch):
    switches = [(100.0, True), (200.0, False)]                     # 100'de ücretliye geçildi, 200'de dönüldü
    assert history.mode_at(50, switches) is True                   # ilk geçişten önce: ücretsiz
    assert history.mode_at(150, switches) is False
    assert history.mode_at(250, switches) is True
    monkeypatch.setattr(settings, "dry_run", False)
    assert history.mode_at(50, []) is False                        # kayıtta geçiş yok: şimdiki mod


# ---------- bütçeyle aynı işlemde ----------

def test_a_virtual_call_records_the_model_it_would_use(mk_project, mk_scene):
    pid = mk_project(10.0)
    sid = mk_scene(pid)
    with killswitch.spend(pid, 0.4, "fal_wan:480p", scene_id=sid, details={"attempt": 1}) as charge:
        [row] = calls(pid)
        assert (row["status"], row["amount_usd"], row["finished_at"]) == ("running", None, None)
        charge.actual = 0.38
    [row] = calls(pid)
    assert (row["service"], row["operation"], row["model"]) == ("fal", "video", settings.fal_wan_endpoint)
    assert row["status"] == "succeeded" and row["dry_run"] is True
    assert (float(row["estimate_usd"]), float(row["amount_usd"])) == (0.4, 0.38)
    assert row["subject"] == "S001_SH01" and row["project_title"].startswith("pytest-") and row["scene_id"] == uuid.UUID(sid)
    assert row["details"] == {"attempt": 1, "resolution": "480p"} and row["finished_at"] >= row["started_at"]
    with session_scope() as s:
        rid = s.scalar(text("SELECT reservation FROM cost_ledger WHERE project_id = :p AND kind = 'commit'"), {"p": pid})
    assert rid == row["reservation"]


def test_a_failed_call_is_recorded_with_its_error_and_nothing_charged(mk_project):
    pid = mk_project(10.0)
    with pytest.raises(RuntimeError):
        with killswitch.spend(pid, 0.2, "flux_lora:storyboard"):
            history.step("fal-ai/flux-lora", "req-7", seed=3)
            raise RuntimeError("bağlantı koptu")
    [row] = calls(pid)
    assert (row["status"], float(row["amount_usd"]), row["error"]) == ("failed", 0.0, "bağlantı koptu")
    assert row["details"]["error_type"] == "RuntimeError" and row["request_id"] == "req-7"
    assert ledger_kinds(pid) == ["reserve", "release"]


def test_steps_name_the_models_actually_called(mk_project):
    pid = mk_project(10.0)
    with killswitch.spend(pid, 0.5, "fal_wan:480p:reference") as charge:
        history.step("fal-ai/flux-lora", "req-1", seed=9, image_size="landscape_16_9", missing=None)
        history.step("fal-ai/wan/v2.2-a14b/image-to-video", "req-2", num_frames=64)
        charge.actual = 0.5
    [row] = calls(pid)
    assert row["model"] == "fal-ai/flux-lora + fal-ai/wan/v2.2-a14b/image-to-video" and row["request_id"] == "req-2"
    assert row["details"]["steps"] == [
        {"model": "fal-ai/flux-lora", "request_id": "req-1", "seed": 9, "image_size": "landscape_16_9"},
        {"model": "fal-ai/wan/v2.2-a14b/image-to-video", "request_id": "req-2", "num_frames": 64}]


def test_a_refused_reservation_leaves_no_row(mk_project):
    pid = mk_project(0.1)
    with pytest.raises(killswitch.BudgetExceeded):
        with killswitch.spend(pid, 1.0, "fal_wan:720p"):
            pass
    assert calls(pid) == []


def test_the_reaper_closes_an_abandoned_call(mk_project):
    pid = mk_project(10.0)
    rid = killswitch.reserve(pid, 0.3, "fal_wan:720p")
    with session_scope() as s:
        s.execute(text("UPDATE cost_ledger SET created_at = now() - interval '2 hours' WHERE reservation = :r"), {"r": rid})
    assert killswitch.reap_stale_reservations() >= 1
    [row] = calls(pid)
    assert row["status"] == "failed" and "60 dakikada kapanmadı" in row["error"] and float(row["amount_usd"]) == 0


def test_the_history_outlives_a_deleted_project(mk_project):
    pid = mk_project(10.0)
    with killswitch.spend(pid, 0.1, "flux_lora:storyboard") as charge:
        charge.actual = 0.1
    [before] = calls(pid)
    with session_scope() as s:
        s.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        row = s.execute(text("SELECT project_id, project_title, amount_usd FROM service_calls WHERE id = :i"),
                        {"i": before["id"]}).one()
    assert row[0] is None and row[1] == before["project_title"] and float(row[2]) == 0.1


def test_key_values_never_reach_the_record(mk_project, monkeypatch):
    pid = mk_project(10.0)
    monkeypatch.setattr(settings, "fal_key", "gizli-anahtar-123456")
    with pytest.raises(RuntimeError):
        with killswitch.spend(pid, 0.1, "flux_lora:storyboard"):
            raise RuntimeError("reddedildi: Key gizli-anahtar-123456")
    assert calls(pid)[0]["error"] == "reddedildi: Key ••••"


def test_a_reference_names_its_asset(api, sample_scene):
    pid, _ = sample_scene()
    kerem = asset_named(pid, "Kerem")
    assert api.post(f"/api/assets/{kerem.id}/references/placeholder").status_code == 201
    [row] = calls(pid)
    assert (row["operation"], row["subject"], row["asset_id"]) == ("reference", "Kerem", kerem.id)
    assert row["model"] == f"{settings.fal_flux_endpoint} + {settings.fal_wan_endpoint}" and row["dry_run"] is True


# ---------- ücretli yollar (sahte istemciler) ----------

def test_a_paid_storyboard_records_the_flux_request(sample_scene, studio, monkeypatch):
    from app.engine.storyboard import make_board
    pid, codes = sample_scene()
    go_paid(monkeypatch)
    make_board(pid, codes["S001_SH01"], standalone=True)
    [row] = calls(pid)
    assert (row["service"], row["operation"], row["dry_run"], row["status"]) == ("fal", "storyboard", False, "succeeded")
    assert (row["model"], row["request_id"]) == (settings.fal_flux_endpoint, "req-1")
    [step] = row["details"]["steps"]
    assert step["image_size"] == "landscape_16_9" and "seed" in step and step["prompt"]
    assert not any(k.endswith("_url") for k in step)
    assert row["details"]["attempt"] == 1


def test_paid_generation_records_each_first_frame_and_video_with_its_request(sample_scene, studio, monkeypatch):
    from app.engine.iteration import run_tiered_generation
    from app.live import fal_video
    pid, codes = sample_scene(probe_n=2, full_n=2, wave_size=2, refined_k=1, max_correction_rounds=0)
    sid = codes["S001_SH01"]
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    run_tiered_generation(pid, sid)
    rows = [r for r in calls(pid) if r["scene_id"] == uuid.UUID(sid)]
    frames = [r for r in rows if r["operation"] == "first_frame"]
    videos = [r for r in rows if r["operation"] == "video"]
    made = variations(sid)
    assert len(frames) == len(videos) == len(made) and all(r["status"] == "succeeded" for r in rows)
    assert {r["request_id"] for r in frames} == {v.params["first_frame_request_id"] for v in made}
    assert {r["request_id"] for r in videos} == {v.external_request_id for v in made}
    endpoint = {"fal_wan": settings.fal_wan_endpoint, "seedance": settings.fal_seedance_endpoint}[made[0].engine]
    assert all(r["model"] == endpoint and r["dry_run"] is False for r in videos)
    assert all(r["model"] == fal_video.FLUX for r in frames)
    assert abs(sum(float(r["amount_usd"]) for r in rows) - sum(float(v.cost_usd) for v in made)) < 1e-6


def test_an_exhausted_balance_is_recorded_with_the_submitted_request(sample_scene, studio, monkeypatch):
    from app.engine.storyboard import make_board
    from app.live import PaidCallBlocked, fal_video
    from tests.fal_fake import HTTPError
    pid, codes = sample_scene()
    go_paid(monkeypatch)
    studio.behaviour[fal_video.FLUX] = lambda args: HTTPError(402)
    with pytest.raises(PaidCallBlocked):
        make_board(pid, codes["S001_SH01"], standalone=True)
    [row] = calls(pid)
    assert row["status"] == "failed" and row["error"].startswith("fal.ai bakiyesi bitti")
    assert row["details"]["error_type"] == "PaidCallBlocked" and row["request_id"] == "req-1"


def test_a_failing_video_records_every_attempt(sample_scene, studio, monkeypatch):
    from app.engine.iteration import run_tiered_generation
    from app.live import fal_video
    from tests.fal_fake import HTTPError
    pid, codes = sample_scene(probe_n=1, full_n=1, wave_size=1, refined_k=1, max_correction_rounds=0)
    go_paid(monkeypatch)
    fake_scorer(monkeypatch)
    studio.behaviour[fal_video.WAN] = studio.behaviour[fal_video.SEEDANCE] = lambda args: HTTPError(503)
    run_tiered_generation(pid, codes["S001_SH01"])
    videos = [r for r in calls(pid) if r["operation"] == "video"]
    assert [r["details"]["attempt"] for r in videos] == [1, 2, 3]
    assert all(r["status"] == "failed" and "503" in r["error"] and r["request_id"] for r in videos)


def test_elevenlabs_records_its_model_and_characters(mk_project, paid, monkeypatch, tmp_path):
    from app.live import elevenlabs
    pid = mk_project(10.0)
    reply = SimpleNamespace(status_code=500, content=b"", text="", headers={"request-id": "el-1"})
    monkeypatch.setattr(elevenlabs.httpx, "post", lambda url, **kw: reply)
    with pytest.raises(Exception):
        with killswitch.spend(pid, 0.01, "elevenlabs"):
            elevenlabs.tts("Merhaba", "voice-1", "anger", str(tmp_path / "o.mp3"))
    [row] = calls(pid)
    [step] = row["details"]["steps"]
    assert (row["model"], row["request_id"]) == (settings.elevenlabs_model, "el-1")
    assert (step["characters"], step["voice_id"], step["emotion"], step["http_status"]) == (7, "voice-1", "anger", 500)


def test_hermes_records_the_model_and_tokens_of_each_llm_call(mk_project, paid):
    from app.live import hermes_llm
    pid = mk_project(10.0)
    raw = SimpleNamespace(id="msg_1", usage_metadata={"input_tokens": 1000, "output_tokens": 200},
                          response_metadata={"model_name": "claude-test"})
    llm = SimpleNamespace(with_structured_output=lambda schema, include_raw: SimpleNamespace(
        invoke=lambda messages: {"raw": raw, "parsed": None, "parsing_error": None}))
    hermes_llm._ask(llm, pid, "Kadro: Kerem\n\nSahne:\nİÇ. EV - GECE")
    [row] = calls(pid)
    assert (row["service"], row["operation"], row["model"], row["request_id"]) == ("anthropic", "script_parse", "claude-test", "msg_1")
    assert float(row["amount_usd"]) == round(pricing.estimate_llm(1000, 200), 4)
    assert row["details"]["steps"][0]["input_tokens"] == 1000


# ---------- eski kayıtlar ----------

def test_the_migration_carries_old_ledger_rows_into_the_history(mk_project, mk_scene):
    """Migration'daki aktarım deyimi, geçmiş sisteminden önceki rezervasyonları işlemleriyle ve kesin modlarıyla taşır."""
    pid = mk_project(10.0)
    sid = mk_scene(pid)
    labels = {"fal_wan:720p": "commit", "flux_lora:first_frame:480p": "release", "flux_lora:storyboard": "commit",
              "v2v:720p:previs": "commit", "fal:lipsync": "commit", "elevenlabs": None, "anthropic": "commit",
              "seedance:720p:revision": "commit", "fal_wan:480p:motion_test": "commit", "flux_lora:reference": "commit"}
    with session_scope() as s:
        for label, end in labels.items():
            rid = uuid.uuid4()
            for kind in ["reserve", *([end] if end else [])]:
                s.execute(text("""INSERT INTO cost_ledger (project_id, scene_id, provider, kind, amount_usd, reservation)
                                  VALUES (:p, :s, :l, CAST(:k AS ledger_kind), 0.1, :r)"""),
                          {"p": pid, "s": sid, "l": label, "k": kind, "r": rid})
    sql = MIGRATION.read_text()
    with session_scope() as s:
        s.connection().exec_driver_sql(sql[sql.index("INSERT INTO service_calls"):])
    rows = {r["label"]: r for r in calls(pid)}
    assert set(rows) == set(labels)
    for label, end in labels.items():
        d, r = history.describe(label), rows[label]
        assert (r["service"], r["operation"]) == (d.service, d.operation), label
        assert r["status"] == {"commit": "succeeded", "release": "failed", None: "running"}[end]
        assert r["details"] == {"backfilled": True} and r["subject"] == "S001_SH01" and r["model"] is None
    assert rows["flux_lora:first_frame:480p"]["dry_run"] is False          # ilk kare yalnızca ücretli modda olur
    assert rows["flux_lora:storyboard"]["dry_run"] is None                  # açılışta ayar kaydından tamamlanır

    t = rows["flux_lora:storyboard"]["started_at"].timestamp()
    audit = [{"ts": t + 60, "field": "DRY_RUN", "action": "mode", "detail": "ücretli moda geçildi"},
             {"ts": t - 60, "field": "FAL_KEY", "action": "set", "detail": "anahtar değişti"}]
    with session_scope() as s:
        assert history.complete_backfill(s, audit) >= len(labels)
    with session_scope() as s:
        assert history.complete_backfill(s, audit) == 0                    # bir kez
    rows = {r["label"]: r for r in calls(pid)}
    assert rows["flux_lora:storyboard"]["dry_run"] is True                  # geçişten önce: ücretsiz
    assert rows["flux_lora:storyboard"]["details"]["mode_inferred"] is True
    assert rows["flux_lora:first_frame:480p"]["dry_run"] is False
    assert "mode_inferred" not in rows["flux_lora:first_frame:480p"]["details"]
    assert rows["fal:lipsync"]["model"] == settings.fal_lipsync_endpoint and rows["fal:lipsync"]["details"]["model_inferred"]


# ---------- API ----------

def _spent(api_project, dry: bool, label: str, amount: float, fail: bool = False) -> str:
    pid = api_project(100.0)
    with session_scope() as s:
        s.execute(text("""INSERT INTO service_calls (reservation, project_id, project_title, service, operation, label, model,
                                                     dry_run, status, estimate_usd, amount_usd, error)
                          SELECT :r, id, title, :svc, :op, :l, :m, :dry, :st, :a, :amt, :err FROM projects WHERE id = :p"""),
                  {"r": uuid.uuid4(), "p": pid, "svc": history.describe(label).service, "op": history.describe(label).operation,
                   "l": label, "m": history.describe(label).model, "dry": dry, "st": "failed" if fail else "succeeded",
                   "a": amount, "amt": 0 if fail else amount, "err": "HTTP 503" if fail else None})
    return pid


def test_the_list_filters_by_project_mode_and_status(api, api_project):
    pid = _spent(api_project, False, "flux_lora:storyboard", 0.035)
    with session_scope() as s:
        s.execute(text("""INSERT INTO service_calls (reservation, project_id, service, operation, label, dry_run, status,
                                                     estimate_usd, amount_usd, error)
                          VALUES (:r, :p, 'fal', 'video', 'fal_wan:720p', true, 'failed', 0.4, 0, 'HTTP 503')"""),
                  {"r": uuid.uuid4(), "p": pid})
    items = api.get("/api/history", params={"project_id": pid}).json()["items"]
    assert [(i["operation"], i["mode"], i["status"]) for i in items] == [("video", "virtual", "failed"),
                                                                         ("storyboard", "real", "succeeded")]
    first = items[1]
    assert first["service_name"] == "fal.ai" and first["operation_title"] == "Storyboard karesi"
    assert first["model"] == settings.fal_flux_endpoint and first["amount_usd"] == 0.035 and first["project_title"].startswith("pytest-")
    assert first["console"] == "https://fal.ai/dashboard"
    real = api.get("/api/history", params={"project_id": pid, "mode": "real"}).json()["items"]
    assert [i["operation"] for i in real] == ["storyboard"]
    failed = api.get("/api/history", params={"project_id": pid, "status": "failed"}).json()["items"]
    assert [i["error"] for i in failed] == ["HTTP 503"]
    page = api.get("/api/history", params={"project_id": pid, "limit": 1}).json()
    rest = api.get("/api/history", params={"project_id": pid, "limit": 1, "before": page["next"]}).json()
    assert [i["operation"] for i in page["items"] + rest["items"]] == ["video", "storyboard"] and rest["next"] is None
    assert api.get("/api/history", params={"mode": "hepsi"}).status_code == 422


def test_the_summary_splits_real_and_virtual_spend_by_service_model_operation_project_and_day(api, api_project):
    pid = _spent(api_project, False, "fal_wan:720p", 0.4)
    with session_scope() as s:
        for dry, label, amount in ((True, "fal_wan:720p", 0.8), (False, "flux_lora:first_frame:480p", 0.035)):
            s.execute(text("""INSERT INTO service_calls (reservation, project_id, service, operation, label, model, dry_run,
                                                         status, estimate_usd, amount_usd)
                              VALUES (:r, :p, 'fal', :op, :l, :m, :dry, 'succeeded', :a, :a)"""),
                      {"r": uuid.uuid4(), "p": pid, "op": history.describe(label).operation, "l": label,
                       "m": history.describe(label).model, "dry": dry, "a": amount})
    body = api.get("/api/history/summary", params={"project_id": pid, "tz": "Europe/Istanbul"}).json()
    assert body["totals"]["calls"] == 3 and body["totals"]["real_usd"] == 0.435 and body["totals"]["virtual_usd"] == 0.8
    assert [(x["service"], x["name"], x["real_usd"]) for x in body["by_service"]] == [("fal", "fal.ai", 0.435)]
    models = {x["model"]: (x["real_usd"], x["virtual_usd"], x["calls"]) for x in body["by_model"]}
    assert models == {settings.fal_wan_endpoint: (0.4, 0.8, 2), settings.fal_flux_endpoint: (0.035, 0.0, 1)}
    assert {x["title"]: x["calls"] for x in body["by_operation"]} == {"Video üretimi": 2, "İlk kare (önce görsel)": 1}
    assert [(x["project_id"], x["calls"]) for x in body["by_project"]] == [(pid, 3)]
    assert len(body["by_day"]) == 1 and body["by_day"][0]["calls"] == 3
    assert api.get("/api/history/summary", params={"tz": "Mars/Olympus"}).status_code == 422


def test_the_csv_lists_every_call_in_turkish(api, api_project):
    pid = _spent(api_project, False, "flux_lora:storyboard", 0.035)
    r = api.get("/api/history.csv", params={"project_id": pid})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("Zaman (UTC),Servis,Model,İşlem,Proje")
    assert len(lines) == 2 and "fal.ai" in lines[1] and "Storyboard karesi" in lines[1] and ",gerçek,başarılı,0.0350,0.0350," in lines[1]


def test_the_settings_page_separates_real_from_virtual_spend(api, api_project):
    before = api.get("/api/settings").json()
    _spent(api_project, False, "fal_wan:720p", 0.4)
    _spent(api_project, True, "fal_wan:720p", 0.8)
    _spent(api_project, False, "elevenlabs", 0.02, fail=True)              # başarısız: harcama değil
    after = api.get("/api/settings").json()
    fal = lambda st: next(x for x in st["services"] if x["id"] == "fal")       # noqa: E731
    assert round(fal(after)["spent_usd"] - fal(before)["spent_usd"], 4) == 0.4
    assert round(fal(after)["virtual_usd"] - fal(before)["virtual_usd"], 4) == 0.8
    assert round(after["spent_usd"] - before["spent_usd"], 4) == 0.4

