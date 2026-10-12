"""ÜCRETLİ senaryo ayrıştırma (Hermes LLM modu). Bu modül hiçbir yerde çalıştırılmadı.

LangGraph akışı: senaryo sahne parçalarına bölünür; `Send` ile paralel `extract` (LLM), sonra `validate` (yalnızca kod), sonra
geçersiz kalan sahneler için en çok 2 tur `repair` (LLM) ve `finish`. Çıktı şeması kural tabanlı ayrıştırıcıyla (rules.parse)
aynıdır: {cast, shots: [{scene_id, heading, description, visual_prompt, camera, emotion, characters, dialogue, duration_s,
action_complexity}]}.

Rol paylaşımı: LLM metni ve bölümlemeyi (çekimler, açıklamalar, görsel istem, duygu, hangi replik hangi çekimde) üretir;
kamera, aksiyon yoğunluğu, süre ve sahne/çekim numaraları kodla, kural tabanlı ayrıştırıcıyla aynı fonksiyonlarla hesaplanır.
Replikler doğrulanır: LLM, senaryodaki replikleri eksiltemez, değiştiremez ya da uyduramaz (kelime düzeyinde karşılaştırılır).
Onarım sonunda hâlâ geçersiz olan sahne, ücretsiz kural tabanlı ayrıştırıcıya düşer.

Bütçe: her LLM çağrısı `spend()` içindedir; rezervasyon belirteç tahminiyle, gerçek tutar yanıtın kullanım bilgisiyle işlenir.
Her giriş noktası önce `guard("anthropic_api_key")` çağırır; langgraph ve langchain yalnızca fonksiyon içinde yüklenir.
"""
import logging
import operator
import re
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

from app.budget import history, killswitch, pricing
from app.config import settings
from app.engine.providers import TransientError
from app.hermes import rules
from app.live import guard

log = logging.getLogger("hellgrind.live.hermes")

