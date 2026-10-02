"""API girdi şemaları ve doğrulama kuralları."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Emotion = Literal["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]
Engine = Literal["fal_wan", "seedance", "higgsfield"]
Shot = Literal["extreme_wide", "wide", "pov", "medium", "over_shoulder", "close_up", "extreme_close_up"]
Movement = Literal["static", "dolly_in", "dolly_out", "pan", "tilt", "crane", "orbit", "tracking", "handheld"]
Angle = Literal["low", "eye", "high"]
RESOLUTIONS = ("480p", "580p", "720p", "1080p")


def _int(lo, hi):
    return lambda v: type(v) is int and lo <= v <= hi          # bool ve float kabul edilmez


def _num(lo, hi, open_low=False):
    return lambda v: type(v) in (int, float) and (lo < v if open_low else lo <= v) and v <= hi


# Ayar değerlerinin geçerli aralıkları. wave_size=0 üretimi sonsuz döngüye sokardı; refined_k, veritabanında
# refined_rank CHECK (1..3) ile sınırlıdır; loudnorm hedefi -70..-5 LUFS dışında FFmpeg'de hata verir.
SETTING_RULES = {
    "clip_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "excellent_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "min_pass_rate": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "probe_n": (_int(1, 200), "1 ile 200 arasında tam sayı"),
    "full_n": (_int(1, 1000), "1 ile 1000 arasında tam sayı"),
    "wave_size": (_int(1, 200), "1 ile 200 arasında tam sayı"),
    "refined_k": (_int(1, 3), "1 ile 3 arasında tam sayı (Rafine Havuz en çok 3 sıra tutar)"),
    "max_correction_rounds": (_int(0, 10), "0 ile 10 arasında tam sayı"),
    "probe_resolution": (lambda v: v in RESOLUTIONS, "480p, 580p, 720p ya da 1080p"),
    "full_resolution": (lambda v: v in RESOLUTIONS, "480p, 580p, 720p ya da 1080p"),
    "soft_budget_ratio": (_num(0, 1, open_low=True), "0 ile 1 arasında (0 hariç) bir sayı"),
    "max_spend_per_10min": (_num(0, 1e7, open_low=True), "sıfırdan büyük bir sayı"),
    "loudness_lufs": (_num(-70, -5), "-70 ile -5 arasında bir sayı"),
}


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    budget_limit_usd: float = Field(gt=0, le=1e7)
    settings: dict = {}

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Proje adı boş olamaz")
        return v

    @field_validator("settings")
    @classmethod
    def _settings(cls, v: dict) -> dict:
        for key, value in v.items():
            rule = SETTING_RULES.get(key)
            if rule is None:
                raise ValueError(f"Bilinmeyen ayar: {key}")
            if not rule[0](value):
                raise ValueError(f"{key}: {rule[1]} olmalı (gelen: {value!r})")
        return v


class CharacterIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    lora_url: str | None = None
    lora_trigger: str | None = None
    lora_scale: float = Field(1.0, ge=0, le=2)
    voice_id: str | None = None


class CameraIn(BaseModel):
    shot: Shot = "medium"
    movement: Movement = "static"
    angle: Angle = "eye"
    lens_mm: int = Field(35, ge=8, le=200)


class PromptSpecIn(BaseModel):
    model_config = ConfigDict(extra="allow")      # düzeltme turlarının eklediği anahtarlar korunur
    prompt: str = ""
    negative: str = ""


class SceneIn(BaseModel):
    scene_code: str = Field(pattern=r"^S\d{3}_SH\d{2}$", examples=["S001_SH01"])
    seq_order: int = Field(ge=0)
    script_text: str = ""
    description: str | None = None
    emotion: Emotion = "neutral"
    camera: CameraIn = CameraIn()
    action_complexity: float = Field(0.0, ge=0, le=1)
    duration_s: float = Field(5.0, ge=2, le=10)
    prompt_spec: PromptSpecIn = PromptSpecIn()
    engine: Engine | None = None
    character_ids: list[UUID] = []


class ScenePatch(BaseModel):
    description: str | None = None
    emotion: Emotion | None = None
    camera: CameraIn | None = None
    action_complexity: float | None = Field(None, ge=0, le=1)
    duration_s: float | None = Field(None, ge=2, le=10)
    prompt_spec: PromptSpecIn | None = None
    engine: Engine | None = None


class AudioIn(BaseModel):
    """Bir ses kanalının yeni sürümü. Müzik ve efektler türün tüm eski kanallarının yerini alır; diyalogda
    `replaces` ile tek bir replik değiştirilir, verilmezse yeni replik eklenir."""
    kind: Literal["dialogue", "music", "sfx", "ambience"]
    source: Literal["tts", "procedural", "upload"] | None = None
    text: str | None = None
    emotion: Emotion | None = None
    character_id: UUID | None = None
    file_key: str | None = None
    start_offset_s: float = Field(0.0, ge=0, le=120)
    gain_db: float = Field(0.0, ge=-60, le=20)
    params: dict = {}
    replaces: UUID | None = None

    @model_validator(mode="after")
    def _by_kind(self):
        default = {"dialogue": "tts", "music": "procedural"}.get(self.kind, "upload")
        self.source = self.source or default
        if self.source == "tts" and self.kind != "dialogue":
            raise ValueError("Konuşma sentezi yalnızca diyalog kanalı içindir")
        if self.source == "procedural" and self.kind != "music":
            raise ValueError("Prosedürel üretim yalnızca müzik kanalı içindir")
        if self.kind == "dialogue" and self.source == "tts" and not (self.text or "").strip():
            raise ValueError("Diyalog kanalı için metin gerekli")
        if self.source == "upload" and not self.file_key:
            raise ValueError("Yüklenen ses için file_key gerekli")
        return self


class KillSwitchIn(BaseModel):
    action: Literal["freeze", "resume"]
    reason: str = "manual"


class ScriptIn(BaseModel):
    text: str = Field(min_length=10, max_length=200_000)
    auto_render: bool = True
    auto_music: bool = True
