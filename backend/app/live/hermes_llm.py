"""ÜCRETLİ: LangGraph + Claude ile senaryo ayrıştırma (HERMES_MODE=llm, DRY_RUN=0, ANTHROPIC_API_KEY).

Akış: segment (sahne başlıklarından böl) -> extract (her sahne paralel, Send; yapılandırılmış çıktı)
      -> validate -> repair (en fazla 2 tur) -> sonuç. Çıktı şeması rules.parse() ile aynıdır.
Her LLM çağrısı Kill-Switch rezervasyonundan geçer (reserve -> commit/release); gerçek maliyet kullanım
bilgisinden (token) hesaplanır.
"""
import re
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from app.budget import killswitch, pricing
from app.config import settings
from app.hermes.rules import SLUG, rules_cast
from app.live import guard

Emotion = Literal["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]
MAX_REPAIR_ROUNDS = 2
MAX_CONCURRENCY = 4              # Send ile açılan paralel sahne çağrıları için üst sınır (hız sınırlarına takılmamak için)
OUTPUT_TOKENS_ESTIMATE = 4000    # rezervasyon tahmini; gerçek maliyet kullanım bilgisinden gelir
CHARS_PER_TOKEN = 3              # Türkçe için tutucu


class Camera(BaseModel):
    shot: Literal["extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder", "pov"]
    movement: Literal["static", "pan", "tilt", "dolly_in", "dolly_out", "tracking", "handheld", "crane", "orbit"]
    angle: Literal["eye", "low", "high", "dutch", "top_down"] = "eye"
    lens_mm: int = Field(35, ge=12, le=200)


class Line(BaseModel):
    character: str
    line: str
    emotion: Emotion


class Shot(BaseModel):
    description: str
    visual_prompt: str = Field(description="Görüntü modeli için İngilizce, somut görsel tarif")
    camera: Camera
    emotion: Emotion
    characters: list[str]
    dialogue: list[Line] = []
    duration_s: float = Field(5.0, ge=2, le=10)
    action_complexity: float = Field(ge=0, le=1)


class Breakdown(BaseModel):
    shots: list[Shot]


SYSTEM = ("Bir animasyon filmi için çekim listesi hazırlıyorsun. Sahneyi 2-10 saniyelik çekimlere böl. "
          "Yalnızca verilen kadrodaki karakter adlarını kullan. action_complexity: dövüş, koşu, çarpışma için 0.7 üstü.")


def merge_by_idx(old: list, new: list) -> list:
    d = dict(old)
    d.update(dict(new))          # repair düğümü aynı idx'i ezer
    return sorted(d.items())


class St(TypedDict):
    chunks: list[str]
    cast: list[str]
    results: Annotated[list[tuple[int, dict]], merge_by_idx]
    errors: list[str]
    attempts: int


def estimate_call(*texts: str) -> float:
    input_tokens = sum(len(t) for t in texts) // CHARS_PER_TOKEN + 200
    return pricing.estimate_llm(input_tokens, OUTPUT_TOKENS_ESTIMATE)


def actual_cost(raw) -> float | None:
    usage = getattr(raw, "usage_metadata", None) or {}
    if "input_tokens" not in usage:
        return None
    return pricing.estimate_llm(usage["input_tokens"], usage.get("output_tokens", 0))


def validate_breakdown(idx: int, br: dict, cast: list[str]) -> list[str]:
    """Hata satırları "idx: ..." biçimindedir (repair düğümü idx'i buradan okur)."""
    if not br["shots"]:
        return [f"{idx}: çekim listesi boş"]
    errs = []
    for sh in br["shots"]:
        names = set(sh["characters"]) | {d["character"] for d in sh["dialogue"]}
        unknown = names - set(cast)
        if unknown:
            errs.append(f"{idx}: kadroda olmayan karakter {sorted(unknown)}")
    return errs


def parse_with_llm(text: str, project_id: str, llm=None) -> dict:
    """`llm`: invoke(mesajlar) -> {"raw", "parsed"} döndüren nesne (include_raw=True ile yapılandırılmış çıktı).
    Verilmezse ChatAnthropic kurulur. guard her durumda ilk iş çalışır."""
    guard("anthropic_api_key")
    from langgraph.graph import END, START, StateGraph      # ücretli bağımlılıklar: yalnızca burada, işlev içinde
    from langgraph.types import Send
    if llm is None:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=settings.hermes_model, api_key=settings.anthropic_api_key, temperature=0.2,
                            max_tokens=8000).with_structured_output(Breakdown, include_raw=True)

    def ask(messages, estimate: float) -> dict:
        with killswitch.spend(project_id, estimate, f"anthropic:{settings.hermes_model}") as charge:
            out = llm.invoke(messages)
            cost = actual_cost(out.get("raw"))
            if cost is not None:
                charge.actual = cost
        if out.get("parsed") is None:                        # ücret yazıldı; çıktı kullanılamıyor
            raise ValueError(f"LLM çıktısı şemaya uymadı: {out.get('parsing_error')}")
        return out["parsed"].model_dump()

    cast = rules_cast(text)
    chunks = [c.strip() for c in re.split(r"(?m)^(?=(?:İÇ\.|DIŞ\.|İÇ/DIŞ\.|INT\.|EXT\.))", text) if c.strip()]

    def extract(x):
        human = f"Kadro: {', '.join(x['cast'])}\n\n{x['text']}"
        return {"results": [(x["idx"], ask([("system", SYSTEM), ("human", human)], estimate_call(SYSTEM, human)))]}

    def validate(st: St):
        errs = []
        for idx, br in st["results"]:
            errs += validate_breakdown(idx, br, st["cast"])
        return {"errors": errs}

    def repair(st: St):
        fixed = []
        for idx in sorted({int(e.split(":")[0]) for e in st["errors"]}):
            human = f"Kadro: {st['cast']}\nHatalar: {st['errors']}\n\n{st['chunks'][idx]}"
            fixed.append((idx, ask([("system", SYSTEM + " Hataları düzelt."), ("human", human)],
                                   estimate_call(SYSTEM, human))))
        return {"results": fixed, "attempts": st["attempts"] + 1}

    g = StateGraph(St)
    g.add_node("extract", extract)
    g.add_node("validate", validate)
    g.add_node("repair", repair)
    g.add_conditional_edges(START, lambda st: [Send("extract", {"idx": i, "text": c, "cast": st["cast"]})
                                               for i, c in enumerate(st["chunks"])], ["extract"])
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", lambda st: "repair" if st["errors"] and st["attempts"] < MAX_REPAIR_ROUNDS else END,
                            ["repair", END])
    g.add_edge("repair", "validate")
    final = g.compile().invoke({"chunks": chunks, "cast": cast, "results": [], "errors": [], "attempts": 0},
                               config={"max_concurrency": MAX_CONCURRENCY})

    # Kural tabanlı ayrıştırıcıyla aynı numaralandırma: yalnızca çekimi olan sahneler sayılır
    shots, scene_no = [], 0
    for idx, br in sorted(final["results"], key=lambda r: r[0]):
        if not br["shots"]:
            continue
        scene_no += 1
        heading = chunks[idx].splitlines()[0] if SLUG.match(chunks[idx]) else "SAHNE"
        for hi, sh in enumerate(br["shots"], start=1):
            sh = {**sh, "characters": [c for c in sh["characters"] if c in cast]}      # onarılamayan uydurma ad kadroya girmez
            shots.append({**sh, "scene_id": f"S{scene_no:03d}_SH{hi:02d}", "heading": heading})
    return {"cast": cast, "shots": shots}
