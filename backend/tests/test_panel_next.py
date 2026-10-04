"""Next.js paneli (frontend/src) ile API arasındaki sözleşme. Tarayıcı ve Node gerektirmez: kaynak kod okunur.

Denetlenenler: panelin çağırdığı her yol arka uçta o yöntemle var; arka ucun gönderebileceği her durum, duygu, kamera,
varlık ve olay değerinin Türkçe karşılığı var; belgelenmiş iş akışı uç noktaları kullanılıyor. Kaynak bulunamazsa
(ör. yalnızca backend/ bağlanmış bir konteyner) atlanır; `make test` frontend/src'yi /frontend/src olarak bağlar.
"""
import pathlib
import re

import pytest

from app.main import app
from app.schemas import Emotion, Movement, Shot

ROOTS = [pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src", pathlib.Path("/frontend/src")]
SRC = next((r for r in ROOTS if (r / "lib" / "api.ts").is_file()), None)
pytestmark = pytest.mark.skipif(SRC is None, reason="frontend/src bulunamadı")

METHODS = {"get": "get", "post": "post", "put": "put", "patch": "patch", "del": "delete", "upload": "post"}


def sources() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text() for p in sorted(SRC.rglob("*.ts*"))}


def norm(path: str) -> str:
    return "/api" + re.sub(r"\$\{[^}]*\}", "{}", path).split("?")[0]


def calls() -> set[tuple[str, str]]:
    """(yöntem, /api/yol) çiftleri: api.<yöntem>(`...`), useSWR anahtarları ve /api bağlantıları (indirme, görsel)."""
    out = set()
    for text in sources().values():
        for m in re.finditer(r"\bapi\.(get|post|put|patch|del|upload)\s*(?:<[^>(]*>)?\(\s*([`\"'])(/[^`\"']*)\2", text):
            out.add((METHODS[m.group(1)], norm(m.group(3))))
        for m in re.finditer(r"useSWR\s*(?:<[^>(]*>)?\(\s*[^,]*?([`\"'])(/[^`\"']*)\1", text):
            out.add(("get", norm(m.group(2))))
        for m in re.finditer(r"(?:href|src)=\{?`(/api/[^`]*)`", text):
            out.add(("get", norm(m.group(1)[len("/api"):])))
    return out


def known() -> set[tuple[str, str]]:
    return {(method, re.sub(r"\{[^}]*\}", "{}", path)) for path, ops in app.openapi()["paths"].items() for method in ops}


def ts_keys(name: str) -> set[str]:
    text = sources()["lib/i18n.ts"]
    body = re.search(r"export const " + name + r"\b[^=]*=\s*\{(.*?)\};", text, re.S).group(1)
    return set(re.findall(r"(?:^|[{,\s])([a-z_0-9]+):", body))


# ---------- API ile tutarlılık ----------

def test_every_endpoint_the_panel_calls_exists_with_that_method():
    used = calls()
    assert len(used) >= 40
    assert used <= known(), sorted(used - known())


@pytest.mark.parametrize("needed", [
    ("get", "/api/meta"), ("get", "/api/projects"), ("post", "/api/projects"), ("get", "/api/projects/{}/overview"),
    ("post", "/api/projects/{}/script"), ("post", "/api/projects/{}/killswitch"), ("post", "/api/projects/{}/assemble"),
    ("post", "/api/projects/{}/render-all"), ("post", "/api/scenes/{}/render"), ("put", "/api/scenes/{}/audio"),
    ("post", "/api/scenes/{}/promote/{}"),
    # Faz 11
    ("patch", "/api/sequences/{}"), ("post", "/api/sequences/{}/tracks"), ("delete", "/api/timeline-tracks/{}"),
    # Faz 12
    ("get", "/api/projects/{}/assets"), ("post", "/api/projects/{}/assets"), ("post", "/api/assets/{}/references"),
    ("post", "/api/assets/{}/approve-still"), ("post", "/api/assets/{}/motion-test"), ("post", "/api/assets/{}/approve-motion"),
    ("post", "/api/assets/{}/lock"), ("post", "/api/assets/{}/unlock"), ("post", "/api/assets/{}/references/placeholder"),
    ("post", "/api/scenes/{}/assets"), ("delete", "/api/scenes/{}/assets/{}"),
    # Faz 13
    ("get", "/api/projects/{}/style"), ("post", "/api/projects/{}/style"), ("post", "/api/projects/{}/style/preview"),
    ("post", "/api/projects/{}/style/activate"),
    # Faz 14
    ("post", "/api/projects/{}/storyboard"), ("get", "/api/projects/{}/storyboard.pdf"), ("post", "/api/scenes/{}/board"),
    ("post", "/api/scenes/{}/upgrade"), ("post", "/api/projects/{}/draft-pass"), ("post", "/api/projects/{}/upgrade-pass"),
    ("patch", "/api/projects/{}"),
    # Faz 15
    ("post", "/api/scenes/{}/method"), ("post", "/api/scenes/{}/external"), ("post", "/api/scenes/{}/guide"),
    ("get", "/api/variations/{}/revisions"), ("post", "/api/variations/{}/revisions"), ("post", "/api/variations/{}/revisions/active"),
    # Faz 16
    ("post", "/api/scenes/{}/grade"), ("get", "/api/scenes/{}/grade/preview.jpg"), ("post", "/api/projects/{}/lut"),
    ("delete", "/api/projects/{}/lut"), ("post", "/api/projects/{}/continuity"),
    # Faz 17
    ("get", "/api/projects/{}/metrics"), ("get", "/api/projects/{}/curation"), ("get", "/api/projects/{}/curation.jsonl"),
    ("get", "/api/projects/{}/export"),
])
def test_the_panel_uses_the_documented_workflow_endpoints(needed):
    assert needed in calls()


