"""Ücretli kilit (değişmez kurallar 1, 2 ve 5): hiçbir ücretli çağrı ücretsiz modda ya da anahtarsız çalışamaz.

Hiçbir test DRY_RUN=0 ile gerçek bir şey çalıştırmaz: yalnızca bellekteki ayar bayrağı çevrilir ve kilidin tepkisi ölçülür.
"""
import ast
import pathlib
import subprocess
import sys

import pytest

from app.config import Settings, settings
from app.engine.providers import LocalProvider, get_provider
from app.live import PAID_KEYS, PaidCallBlocked, guard

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
BACKEND = APP.parent
PAID_PACKAGES = {"fal_client", "langgraph", "langchain", "langchain_anthropic", "langchain_core", "anthropic",
                 "elevenlabs", "pinecone", "openai"}


def python_files(root=APP):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


# ---------- guard ----------

@pytest.mark.parametrize("key", PAID_KEYS)
def test_free_mode_blocks_every_key_even_when_the_key_is_set(monkeypatch, key):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, key, "gerçek-görünümlü-anahtar")
    with pytest.raises(PaidCallBlocked, match="DRY_RUN=1"):
        guard(key)


@pytest.mark.parametrize("key", PAID_KEYS)
def test_paid_mode_blocks_an_empty_key(monkeypatch, key):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, key, "")
    with pytest.raises(PaidCallBlocked, match=key.upper()):
        guard(key)


@pytest.mark.parametrize("key", PAID_KEYS)
def test_the_key_is_handed_out_only_when_both_the_mode_and_the_key_allow(monkeypatch, key):
    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(settings, key, "test-anahtari")
    assert guard(key) == "test-anahtari"


def test_an_unknown_key_name_is_a_programming_error_not_a_silent_pass():
    with pytest.raises(ValueError):
        guard("pinecone_api_key")


def test_the_guard_reads_settings_at_call_time(monkeypatch):
    monkeypatch.setattr(settings, "fal_key", "k")
    monkeypatch.setattr(settings, "dry_run", False)
    assert guard("fal_key") == "k"
    monkeypatch.setattr(settings, "dry_run", True)
    with pytest.raises(PaidCallBlocked):
        guard("fal_key")


def test_paid_call_blocked_is_a_runtime_error():
    assert issubclass(PaidCallBlocked, RuntimeError)


# ---------- varsayılanlar ----------

def test_free_mode_is_the_default_and_paid_keys_default_to_empty(monkeypatch):
    for name in ("DRY_RUN", "HERMES_MODE", "TTS_ENGINE", "VAST_API_KEY", "H3_API_KEY", "H3_ENDPOINT_URL", "FAL_KEY",
                 "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)       # konteyner ortamı yönetmenin .env'inden gelir; varsayılanlar sınanır
    defaults = Settings(_env_file=None)
    assert defaults.dry_run is True and defaults.hermes_mode == "rules" and defaults.tts_engine == "espeak"
    assert (defaults.vast_api_key, defaults.h3_api_key, defaults.fal_key, defaults.anthropic_api_key,
            defaults.elevenlabs_api_key) == ("", "", "", "", "")
    assert (defaults.h3_auto_start, defaults.h3_allow_nordic, defaults.h3_idle_stop_minutes) == (False, False, 10)
    # RunPod uç noktası silindi: kendi barındırdığımız H3 kapalı; video fal.ai'de (taslak H3 Max, final Seedance 2.5)
    assert defaults.h3_platform == "kapali" and defaults.runpod_api_key == "" and defaults.runpod_idle_timeout_s == 60
    assert defaults.h3_endpoint_url == ""
    assert (defaults.video_draft_engine, defaults.video_final_engine, defaults.keyframe_platform) == \
        ("h3max", "seedance25", "fal")
    # test aşaması: en ucuz varsayılanlar (H3 Max 480P, Seedance taslağı 480p; 720p yalnızca taslak kapalıyken)
    assert (defaults.h3max_resolution, defaults.h3max_prompt_expansion, defaults.seedance25_resolution,
            defaults.seedance25_audio, defaults.seedance25_draft) == ("480P", "disabled", "720p", False, True)
    assert (defaults.video_input_mode, defaults.scene_render_mode, defaults.upscale_endpoint) == \
        ("reference", "whole_scene", "fal-ai/bytedance-upscaler/upscale/video")


def test_the_test_run_itself_is_in_free_mode():
    assert settings.dry_run is True


@pytest.mark.parametrize("engine", ["h3", "h3max", "seedance25", "fal_wan", "seedance", "higgsfield"])
def test_get_provider_returns_the_local_provider_in_free_mode(monkeypatch, engine):
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "fal_key", "anahtar")
    monkeypatch.setattr(settings, "h3_api_key", "anahtar")
    assert isinstance(get_provider(engine), LocalProvider)


