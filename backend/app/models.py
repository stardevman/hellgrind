"""Veritabanı tabloları db/init/001_core.sql ile oluşturulur; burada yalnızca eşlenir."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Numeric, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def pg_enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, create_type=False)


ProjectStatus = pg_enum("project_status", "draft", "parsing", "producing", "paused", "killed", "completed")
SceneStatus = pg_enum("scene_status", "pending", "parsed", "probing", "generating", "correcting", "refined",
                      "needs_review", "audio", "lipsync", "muxing", "done", "failed", "frozen")
EngineKind = pg_enum("engine_kind", "fal_wan", "seedance", "higgsfield")
VariationTier = pg_enum("variation_tier", "probe", "full")
VariationStatus = pg_enum("variation_status", "queued", "running", "generated", "passed", "rejected",
                          "purged", "refined", "failed")
AudioKind = pg_enum("audio_kind", "dialogue", "music", "sfx", "ambience")
EmotionTag = pg_enum("emotion_tag", "neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension")
LedgerKind = pg_enum("ledger_kind", "reserve", "commit", "release")

PK = lambda: mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # noqa: E731


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = PK()
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(ProjectStatus, default="draft")
    budget_limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Character(Base):
    __tablename__ = "characters"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    lora_url: Mapped[str | None] = mapped_column(Text)
    lora_trigger: Mapped[str | None] = mapped_column(Text)
    lora_scale: Mapped[float] = mapped_column(Float, default=1.0)
    reference_images: Mapped[list] = mapped_column(JSONB, default=list)
    voice_id: Mapped[str | None] = mapped_column(Text)
    pinecone_namespace: Mapped[str] = mapped_column(Text)
    embedding_version: Mapped[int] = mapped_column(Integer, default=1)
    style_constraints: Mapped[dict] = mapped_column(JSONB, default=dict)


class Scene(Base):
    __tablename__ = "scenes"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    scene_code: Mapped[str] = mapped_column(Text)
    seq_order: Mapped[int] = mapped_column(Integer)
    script_text: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(EmotionTag, default="neutral")
    camera: Mapped[dict] = mapped_column(JSONB, default=dict)
    action_complexity: Mapped[float] = mapped_column(Float, default=0.0)
    engine: Mapped[str | None] = mapped_column(EngineKind)
    duration_s: Mapped[float] = mapped_column(Float, default=5.0)
    prompt_spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    prompt_revision: Mapped[int] = mapped_column(Integer, default=0)
    correction_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(SceneStatus, default="pending")
    visual_hash: Mapped[str | None] = mapped_column(Text)
    dialogue_hash: Mapped[str | None] = mapped_column(Text)
    bed_hash: Mapped[str | None] = mapped_column(Text)
    mux_hash: Mapped[str | None] = mapped_column(Text)
    lipsync_video_key: Mapped[str | None] = mapped_column(Text)
    final_video_key: Mapped[str | None] = mapped_column(Text)
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SceneCharacter(Base):
    __tablename__ = "scene_characters"
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"), primary_key=True)
    character_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, default="primary")
    screen_position: Mapped[str | None] = mapped_column(Text)
    dialogue: Mapped[list] = mapped_column(JSONB, default=list)


class Variation(Base):
    __tablename__ = "variations"
    id: Mapped[uuid.UUID] = PK()
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    tier: Mapped[str] = mapped_column(VariationTier)
    wave_no: Mapped[int] = mapped_column(Integer, default=0)
    engine: Mapped[str] = mapped_column(EngineKind)
    seed: Mapped[int] = mapped_column(BigInteger)
    prompt_revision: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    external_request_id: Mapped[str | None] = mapped_column(Text)
    keyframe_key: Mapped[str | None] = mapped_column(Text)
    video_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(VariationStatus, default="queued")
    clip_score: Mapped[float | None] = mapped_column(Float)
    face_score: Mapped[float | None] = mapped_column(Float)
    composite_score: Mapped[float | None] = mapped_column(Float)
    anomalies: Mapped[list] = mapped_column(JSONB, default=list)
    refined_rank: Mapped[int | None] = mapped_column(SmallInteger)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AudioTrack(Base):
    __tablename__ = "audio_tracks"
    id: Mapped[uuid.UUID] = PK()
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    character_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("characters.id"))
    kind: Mapped[str] = mapped_column(AudioKind)
    source: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(EmotionTag)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(Text)
    file_key: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    start_offset_s: Mapped[float] = mapped_column(Float, default=0.0)
    gain_db: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class CostLedger(Base):
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    scene_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    variation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("variations.id"))
    provider: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(LedgerKind)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    reservation: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
