"""Anthropic istemcisi (Hermes ayrıştırıcı ve sahne istemi yazıcı ortak). Örnekleme parametreleri (temperature, top_p,
top_k) HİÇBİR modelde gönderilmez: claude-opus-5-5 "`temperature` is deprecated for this model" (400) döner,
langchain_anthropic claude-sonnet-5-5'te varsayılan dışı değeri reddeder. langchain_anthropic yalnızca fonksiyon içinde
yüklenir; anahtar çağıranın guard() sonucudur."""

SAMPLING = ("temperature", "top_p", "top_k")


def client_kwargs(model: str, api_key: str, max_tokens: int, max_retries: int = 2) -> dict:
    """ChatAnthropic kurulum parametreleri; örnekleme parametresi yok."""
    kwargs = {"model": model, "api_key": api_key, "max_tokens": max_tokens, "max_retries": max_retries}
    assert not SAMPLING & kwargs.keys()
    return kwargs


def make(model: str, api_key: str, max_tokens: int, max_retries: int = 2):
    from langchain_anthropic import ChatAnthropic            # ücretli paket: yalnızca fonksiyon içinde
    return ChatAnthropic(**client_kwargs(model, api_key, max_tokens, max_retries))