def test_regenerate_forces_the_visual_stage_and_music_change_sends_the_documented_fields():
    src = "\n".join(sources().values())
    assert "/render?force_visual=true`" in src
    assert re.search(r'api\.put<[^>]*>\(`/scenes/\$\{sc\.id\}/audio`, \{ kind: "music", emotion, gain_db', src)
    assert "/assemble?kind=draft`" in src and "draft_timeline" in src
    assert re.search(r'\["edl",.*\["otio",.*\["package",', src) and "/export?format=${f}" in src


def test_new_project_form_sets_the_velocity_guard_to_half_the_budget():
    """Örnek film saniyeler içinde ~22 sanal USD harcar; varsayılan 20 USD / 10 dk sınırı filmi ortasında dondururdu."""
    assert "max_spend_per_10min: Math.max(0.5, limit / 2)" in sources()["components/StartView.tsx"]


def test_the_panel_polls_the_overview_every_two_seconds():
    panel = sources()["lib/panel.tsx"]
    assert "POLL_MS = 2000" in panel and re.search(r"/overview`[^)]*refreshInterval: POLL_MS", panel, re.S)


# ---------- çeviri haritaları tüm değerleri kapsar ----------

def test_maps_cover_every_value_the_backend_can_send():
    from app.engine.references import REFERENCE_KINDS
    from app.models import AssetStatus, AssetType, ProjectStatus, SceneStatus
    assert set(SceneStatus.enums) <= ts_keys("STATUS_TR")
    assert set(ProjectStatus.enums) <= ts_keys("PROJECT_STATUS_TR")
    assert set(Emotion.__args__) <= ts_keys("EMOTION_TR") and set(Emotion.__args__) <= ts_keys("EMOTION_COLOR")
    assert set(Shot.__args__) <= ts_keys("SHOT_TR") and set(Movement.__args__) <= ts_keys("MOVE_TR")
    assert set(AssetStatus.enums) <= ts_keys("ASTATUS_TR") and set(AssetType.enums) <= ts_keys("TYPE_TR")
    assert set(REFERENCE_KINDS) <= ts_keys("REF_TR") and set(REFERENCE_KINDS) <= ts_keys("REF_ACCEPT")
    assert {"black", "freeze", "low_clip", "empty", "unreadable", "flat"} <= ts_keys("ANOMALY_TR")
    assert {"quality_gate", "not_top_k", "superseded", "interrupted", "frozen", "failed"} <= ts_keys("PURGE_TR")
    assert {"fal_wan", "seedance", "higgsfield", "flux_lora", "v2v", "fal", "elevenlabs", "anthropic"} <= ts_keys("KIND_TR")
    assert {"generate", "previs_guide", "external"} <= ts_keys("METHOD_TR")


def test_every_planner_stage_has_a_turkish_name():
    from app.core.rerender import Stage
    text = sources()["lib/text.ts"]
    names = set(re.findall(r"\b([A-Z]+):", re.search(r"const STAGE_TR[^=]*=\s*\{(.*?)\};", text, re.S).group(1)))
    assert {s.name for s in Stage if s.name not in ("NONE", "VISUAL")} <= names


def test_every_event_kind_the_backend_writes_is_rendered():
    emitted = set()
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        emitted |= set(re.findall(r'killswitch\.event\([^,]+,\s*"([a-z_]+)"', path.read_text()))
        emitted |= set(re.findall(r'\bevent\(project_id,\s*"([a-z_]+)"', path.read_text()))
    emitted |= {"frozen", "resumed", "soft_limit"}                       # killswitch.py içinden
    rendered = set(re.findall(r'case "([a-z_]+)":', sources()["lib/text.ts"]))
    assert emitted and emitted <= rendered, emitted - rendered


# ---------- güvenlik ----------

def test_no_raw_html_injection():
    """React metni kendisi kaçışlar; dangerouslySetInnerHTML kullanılmaz (senaryo ve yönetmen notları kullanıcı girdisidir)."""
    offenders = [name for name, text in sources().items() if "dangerouslySetInnerHTML" in text or "innerHTML" in text]
    assert offenders == []


def test_paid_mode_is_never_switched_from_the_panel():
    """Panel ücretli modu yalnızca gösterir; DRY_RUN'ı değiştiren bir uç ya da ayar yoktur."""
    src = "\n".join(sources().values())
    assert "dry_run:" not in re.sub(r"meta\.dry_run|p\.dry_run|dry_run: boolean", "", src)
