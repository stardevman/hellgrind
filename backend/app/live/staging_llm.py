"""ÜCRETLİ: sahneleme planını Claude yazar (Anthropic, istem yazıcının modeli). Her çağrı önce `guard("anthropic_api_key")`
çağırır ve bütçe rezervasyonundan geçer; ücretli modda rezervasyondan önce yönetmenin iznini ister (app.budget.consent,
tür "llm"). İzin yoksa çağrı yapılmaz, istek 428 döner, panel izin kartını açar ve izin verilince isteği yineler.
langchain_anthropic yalnızca fonksiyon içinde yüklenir. İstek ve doğrulama `app/post/staging_writer.py`'dedir."""
import logging

from app.budget import history, killswitch, pricing
from app.engine.providers import TransientError
from app.live import guard

log = logging.getLogger("hellgrind.live.staging_llm")

MAX_OUTPUT_TOKENS = 2500
MAX_ATTEMPTS = 3                 # geçici hata (429, 5xx, zaman aşımı) yeniden denemesi
CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1100              # 1024 px'lik kare için tahmin (genişlik × yükseklik / 750)
LABEL = "anthropic:staging"


def _chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(len(p.get("text", "")) for p in content or [] if isinstance(p, dict))      # görsel ayrıca sayılır


def estimate(system: str, messages: list, image: bool) -> float:
    chars = len(system) + sum(_chars(m[1]) for m in messages)
    tokens = chars // CHARS_PER_TOKEN + (IMAGE_TOKENS if image else 0)
    from app.engine.prompt_writer import writer_model
    return pricing.estimate_llm(tokens, MAX_OUTPUT_TOKENS, model=writer_model())


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content or [])


def _transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(w in text for w in ("rate", "429", "overloaded", "529", "timeout", "temporar", "connection", "503", "502"))


def ask(project_id: str, scene_id: str, system: str, text: str, image_b64: str | None,
        history_: list | None = None) -> dict:
    """Bir yazım. history_: önceki (rol, içerik) çiftleri (onarım turu). {"text", "model", "usd", "messages"}."""
    api_key = guard("anthropic_api_key")
    from app.engine.prompt_writer import writer_model
    from app.live.anthropic_client import make
    model_id = writer_model()
    llm = make(model_id, api_key, MAX_OUTPUT_TOKENS)
    first = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}] if image_b64 else []
    messages = list(history_ or [("human", [*first, {"type": "text", "text": text}])])
    if history_:
        messages.append(("human", text))
    est = estimate(system, messages, bool(image_b64))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with killswitch.spend(project_id, est, LABEL, scene_id=scene_id) as charge:
                raw = llm.invoke([("system", system), *messages])
                usage = getattr(raw, "usage_metadata", None) or {}
                meta = getattr(raw, "response_metadata", None) or {}
                model = meta.get("model_name") or meta.get("model") or model_id
                history.step(model, getattr(raw, "id", None) or meta.get("id"), input_tokens=usage.get("input_tokens"),
                             output_tokens=usage.get("output_tokens"), purpose="staging_plan")
                usd = (pricing.estimate_llm(usage["input_tokens"], usage["output_tokens"], model=model_id)
                       if usage else est)
                charge.actual = usd
            reply = _text(raw.content)
            return {"text": reply, "model": model, "usd": float(usd), "messages": [*messages, ("ai", reply)]}
        except Exception as e:                                # noqa: BLE001
            if killswitch.ProjectFrozen in type(e).__mro__ or not _transient(e):
                raise
            log.warning("sahneleme LLM geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
    raise TransientError("Sahneleme LLM çağrısı yeniden denemelere rağmen başarısız")
