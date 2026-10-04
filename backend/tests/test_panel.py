"""Klasik panel (tek statik HTML, /panel-klasik): tasarım sözleşmesi ve API ile tutarlılık. Tarayıcı gerektirmez.
Next.js paneli (/panel) için aynı sözleşmeler test_panel_next.py içindedir.

Gerçek tarayıcı davranışı (tıklama, yoklama, video) ayrıca elle/Brave ile doğrulanır; burada panelin sözleşmeleri
(uç noktalar, kimlikler, çeviri haritaları, renkler, erişilebilirlik) bozulmasın diye statik olarak denetlenir.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

from app.main import app
from app.schemas import Angle, Emotion, Movement, Shot

HTML = (pathlib.Path(__file__).resolve().parents[1] / "app" / "panel" / "index.html").read_text()
SCRIPT = HTML[HTML.index("<script>") + 8:HTML.index("</script>")]
STYLE = HTML[HTML.index("<style>"):HTML.index("</style>")]


def js_object_keys(name):
    body = re.search(r"const " + name + r" = \{(.*?)\};", SCRIPT, re.S).group(1)
    return set(re.findall(r"(?:^|[{,\s])([a-z_]+):", body))


# ---------- sayfa ----------

def test_panel_is_a_single_turkish_page_served_without_a_build(api):
    r = api.get("/panel-klasik/")
    assert r.status_code == 200 and r.text == HTML
    assert '<html lang="tr">' in HTML and "<script src" not in HTML          # dış betik yok: build adımı yok


@pytest.mark.parametrize("text", [
    "Ücretsiz mod: hiçbir ücretli servis çağrılmaz", "Yeni proje", "Üretimi durdur", "Üretime devam et",
    "Senaryoyu işle ve üret", "Bunu kullan", "Müziği değiştir", "Bu sahneyi baştan üret", "Rafine havuz",
    "Çekim bilgileri", "Diyalog", "Olay akışı", "birleştir", "Sonuca dön",
])
def test_panel_has_the_documented_turkish_texts(text):
    assert text in HTML


def test_polls_the_overview_every_two_seconds():
    assert "const POLL_MS = 2000" in SCRIPT and "/overview" in SCRIPT


# ---------- tasarım sözleşmesi ----------

@pytest.mark.parametrize("name,hex", [("slag", "2A2D31"), ("plate", "33373C"), ("ash", "E6E1D8"), ("dust", "A39E95"),
                                      ("ember", "E4A23E"), ("verdigris", "62AE8E"), ("oxide", "C2573F"), ("steel", "86A5B8")])
def test_palette_tokens_are_the_documented_colours(name, hex):
    assert re.search(rf"--{name}:\s*#{hex}", STYLE, re.I)


def test_font_is_archivo_with_the_width_axis():
    assert "family=Archivo:wdth,wght@" in HTML and "font-stretch" in STYLE and '"Archivo"' in STYLE


def test_there_are_no_uppercase_labels_or_small_caps():
    assert "text-transform" not in STYLE and "small-caps" not in STYLE and "uppercase" not in STYLE.lower()


def test_accessibility_features_are_present():
    assert ":focus-visible" in STYLE and "outline: 2px solid" in STYLE
    assert "@media (prefers-reduced-motion: reduce)" in STYLE and "animation: none" in STYLE
    assert 'name="viewport"' in HTML and "aria-live" in HTML and 'role="alert"' in HTML


def test_layout_collapses_to_one_column_on_small_screens():
    mobile = STYLE[STYLE.index("@media (max-width"):]
    assert "grid-template-columns: 1fr" in mobile


def test_film_strip_states_are_all_styled():
    for state in ("refined", "rejected", "retired", "working", "failed", "selected", "tier-start"):
        assert f".tile.{state}" in STYLE, state
    assert "breathe" in STYLE and "var(--verdigris)" in STYLE and "var(--oxide)" in STYLE and "var(--ember)" in STYLE


# ---------- API ile tutarlılık ----------

def api_calls():
    calls = set()
    for m in re.finditer(r'api\(\s*([`"\'])(.+?)\1\s*(?:,\s*"(\w+)")?', SCRIPT):
        path = re.sub(r"\$\{[^}]*\}", "{}", m.group(2)).split("?")[0]
        calls.add(((m.group(3) or "GET").lower(), "/api" + path))
    return calls


def test_every_endpoint_the_panel_calls_exists_with_that_method():
    schema = app.openapi()["paths"]
    known = {(method, re.sub(r"\{[^}]*\}", "{}", path)) for path, ops in schema.items() for method in ops}
    calls = api_calls()
    assert len(calls) >= 10
    assert calls <= known, calls - known


def test_the_panel_uses_the_documented_workflow_endpoints():
    paths = {p for _, p in api_calls()}
    for needed in ("/api/meta", "/api/projects", "/api/projects/{}/overview", "/api/projects/{}/script",
                   "/api/projects/{}/killswitch", "/api/projects/{}/assemble", "/api/scenes/{}/render",
                   "/api/scenes/{}/audio", "/api/scenes/{}/promote/{}"):
        assert needed in paths, needed


def test_regenerate_uses_force_visual_and_music_change_sends_the_documented_fields():
    assert "render?force_visual=true" in SCRIPT
    assert re.search(r'api\(`/scenes/\$\{id\}/audio`, "PUT", \{ kind: "music"', SCRIPT)


def test_every_element_the_script_looks_up_exists_in_the_html():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', SCRIPT))
    assert used and not used - ids, used - ids


def test_new_project_form_sets_the_velocity_guard_to_half_the_budget():
    """Örnek film saniyeler içinde ~22 sanal USD harcar; varsayılan 20 USD / 10 dk sınırı filmi ortasında dondururdu."""
    assert "max_spend_per_10min: Math.max(0.5, budget / 2)" in SCRIPT


# ---------- çeviri haritaları tüm değerleri kapsar ----------

def test_status_and_emotion_maps_cover_every_value_the_backend_can_send():
    from app.models import SceneStatus
    assert set(SceneStatus.enums) <= js_object_keys("STATUS_TR")
    assert set(Emotion.__args__) <= js_object_keys("EMOTION_TR") and set(Emotion.__args__) <= js_object_keys("EMOTION_COLOR")
    from app.models import ProjectStatus
    assert set(ProjectStatus.enums) <= js_object_keys("PROJECT_STATUS_TR")
    assert set(Shot.__args__) <= js_object_keys("SHOT_TR") and set(Movement.__args__) <= js_object_keys("MOVE_TR")
    assert Angle.__args__ and {"black", "freeze", "low_clip", "empty", "unreadable", "flat"} <= js_object_keys("ANOMALY_TR")


def test_every_event_kind_the_backend_writes_is_rendered():
    emitted = set()
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        emitted |= set(re.findall(r'killswitch\.event\([^,]+,\s*"([a-z_]+)"', path.read_text()))
        emitted |= set(re.findall(r'\bevent\(project_id,\s*"([a-z_]+)"', path.read_text()))
    emitted |= {"frozen", "resumed", "soft_limit"}                       # killswitch.py içinden
    rendered = set(re.findall(r'case "([a-z_]+)":', SCRIPT))
    assert emitted and emitted <= rendered, emitted - rendered


# ---------- betik ----------

@pytest.mark.skipif(not shutil.which("node"), reason="node kurulu değil")
def test_the_script_is_syntactically_valid_javascript(tmp_path):
    path = tmp_path / "panel.js"
    path.write_text(SCRIPT)
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def interpolations(line):
    """Satırdaki ${...} ifadeleri (iç içe süslü parantezler eşleştirilerek)."""
    out, i = [], 0
    while (i := line.find("${", i)) != -1:
        depth, j = 1, i + 2
        while j < len(line) and depth:
            depth += {"{": 1, "}": -1}.get(line[j], 0)
            j += 1
        out.append(line[i + 2:j - 1].strip())
        i = j
    return out


def test_every_dynamic_text_that_reaches_html_is_escaped():
    """HTML üreten satırlarda kullanıcı/senaryo kaynaklı metin alanları esc() içinden geçmeli (XSS)."""
    risky = re.compile(r"\.(description|heading|text|reason|last_error|title|character|mood|scene_code)\b|\berror\b")
    offenders = []
    for line in SCRIPT.splitlines():
        if "<" not in line or "${" not in line or line.lstrip().startswith("//"):
            continue
        for expr in interpolations(line):
            if risky.search(expr) and not expr.startswith("esc("):
                offenders.append(expr)
    assert offenders == []


# ---------- varlıklar (Faz 12) ----------

def test_the_assets_tab_has_its_workflow_and_maps_every_asset_value():
    from app.engine.references import REFERENCE_KINDS
    from app.models import AssetStatus, AssetType
    for text in ("Varlıklar", "Durağanı onayla", "Hareket testi üret", "Hareket testini onayla", "Kilitle", "Kilidi aç",
                 "Yerel taslak üret", "Eksik referans", "Engellenen çekimleri yeniden dene", "Nesne bağla"):
        assert text in HTML, text
    assert set(AssetStatus.enums) <= js_object_keys("ASTATUS_TR") and set(AssetType.enums) <= js_object_keys("TYPE_TR")
    assert set(REFERENCE_KINDS) <= js_object_keys("REF_TR") and set(REFERENCE_KINDS) <= js_object_keys("REF_ACCEPT")


def test_the_panel_uses_the_asset_endpoints():
    paths = {p for _, p in api_calls()}
    for needed in ("/api/projects/{}/assets", "/api/assets/{}/approve-still", "/api/assets/{}/motion-test",
                   "/api/assets/{}/approve-motion", "/api/assets/{}/lock", "/api/assets/{}/unlock",
                   "/api/assets/{}/references/placeholder", "/api/scenes/{}/assets", "/api/scenes/{}/assets/{}",
                   "/api/projects/{}/render-all"):
        assert needed in paths, needed
    upload = re.search(r"fetch\(`/api(/assets/\$\{id\}/references)\?", SCRIPT)                # ham gövdeyle yükleme
    known = {re.sub(r"\{[^}]*\}", "{}", p) for p, ops in app.openapi()["paths"].items() if "post" in ops}
    assert upload and "/api" + re.sub(r"\$\{[^}]*\}", "{}", upload.group(1)) in known


def test_blocked_scenes_link_to_their_missing_assets():
    assert "data-goto-asset" in SCRIPT and "sc.missing" in SCRIPT and ".status.blocked" in STYLE


# ---------- stil kılavuzu (Faz 13) ----------

def test_the_style_tab_has_its_workflow_and_uses_the_style_endpoints():
    for text in ("Stil kılavuzu", "Işık öneki", "Hareket tarzı", "Yeni sürüm olarak kaydet", "Etkisini önizle",
                 "Etkinleştir", "Etkilenen çekimleri hemen yeniden üret", "Örnekle doldur"):
        assert text in HTML, text
    calls = api_calls()
    for needed in (("get", "/api/projects/{}/style"), ("post", "/api/projects/{}/style"),
                   ("post", "/api/projects/{}/style/preview"), ("post", "/api/projects/{}/style/activate")):
        assert needed in calls, needed
    assert 'id="pane-style"' in HTML


# ---------- sekanslar (Faz 11) ----------

def test_scenes_are_grouped_under_sequences_and_the_sequence_tools_use_their_endpoints():
    for text in ("Sekansın tamamı", "Yalnızca bu çekim", "Bütçe tavanı", "Tavanı kaydet", "Stem yükle", "Tavan doldu",
                 "Sekanssız"):
        assert text in HTML, text
    assert "seq-group" in SCRIPT and "seqHead" in SCRIPT and 'id="seqbar"' in HTML
    calls = api_calls()
    for needed in (("patch", "/api/sequences/{}"), ("post", "/api/sequences/{}/tracks"),
                   ("delete", "/api/timeline-tracks/{}")):
        assert needed in calls, needed
    assert re.search(r"fetch\(`/api/sequences/\$\{q\.id\}/tracks\?", SCRIPT)                     # stem: ham gövde


# ---------- storyboard ve taslak geçiş (Faz 14) ----------

def test_the_storyboard_tab_and_the_rough_cut_use_their_endpoints():
    for text in ("Storyboard", "Kareleri hazırla", "PDF indir", "Onayla", "Değişiklik iste", "Taslak geçişi başlat",
                 "İyileştirme geçişi", "İyileştir", "Kaba kurgu", "Final", "kare onayı zorunlu"):
        assert text in HTML, text
    calls = api_calls()
    for needed in (("post", "/api/projects/{}/storyboard"), ("post", "/api/scenes/{}/board"),
                   ("post", "/api/scenes/{}/upgrade"), ("post", "/api/projects/{}/draft-pass"),
                   ("post", "/api/projects/{}/upgrade-pass"), ("patch", "/api/projects/{}"),
                   ("post", "/api/projects/{}/assemble")):
        assert needed in calls, needed
    assert "storyboard.pdf" in SCRIPT and "?kind=draft" in SCRIPT and "draft_timeline" in SCRIPT
    assert {"pending", "approved", "changes_requested"} <= js_object_keys("BOARD_TR")


# ---------- yöntem ve elle düzeltme (Faz 15) ----------

def test_the_method_and_revision_tools_use_their_endpoints():
    for text in ("Yöntem", "Previs rehberi", "Dış çekim", "Previs rehberi yükle", "Dış çekim yükle", "Elle düzeltme",
                 "Revizyon yükle", "Orijinale dön", "Düzenlenmiş ilk kare"):
        assert text in HTML, text
    calls = api_calls()
    for needed in (("post", "/api/scenes/{}/method"), ("post", "/api/variations/{}/revisions/active"),
                   ("get", "/api/variations/{}/revisions")):
        assert needed in calls, needed
    known = {re.sub(r"\{[^}]*\}", "{}", p) for p, ops in app.openapi()["paths"].items() if "post" in ops}
    for raw in re.findall(r"uploadRaw\(`/api([^`?]+)\?", SCRIPT):                         # ham gövdeyle yüklemeler
        assert "/api" + re.sub(r"\$\{[^}]*\}", "{}", raw) in known, raw
    assert len(re.findall(r"uploadRaw\(`/api", SCRIPT)) == 4                           # dış çekim, rehber, revizyon, LUT


# ---------- verimlilik ve dışa aktarma (Faz 17) ----------

def test_the_efficiency_tab_shows_the_metrics_and_offers_every_export():
    for text in ("Verimlilik", "Kabul edilen çekim başına varyasyon", "Film dakikası başına sanal maliyet",
                 "Varyasyon payı", "Harcama payı", "Eleme nedenleri", "Test turu başarı oranı", "Ortalama düzeltme turu",
                 "Kaba kurgudan iyileştirilen", "Son kararlar", "Profesyonel dışa aktarma", "EDL (CMX3600)",
                 "OpenTimelineIO", "Çekim paketi (zip)", "Kürasyon kaydını indir"):
        assert text in HTML, text
    calls = api_calls()
    for needed in (("get", "/api/projects/{}/metrics"), ("get", "/api/projects/{}/curation")):
        assert needed in calls, needed
    known = {re.sub(r"\{[^}]*\}", "{}", p) for p, ops in app.openapi()["paths"].items() if "get" in ops}
    assert "/api/projects/{}/export" in known and "/api/projects/{}/curation.jsonl" in known
    assert "/export?format=${f}" in SCRIPT and "/curation.jsonl" in SCRIPT
    assert re.search(r'\["edl",.*\["otio",.*\["package",', SCRIPT)
    assert {"quality_gate", "not_top_k", "superseded", "interrupted", "frozen", "failed"} <= js_object_keys("PURGE_TR")
    assert {"fal_wan", "seedance", "flux_lora", "fal", "elevenlabs", "anthropic"} <= js_object_keys("KIND_TR")
    assert "v2v:" in SCRIPT                                                    # defter etiketlerinin önekleri Türkçe