MAX_REPAIR_ROUNDS = 2
MAX_PARALLEL_CALLS = 4          # paralel LLM çağrısı üst sınırı (hız sınırı ve ani harcama için)
MAX_ATTEMPTS = 3
MAX_OUTPUT_TOKENS = 2500
PROMPT_OVERHEAD_TOKENS = 700
CHARS_PER_TOKEN = 3             # Türkçe için tutucu tahmin
EMOTIONS = ["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]
KINDS = ["establish", "action", "dialogue", "beat"]

SYSTEM = f"""Sen bir senaryo ayrıştırıcısın. Sana tek bir sahne verilir; onu çekimlere bölersin.
Kurallar:
- Her aksiyon paragrafı yeni bir çekimdir. Sahnenin ilk aksiyonsuz çekimi "establish", koşma/atlama/patlama gibi yoğun aksiyon "action",
  yalnızca konuşma için açılan çekim "dialogue", diğerleri "beat" olur.
- Replikleri AYNEN koru: ekleme, silme, değiştirme yok. Her repliği, konuşan karakterin göründüğü çekime ata; bir çekimde en çok {rules.MAX_LINES_PER_SHOT} replik.
- Karakter adları yalnızca verilen kadrodan olur.
- Duygu şunlardan biri: {", ".join(EMOTIONS)}.
- description: çekimde ne olduğunu anlatan kısa Türkçe cümle. visual_prompt: yalnızca GÖRÜNTÜYÜ anlatır (mekân, ışık, hareket); replik metni içermez.
"""


class LineDraft(BaseModel):
    character: str
    line: str
    emotion: Literal[tuple(EMOTIONS)] | None = None          # type: ignore[valid-type]


class ShotDraft(BaseModel):
    description: str
    visual_prompt: str = ""
    kind: Literal[tuple(KINDS)] = "beat"                      # type: ignore[valid-type]
    emotion: Literal[tuple(EMOTIONS)] = "neutral"             # type: ignore[valid-type]
    characters: list[str] = Field(default_factory=list)
    dialogue: list[LineDraft] = Field(default_factory=list)


class SceneDraft(BaseModel):
    shots: list[ShotDraft]


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class State(TypedDict, total=False):
    project_id: str
    cast: list[str]
    chunks: list[str]
    sequences: list                          # parça sırası -> sekans anahtarı (kodla, kural tabanlı ayrıştırıcının kuralıyla)
    drafts: Annotated[dict, _merge]          # sahne sırası -> taslak (paralel extract ve repair birleştirir)
    problems: dict                           # sahne sırası -> hata listesi (her validate geçişinde yeniden yazılır)
    passes: int
    result: dict


# ---------- doğrulama (yalnızca kod) ----------

def _words(text: str) -> list[str]:
    return re.findall(r"\w+", rules.tr_lower(text))


def expected_dialogue(chunk: str) -> list[tuple[str, str]]:
    """Senaryo parçasındaki replikler, kural tabanlı ayrıştırıcının gözüyle: [(karakter, kelime)]."""
    return [(b["name"], w) for b in rules.tokenize(chunk) if b["type"] == "speech" for w in _words(b["text"])]


def problems_of(draft: SceneDraft | None, chunk: str, cast: list[str]) -> list[str]:
    if draft is None:
        return ["Yanıt beklenen biçimde okunamadı."]
    errors = []
    if not draft.shots:
        errors.append("Sahnede hiç çekim yok.")
    for i, shot in enumerate(draft.shots, start=1):
        if not shot.description.strip():
            errors.append(f"Çekim {i}: açıklama boş.")
        if len(shot.dialogue) > rules.MAX_LINES_PER_SHOT:
            errors.append(f"Çekim {i}: en çok {rules.MAX_LINES_PER_SHOT} replik olabilir.")
        for name in [*shot.characters, *(l.character for l in shot.dialogue)]:
            if name not in cast:
                errors.append(f"Çekim {i}: '{name}' kadroda yok (kadro: {', '.join(cast) or 'boş'}).")
        if any(not l.line.strip() for l in shot.dialogue):
            errors.append(f"Çekim {i}: boş replik var.")
    actual = [(l.character, w) for shot in draft.shots for l in shot.dialogue for w in _words(l.line)]
    if actual != expected_dialogue(chunk):
        errors.append("Replikler senaryodakiyle aynı değil: eksik, fazla ya da değiştirilmiş replik var; "
                      "tüm replikleri aynen, sırasıyla ve doğru karakterle koru.")
    return errors


# ---------- LLM çağrısı (bütçeli) ----------

def _transient(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return (isinstance(status, int) and (status == 429 or status >= 500)) or \
        type(exc).__name__ in ("APIConnectionError", "APITimeoutError") or isinstance(exc, (TimeoutError, TransientError))


def make_llm(api_key: str):
    from app.live.anthropic_client import make             # örnekleme parametresi gönderilmez
    return make(settings.hermes_model, api_key, MAX_OUTPUT_TOKENS)


def _ask(llm, project_id: str, prompt: str) -> SceneDraft | None:
    """Bir LLM çağrısı. Rezervasyon belirteç tahminiyle alınır; gerçek maliyet yanıtın kullanım bilgisinden yazılır."""
    estimate = pricing.estimate_llm(PROMPT_OVERHEAD_TOKENS + len(prompt) // CHARS_PER_TOKEN, MAX_OUTPUT_TOKENS)
    structured = llm.with_structured_output(SceneDraft, include_raw=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with killswitch.spend(project_id, estimate, "anthropic") as charge:
                answer = structured.invoke([("system", SYSTEM), ("human", prompt)])
                raw = answer["raw"]
                usage = getattr(raw, "usage_metadata", None) or {}
                meta = getattr(raw, "response_metadata", None) or {}
                history.step(meta.get("model_name") or meta.get("model") or settings.hermes_model,
                             getattr(raw, "id", None) or meta.get("id"), input_tokens=usage.get("input_tokens"),
                             output_tokens=usage.get("output_tokens"))
                charge.actual = (pricing.estimate_llm(usage["input_tokens"], usage["output_tokens"])
                                 if usage else estimate)
            return answer.get("parsed")
        except Exception as e:                                # noqa: BLE001
            if killswitch.ProjectFrozen in type(e).__mro__ or not _transient(e):
                raise
            log.warning("LLM geçici hata (deneme %d/%d): %s", attempt, MAX_ATTEMPTS, e)
    raise TransientError("LLM çağrısı yeniden denemelere rağmen başarısız")


def _extract_prompt(chunk: str, cast: list[str]) -> str:
    return f"Kadro: {', '.join(cast) or '(yok)'}\n\nSahne:\n{chunk}"


def _repair_prompt(chunk: str, cast: list[str], draft: SceneDraft | None, errors: list[str]) -> str:
    previous = draft.model_dump_json(indent=1) if draft else "(okunamadı)"
    return (f"{_extract_prompt(chunk, cast)}\n\nÖnceki çıktın şu hatalar yüzünden reddedildi:\n- " + "\n- ".join(errors)
            + f"\n\nÖnceki çıktı:\n{previous}\n\nHataları düzelterek sahneyi yeniden ayrıştır.")


# ---------- grafik ----------

def build_graph(llm):
    from langgraph.graph import END, START, StateGraph        # ücretli paket: yalnızca fonksiyon içinde
    from langgraph.types import Send

    def fan_out(state: State):
        return [Send("extract", {"index": i, "chunk": chunk, "cast": state["cast"], "project_id": state["project_id"]})
                for i, chunk in enumerate(state["chunks"])]

    def extract(job: dict):
        return {"drafts": {job["index"]: _ask(llm, job["project_id"], _extract_prompt(job["chunk"], job["cast"]))}}

    def validate(state: State):
        problems = {i: errs for i, chunk in enumerate(state["chunks"])
                    if (errs := problems_of(state["drafts"].get(i), chunk, state["cast"]))}
        return {"problems": problems, "passes": state.get("passes", 0) + 1}

    def route(state: State):
        if state["problems"] and state["passes"] <= MAX_REPAIR_ROUNDS:       # 1. geçiş -> 1. onarım, 2. geçiş -> 2. onarım
            return [Send("repair", {"index": i, "chunk": state["chunks"][i], "cast": state["cast"], "errors": errs,
                                    "draft": state["drafts"].get(i), "project_id": state["project_id"]})
                    for i, errs in state["problems"].items()]
        return "finish"

    def repair(job: dict):
        prompt = _repair_prompt(job["chunk"], job["cast"], job["draft"], job["errors"])
        return {"drafts": {job["index"]: _ask(llm, job["project_id"], prompt)}}

    def finish(state: State):
        return {"result": assemble(state)}

    g = StateGraph(State)
    for name, fn in (("extract", extract), ("validate", validate), ("repair", repair), ("finish", finish)):
        g.add_node(name, fn)
    g.add_conditional_edges(START, fan_out, ["extract"])
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", route, ["repair", "finish"])
    g.add_edge("repair", "validate")
    g.add_edge("finish", END)
    return g.compile()


def _heading(chunk: str) -> str:
    first = next((l.strip() for l in chunk.split("\n") if l.strip()), "SAHNE")
    return first if rules.is_slug(first) else "SAHNE"


def assemble(state: State) -> dict:
    """Doğrulanmış taslaklardan nihai çıktı. Sayısal alanlar ve numaralar kodla, kural tabanlı ayrıştırıcıyla aynı
    fonksiyonlarla hesaplanır; geçersiz kalan sahne ücretsiz kural tabanlı ayrıştırıcıya düşer."""
    scenes: list[list[dict]] = []
    for i, chunk in enumerate(state["chunks"]):
        heading, draft = _heading(chunk), state["drafts"].get(i)
        if state["problems"].get(i):
            log.warning("sahne %d onarım sonunda geçersiz, kural tabanlı ayrıştırıcıya düşülüyor: %s", i + 1, state["problems"][i])
            scenes.append([{**s, "heading": s["heading"]} for s in rules.parse(chunk)["shots"]])
            continue
        shots = []
        location = rules.location_name(heading)
        # Geçiş ipuçları ("CUT TO:", "CONTINUOUS:") kodla: kural tabanlı ayrıştırıcı aynı sayıda çekim bulduysa onunkiler
        # (LLM geçiş satırlarını çekimlere bağlamaz); geçişin kendisi aşağıda aynı kuralla sınıflanır (rules.classify).
        ruled = [s.get("transition_hint") for s in rules.parse(chunk)["shots"]]
        hints = ruled if len(ruled) == len(draft.shots) else [None] * len(draft.shots)
        for n, (shot, hint) in enumerate(zip(draft.shots, hints)):
            dialogue = [{"character": l.character, "line": l.line.strip(), "emotion": l.emotion or shot.emotion} for l in shot.dialogue]
            speakers = [l["character"] for l in dialogue]
            characters = list(dict.fromkeys([*shot.characters, *speakers]))
            camera, complexity = rules.camera_for(shot.kind, shot.emotion)
            description, props = rules.take_props(shot.description.strip())
            draft_shot = {"kind": shot.kind, "characters": characters, "description": description}
            shots.append({
                "heading": heading, "location": location, "props": props, "description": draft_shot["description"],
                "visual_prompt": shot.visual_prompt.strip() or rules.visual_prompt_for(heading, draft_shot),
                "camera": camera, "emotion": shot.emotion, "characters": characters, "dialogue": dialogue,
                "duration_s": rules.shot_duration(draft_shot["description"], dialogue, shot.kind != "dialogue"),
                "action_complexity": complexity, "transition_hint": hint, "new_scene": n == 0,
            })
        # Senaryodaki [NESNE: ...] işaretleri kod tarafından okunur: LLM'in hiçbir çekime koymadığı nesne, kural tabanlı
        # ayrıştırıcıdaki gibi sahnenin tüm çekimlerine bağlanır (eksik referans sessizce kaybolmasın).
        placed = {p for shot in shots for p in shot["props"]}
        leftover = [p for s in rules.parse(chunk)["shots"] for p in s["props"] if p not in placed]
        for shot in shots:
            shot["props"] += [p for p in dict.fromkeys(leftover) if p not in shot["props"]]
        scenes.append(shots)
    # Sekanslar (# SEKANS: ...) kodla, kural tabanlı ayrıştırıcının kuralıyla atanır: LLM sekans satırlarını görmez.
    keys = state.get("sequences") or [(0, None)] * len(scenes)
    kept = [(shots, key) for shots, key in zip(scenes, keys) if shots]
    codes, sequences = rules.number_sequences([key for _, key in kept])
    shots_out = []
    for scene_no, ((shots, _), code) in enumerate(zip(kept, codes), start=1):
        for shot_no, shot in enumerate(shots, start=1):
            shots_out.append({"scene_id": f"S{scene_no:03d}_SH{shot_no:02d}", "sequence": code,
                              **{k: v for k, v in shot.items() if k not in ("scene_id", "sequence")}})
    rules.classify(shots_out)                  # geçiş, gerekçe, zincirden gelen ve kareden çıkan karakterler
    return {"cast": state["cast"], "sequences": sequences, "shots": shots_out}


def parse_with_llm(text: str, project_id: str, llm=None) -> dict:
    """Senaryoyu LLM ile ayrıştırır. `llm` yalnızca testlerde verilir; yoksa guard'dan geçip ChatAnthropic kurulur."""
    key = guard("anthropic_api_key")
    cast = list(dict.fromkeys(b["name"] for b in rules.tokenize(text) if b["type"] == "speech"))
    pairs = rules.split_scenes_with_sequences(text)
    chunks = [chunk for chunk, _ in pairs]
    if not chunks:
        return {"cast": cast, "sequences": [], "shots": []}
    graph = build_graph(llm or make_llm(key))
    final = graph.invoke({"project_id": project_id, "cast": cast, "chunks": chunks,
                          "sequences": [key for _, key in pairs], "drafts": {}, "problems": {}, "passes": 0},
                         config={"max_concurrency": MAX_PARALLEL_CALLS})
    return final["result"]
