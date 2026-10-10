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
                      "needs_review", "audio", "lipsync", "muxing", "done", "failed", "frozen", "blocked",
                      "storyboard", "board_review", "grading")
EngineKind = pg_enum("engine_kind", "fal_wan", "seedance", "higgsfield", "h3")
VariationTier = pg_enum("variation_tier", "probe", "full")
VariationStatus = pg_enum("variation_status", "queued", "running", "generated", "passed", "rejected",
                          "purged", "refined", "failed")
AudioKind = pg_enum("audio_kind", "dialogue", "music", "sfx", "ambience")
EmotionTag = pg_enum("emotion_tag", "neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension")
LedgerKind = pg_enum("ledger_kind", "reserve", "commit", "release")
AssetType = pg_enum("asset_type", "character", "location", "prop")
AssetStatus = pg_enum("asset_status", "draft", "still_approved", "motion_tested", "locked")

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
    timeline_key: Mapped[str | None] = mapped_column(Text)
    draft_timeline_key: Mapped[str | None] = mapped_column(Text)
    lut_key: Mapped[str | None] = mapped_column(Text)
    lut_hash: Mapped[str | None] = mapped_column(Text)
    continuity: Mapped[dict | None] = mapped_column(JSONB)
    script_source: Mapped[str | None] = mapped_column(Text)
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
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id"))
    kind: Mapped[str] = mapped_column(Text, default="human")   # human | non_human (robot, hayvan, yaratık)


class Sequence(Base):
    """Filmin bir bölümü (Havalimanı, Taksi...). Müzik ve isteğe bağlı bütçe tavanı sekans düzeyindedir."""
    __tablename__ = "sequences"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    seq_order: Mapped[int] = mapped_column(Integer)
    budget_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0)
    status: Mapped[str] = mapped_column(Text, default="active")          # active | capped
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TimelineTrack(Base):
    """Film ya da sekans düzeyinde fon kanalı (müzik, efekt, ambiyans). Zamanlar sekansın başına göredir."""
    __tablename__ = "timeline_tracks"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sequences.id"))
    kind: Mapped[str] = mapped_column(AudioKind)
    source: Mapped[str] = mapped_column(Text)                             # procedural | upload
    file_key: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float | None] = mapped_column(Float)
    gain_db: Mapped[float] = mapped_column(Float, default=0.0)
    fade_in_s: Mapped[float] = mapped_column(Float, default=0.0)
    fade_out_s: Mapped[float] = mapped_column(Float, default=0.0)
    content_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Scene(Base):
    __tablename__ = "scenes"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sequences.id"))
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
    board_status: Mapped[str] = mapped_column(Text, default="pending")     # pending | approved | changes_requested
    board_note: Mapped[str | None] = mapped_column(Text)
    board_key: Mapped[str | None] = mapped_column(Text)
    board_hash: Mapped[str | None] = mapped_column(Text)
    board_score: Mapped[float | None] = mapped_column(Float)                # storyboard karesinin kalite skoru (ücretli mod)
    board_source: Mapped[str | None] = mapped_column(Text)                  # local | flux | comfyui | upload
    h3_mode: Mapped[str | None] = mapped_column(Text)                       # None: otomatik (core/h3mode.py) | fl2va | ref2va
    face_expected: Mapped[bool | None] = mapped_column(Boolean)            # None: otomatik; kimlik kapısı
    transition_auto: Mapped[str | None] = mapped_column(Text)              # senaryodan: continuous | cut | new_scene
    transition_in: Mapped[str | None] = mapped_column(Text)                # yönetmenin seçimi; None: otomatik
    board_sha: Mapped[str | None] = mapped_column(Text)                    # ilk karenin (yazısız) SHA-256'sı
    board_parent: Mapped[str | None] = mapped_column(Text)                 # zincirle çizildiyse önceki karenin board_sha'sı
    transition_hint: Mapped[str | None] = mapped_column(Text)              # senaryodan zorlanan geçiş: cut | continuous
    transition_reason: Mapped[str | None] = mapped_column(Text)            # otomatik sınıflamanın gerekçesi
    director_note: Mapped[str | None] = mapped_column(Text)                # "Değişiklik iste" notu: istemlere girer
    camera_plan: Mapped[dict | None] = mapped_column(JSONB)                # kesmede otomatik kamera (30° kuralı); boşsa camera
    entry_mode: Mapped[str | None] = mapped_column(Text)                   # karakter girişi: cut (varsayılan) | walk_in
    grade_auto: Mapped[dict | None] = mapped_column(JSONB)                 # otomatik renk eşleştirmesi (geri alınabilir)
    end_board_key: Mapped[str | None] = mapped_column(Text)                # çekim sonu karesi (Kn_end, yazısız)
    end_board_sha: Mapped[str | None] = mapped_column(Text)
    end_board_parent: Mapped[str | None] = mapped_column(Text)             # çizildiği Kn'nin board_sha'sı
    end_board_score: Mapped[float | None] = mapped_column(Float)
    end_board_note: Mapped[str | None] = mapped_column(Text)
    keyframe_key: Mapped[str | None] = mapped_column(Text)                  # yönetmenin yüklediği ilk kare
    keyframe_hash: Mapped[str | None] = mapped_column(Text)
    upgrade_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cut_tier: Mapped[str | None] = mapped_column(Text)                     # draft | final
    draft_video_key: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text, default="generate")          # generate | previs_guide | external
    external_key: Mapped[str | None] = mapped_column(Text)
    external_hash: Mapped[str | None] = mapped_column(Text)
    guide_key: Mapped[str | None] = mapped_column(Text)
    guide_hash: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[dict] = mapped_column(JSONB, default=dict)
    grade_hash: Mapped[str | None] = mapped_column(Text)
    graded_video_key: Mapped[str | None] = mapped_column(Text)
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


