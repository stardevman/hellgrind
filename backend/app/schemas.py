from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Emotion = Literal["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]


class ProjectIn(BaseModel):
    title: str
    budget_limit_usd: float = Field(gt=0)
    settings: dict = {}


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