# ---------- ücretli kod yalnızca app/live altında ve yalnızca fonksiyon içinde ----------

def imported_roots(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((a.name.split(".")[0], node) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0], node


def test_paid_packages_are_imported_only_under_app_live():
    offenders = [f"{p.relative_to(APP)}: {root}" for p in python_files() if "live" not in p.relative_to(APP).parts
                 for root, _ in imported_roots(ast.parse(p.read_text())) if root in PAID_PACKAGES]
    assert offenders == []


def test_paid_packages_inside_app_live_are_only_imported_inside_functions():
    offenders = []
    for p in python_files(APP / "live"):
        tree = ast.parse(p.read_text())
        for node in tree.body:                                              # modül düzeyindeki ifadeler
            for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    roots = [a.name.split(".")[0] for a in sub.names] if isinstance(sub, ast.Import) else [(sub.module or "").split(".")[0]]
                    offenders += [f"{p.name}: {r}" for r in roots if r in PAID_PACKAGES]
    assert offenders == []


def test_the_rest_of_the_code_loads_the_live_package_only_inside_functions():
    """app.live'ı modül düzeyinde içe aktaran bir modül, ücretsiz modda ücretli kodu yüklerdi."""
    offenders = []
    for p in python_files():
        if "live" in p.relative_to(APP).parts:
            continue
        for node in ast.parse(p.read_text()).body:
            for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []:
                if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("app.live"):
                    offenders.append(str(p.relative_to(APP)))
    assert offenders == []


def test_secret_key_values_are_read_only_through_the_guard():
    """Anahtar DEĞERLERİ yalnızca config.py'de tanımlanır ve live/__init__.py'deki guard() ile okunur. Anahtar ADLARINI
    guard()'a dize olarak vermek serbesttir; `settings.fal_key` gibi değer okumak ya da getattr ile almak değildir."""
    allowed = {pathlib.Path("config.py"), pathlib.Path("live/__init__.py")}
    offenders = []
    for p in python_files():
        rel = p.relative_to(APP)
        if rel in allowed:
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in PAID_KEYS:
                offenders.append(f"{rel}:{node.lineno} .{node.attr}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in PAID_KEYS:
                offenders.append(f"{rel}:{node.lineno} getattr({node.args[1].value})")
    assert offenders == []


def test_importing_the_application_in_free_mode_never_loads_a_paid_package():
    code = ("import sys\nimport app.main, app.worker.tasks, app.engine.iteration, app.post.pipeline, app.hermes.ingest\n"
            "paid = {'fal_client','langgraph','langchain','langchain_anthropic','anthropic','elevenlabs','pinecone'}\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in paid))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BACKEND,
                         env={"PATH": "", "PYTHONPATH": str(BACKEND), "DRY_RUN": "1"}).stdout.strip()
    assert out == "[]"


def test_live_modules_are_not_loaded_in_free_mode():
    code = "import sys, app.main, app.worker.tasks; print(sorted(m for m in sys.modules if m.startswith('app.live.')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BACKEND,
                         env={"PATH": "", "PYTHONPATH": str(BACKEND), "DRY_RUN": "1"}).stdout.strip()
    assert out == "[]"


# ---------- sırlar ----------

def test_env_is_never_committed():
    root = BACKEND.parent
    if not (root / ".gitignore").exists():
        pytest.skip("depo kökü bağlı değil")
    assert ".env" in (root / ".gitignore").read_text().splitlines()
    env_example = (root / ".env.example").read_text()
    for key in ("VAST_API_KEY", "H3_API_KEY", "FAL_KEY", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        assert f"\n{key}=\n" in env_example + "\n"                              # örnek dosyada anahtarlar boş
    assert "DRY_RUN=1" in env_example