class Asset(Base):
    """Karakter, mekân ya da nesne. Durum akışı: draft -> still_approved -> motion_tested -> locked."""
    __tablename__ = "assets"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    type: Mapped[str] = mapped_column(AssetType)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    render_style: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(AssetStatus, default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    references: Mapped[dict] = mapped_column("references", JSONB, default=dict)
    reference_history: Mapped[list] = mapped_column(JSONB, default=list)   # yerini yenisine bırakan referanslar
    invariants: Mapped[list] = mapped_column(JSONB, default=list)          # tasarım değişmezleri (her isteme eklenir)
    motion_test_variation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SceneAsset(Base):
    __tablename__ = "scene_assets"
    scene_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="manual")     # script | manual


class StyleBible(Base):
    """Projenin sürümlü stil kılavuzu. Sürümler değişmez: her düzenleme yeni bir sürümdür; proje başına tek sürüm etkindir."""
    __tablename__ = "style_bibles"
    id: Mapped[uuid.UUID] = PK()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    version: Mapped[int] = mapped_column(Integer)
    lighting_prefix: Mapped[str] = mapped_column(Text, default="")
    rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    negative: Mapped[str] = mapped_column(Text, default="")
    motion_style: Mapped[str] = mapped_column(Text, default="")
    palette: Mapped[list] = mapped_column(JSONB, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Variation(Base):
    __tablename__ = "variations"
    id: Mapped[uuid.UUID] = PK()
    scene_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id"))   # hareket testi
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
    revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))   # etkin elle düzeltme; boşsa orijinal
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Revision(Base):
    """Varyasyonun elle düzeltilmiş sürümü (video ya da ilk kare). Orijinal varyasyon dokunulmadan kalır."""
    __tablename__ = "revisions"
    id: Mapped[uuid.UUID] = PK()
    variation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("variations.id"))
    scene_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)                                 # video | first_frame
    source_key: Mapped[str] = mapped_column(Text)
    video_key: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    anomalies: Mapped[list] = mapped_column(JSONB, default=list)
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
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sequences.id"))
    provider: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(LedgerKind)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    reservation: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CurationEvent(Base):
    """Yönetmenin bir kararı (Faz 17): seçim, eleme, iyileştirme işareti, onay... Kalite modeli eğitiminin verisi."""
    __tablename__ = "curation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"))
    scene_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scenes.id"))
    variation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("variations.id"))
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class H3Output(Base):
    """MiniMax H3 makinesinde üretilen bir çıktının yerel depoya indirilip doğrulanma kaydı (çıktı güvencesi)."""
    __tablename__ = "h3_outputs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[str | None] = mapped_column(Text)
    server: Mapped[str] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    task: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, default="submitted")
    remote: Mapped[dict] = mapped_column(JSONB, default=dict)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    meta_key: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
