"""Panel asistanının Claude çağrısı (Anthropic Messages API, araç kullanımı). Ücretli: her çağrı önce
`guard("anthropic_api_key")` çağırır ve bütçe rezervasyonundan geçer (tahmin rezerve edilir, gerçek bedel yanıtın belirteç
kullanımından yazılır). Kalan bütçe tahmine yetmiyorsa rezervasyon denenmez (asistan yüzünden proje donmasın).
`anthropic` paketi yalnızca fonksiyon içinde yüklenir. Döngü ve araçlar `app.assistant`'tadır.

Model ve çaba Ayarlar'dan (ASSISTANT_MODEL, ASSISTANT_EFFORT). Düşünme uyarlamalıdır (modelin varsayılanı); örnekleme
parametresi gönderilmez. Sistem istemi ve araçlar önbelleğe alınır; ret durumunda sunucu tarafı yedek model devreye girer."""
import json
import logging

from app.budget import history, killswitch, pricing
from app.config import settings
from app.engine.providers import TransientError
from app.live import guard

log = logging.getLogger("hellgrind.live.assistant")

MAX_OUTPUT_TOKENS = 8000
CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1100              # 768 px genişliğinde bir kare (yaklaşık)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CACHE_READ = 0.1                 # önbellekten okunan girdi, normal girdi fiyatının oranı
CACHE_WRITE = 1.25


def estimate_tokens(system: str, tool_defs: list, messages: list) -> int:
    images = 0

    def walk(x):
        nonlocal images
        if isinstance(x, dict):
            if x.get("type") == "image":
                images += 1
                return 0
            return sum(walk(v) for v in x.values())
        if isinstance(x, list):
            return sum(walk(v) for v in x)
        return len(x) if isinstance(x, str) else 0
    chars = len(system) + len(json.dumps(tool_defs)) + walk(messages)
    return chars // CHARS_PER_TOKEN + images * IMAGE_TOKENS


def usage_usd(usage, model: str) -> tuple[int, int, float]:
    """(girdi, çıktı, $): önbellek yazımı ve okuması fiyat oranlarıyla girdiye çevrilir."""
    fresh = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    equivalent = round(fresh + written * CACHE_WRITE + read * CACHE_READ)
    return fresh + written + read, out, pricing.estimate_llm(equivalent, out, model=model)


def _consent(project_id: str) -> None:
    """Yönetmen asistana yazdı: bu çağrı onun isteğidir. İzin akışı (varsa) "llm" türünü bir kez için açık sayar."""
    try:
        from app.budget import consent                 # type: ignore[attr-defined]
    except ImportError:
        return
    try:
        consent.allow(project_id, "llm", consent.target_of(), "once", None, by="assistant")
    except Exception:                                   # noqa: BLE001 — izin yazılamazsa rezervasyon yine sorar
        log.warning("asistan çağrısı izin akışına yazılamadı", exc_info=True)


def ask(project_id: str, system: str, tool_defs: list, messages: list) -> dict:
    """Bir model çağrısı. {"content": [blok sözlükleri], "stop_reason", "model", "input_tokens", "output_tokens", "usd"}."""
    api_key = guard("anthropic_api_key")
    import anthropic                                   # ücretli paket: yalnızca fonksiyon içinde
    from app.assistant.core import BudgetShort
    from app.db import session_scope
    from app.models import Project
    model = settings.assistant_model
    estimate = pricing.estimate_llm(estimate_tokens(system, tool_defs, messages), MAX_OUTPUT_TOKENS, model=model)
    with session_scope() as s:
        p = s.get(Project, project_id)
        remaining = float(p.budget_limit_usd) - float(p.spent_usd) - float(p.reserved_usd)
    if remaining < estimate:
        raise BudgetShort(f"kalan bütçe ({remaining:.2f} $) bir yanıtın tahminine ({estimate:.2f} $) yetmiyor; "
                          "Ayarlar'dan bütçeyi artırın.")
    _consent(project_id)
    client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    try:
        with killswitch.spend(project_id, estimate, "anthropic") as charge:
            resp = client.beta.messages.create(
                model=model, max_tokens=MAX_OUTPUT_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tool_defs, messages=messages, betas=[FALLBACK_BETA],
                extra_body={"output_config": {"effort": settings.assistant_effort}, "fallbacks": "default",
                            "cache_control": {"type": "ephemeral"}})
            tokens_in, tokens_out, usd = usage_usd(resp.usage, model)
            history.step(resp.model, resp.id, input_tokens=tokens_in, output_tokens=tokens_out, purpose="assistant")
            charge.actual = usd
    except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
        raise TransientError(f"Claude API geçici olarak yanıt vermedi: {e}") from e
    return {"content": [b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            "stop_reason": resp.stop_reason, "model": resp.model, "input_tokens": tokens_in,
            "output_tokens": tokens_out, "usd": float(usd)}
