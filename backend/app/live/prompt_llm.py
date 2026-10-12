"""Sahne istemi yazıcının LLM çağrısı (Anthropic, Hermes modeli). Ücretli: her çağrı önce `guard("anthropic_api_key")`
çağırır ve bütçe rezervasyonundan geçer (tahmin rezerve edilir, gerçek bedel yanıtın belirteç kullanımından yazılır).
langchain_anthropic yalnızca fonksiyon içinde yüklenir. Girdi ve doğrulama `app.engine.prompt_writer`'dadır."""
import logging

from app.budget import history, killswitch, pricing
from app.config import settings
from app.engine.providers import TransientError
from app.live import guard

log = logging.getLogger("hellgrind.live.prompt_llm")

MAX_OUTPUT_TOKENS = 1400
MAX_ATTEMPTS = 3                 # geçici hata (429, 5xx, zaman aşımı) yeniden denemesi
CHARS_PER_TOKEN = 4


def _transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(w in text for w in ("rate", "429", "overloaded", "529", "timeout", "temporar", "connection", "503", "502"))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content or [])


def ask(project_id: str, system: str, user: str) -> dict:
    """Bir yazım. {"text", "model", "input_tokens", "output_tokens", "usd"}."""
    api_key = guard("anthropic_api_key")
    from langchain_anthropic import ChatAnthropic            # ücretli paket: yalnızca fonksiyon içinde
    llm = ChatAnthropic(model=settings.hermes_model, api_key=api_key, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.4,
                        max_retries=2)
    estimate = pricing.estimate_llm((len(system) + len(user)) // CHARS_PER_TOKEN, MAX_OUTPUT_TOKENS)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with killswitch.spend(project_id, estimate, "anthropic") as charge:
                raw = llm.invoke([("system", system), ("human", user)])
                usage = getattr(raw, "usage_metadata", None) or {}
                meta = getattr(raw, "response_metadata", None) or {}
                model = meta.get("model_name") or meta.get("model") or settings.hermes_model
                history.step(model, getattr(raw, "id", None) or meta.get("id"), input_tokens=usage.get("input_tokens"),
                             output_tokens=usage.get("output_tokens"), purpose="scene_prompt")
                usd = (pricing.estimate_llm(usage["input_tokens"], usage["output_tokens"]) if usage else estimate)
                charge.actual = usd
            return {"text": _text(raw.content), "model": model, "input_tokens": usage.get("input_tokens") or 0,
                    "output_tokens": usage.get("output_tokens") or 0, "usd": float(usd)}
        except Exception as e:                                # noqa: BLE001
            if killswitch.ProjectFrozen in type(e).__mro__ or not _transient(e):
                raise
            log.warning("istem yazıcı LLM geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
    raise TransientError("İstem yazıcı LLM çağrısı yeniden denemelere rağmen başarısız")
