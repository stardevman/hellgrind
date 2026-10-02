from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

Emotion = Literal["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]


def _int(lo, hi):
    return lambda v: type(v) is int and lo <= v <= hi


def _num(lo, hi, open_low=False):
    return lambda v: type(v) in (int, float) and (lo < v if open_low else lo <= v) and v <= hi


# Ayar değerlerinin geçerli aralıkları. wave_size=0 worker'ı sonsuz döngüye sokar; refined_k veritabanında
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
    "probe_resolution": (lambda v: v in ("480p", "580p", "720p", "1080p"), "480p, 580p, 720p ya da 1080p"),
    "full_resolution": (lambda v: v in ("480p", "580p", "720p", "1080p"), "480p, 580p, 720p ya da 1080p"),
    "soft_budget_ratio": (_num(0, 1, open_low=True), "0 ile 1 arasında (0 hariç) bir sayı"),
    "max_spend_per_10min": (_num(0, 1e6, open_low=True), "sıfırdan büyük bir sayı"),
    "loudness_lufs": (_num(-70, -5), "-70 ile -5 arasında bir sayı"),
}


class ProjectIn(BaseModel):
    title: str
    budget_limit_usd: float = Field(gt=0)
    settings: dict = {}

    @field_validator("settings")
    @classmethod
    def _valid_settings(cls, v: dict) -> dict:
        for key, value in v.items():
            rule = SETTING_RULES.get(key)
            if rule and not rule[0](value):
                raise ValueError(f"{key}: {rule[1]} olmalı (gelen: {value!r})")
        return v


class CharacterIn(BaseModel):
    name: str
    description: str | None = None
    lora_url: str | None = None
    lora_trigger: str | None = None
    lora_scale: float = 1.0
    voice_id: str | None = None


class SceneIn(BaseModel):
    scene_code: str = Field(pattern=r"^S\d{3}_SH\d{2}$", examples=["S001_SH01"])
    seq_order: int
    script_text: str = ""
    description: str | None = None
    emotion: Emotion = "neutral"
    camera: dict = {"shot": "medium", "movement": "static", "angle": "eye", "lens_mm": 35}
    action_complexity: float = Field(0.0, ge=0, le=1)
    duration_s: float = Field(5.0, ge=2, le=10)
    prompt_spec: dict = {"prompt": "", "negative": ""}
    character_ids: list[UUID] = []


class ScenePatch(BaseModel):
    description: str | None = None
    emotion: Emotion | None = None
    camera: dict | None = None
    action_complexity: float | None = Field(None, ge=0, le=1)
    duration_s: float | None = Field(None, ge=2, le=10)
    prompt_spec: dict | None = None


class AudioTrackIn(BaseModel):
    kind: Literal["dialogue", "music", "sfx", "ambience"]
    source: str = "dry_run"            # dry_run | sample | elevenlabs | upload
    text: str | None = None
    emotion: Emotion | None = None
    character_id: UUID | None = None
    file_key: str | None = None
    start_offset_s: float = 0.0
    gain_db: float = 0.0
    params: dict = {}


class KillSwitchIn(BaseModel):
    action: Literal["freeze", "resume"]
    reason: str = "manual"


class ScriptIn(BaseModel):
    text: str = Field(min_length=10)
    auto_render: bool = True
    auto_music: bool = True
