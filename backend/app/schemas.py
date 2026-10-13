"""API girdi şemaları ve doğrulama kuralları."""
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Emotion = Literal["neutral", "joy", "anger", "fear", "sadness", "surprise", "disgust", "tension"]
Engine = Literal["h3max", "seedance25", "seedance", "h3", "fal_wan", "higgsfield"]
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
    "identity_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "look_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "look_dino_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "look_review_margin": (_num(0, 0.5), "0 ile 0,5 arasında bir sayı"),
    "continuity_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "cut_continuity_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "transition_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "arrival_review_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "state_threshold": (_num(0, 1), "0 ile 1 arasında bir sayı"),
    "chain_end_frame": (lambda v: type(v) is bool, "true ya da false"),
    "seam_mode": (lambda v: v in ("keyframe", "exact"), "keyframe ya da exact"),
    "seam_crossfade_frames": (_int(0, 6), "0 ile 6 arasında tam sayı"),
    "qwen_edit_mode": (lambda v: v in (None, "full", "medium", "lightning"), "full, medium, lightning ya da boş"),
    "audio": (lambda v: v in ("on", "off"), "on ya da off"),
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
    "lint_strictness": (lambda v: v in ("off", "warn", "block"), "off, warn ya da block"),
    "require_board_approval": (lambda v: type(v) is bool, "true ya da false"),
    "paid_audio": (lambda v: type(v) is bool, "true ya da false"),
    "asset_min_status": (lambda v: v in ("still_approved", "motion_tested", "locked"),
                         "still_approved, motion_tested ya da locked"),
    # fal.ai video motorları: boş = Ayarlar'daki VIDEO_DRAFT_ENGINE / VIDEO_FINAL_ENGINE
    "draft_engine": (lambda v: v in (None, "h3max", "seedance25", "seedance", "h3"),
                     "h3max, seedance25, seedance, h3 ya da boş"),
    "final_engine": (lambda v: v in (None, "seedance25", "h3max", "seedance"), "seedance25, h3max, seedance ya da boş"),
    "fal_clip_attempts": (_int(1, 5), "1 ile 5 arasında tam sayı"),
    "require_plan_approval": (lambda v: v is None or type(v) is bool, "true, false ya da boş (ücretli modda açık)"),
    "h3max_resolution": (lambda v: v in (None, "480P", "768P", "1080P"), "480P, 768P, 1080P ya da boş"),
    "seedance25_draft": (lambda v: v is None or type(v) is bool, "true, false ya da boş"),
    "seedance25_resolution": (lambda v: v in (None, "480p", "720p", "1080p"), "480p, 720p, 1080p ya da boş"),
    "on_twos": (lambda v: type(v) is bool, "true ya da false"),
    "voice_first": (lambda v: type(v) is bool, "true ya da false"),
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
    side: int = Field(0, ge=-90, le=90)            # yan açı (derece; + sağ, - sol): kesmede 30° kuralı


class PromptSpecIn(BaseModel):
    model_config = ConfigDict(extra="allow")      # düzeltme turlarının eklediği anahtarlar korunur
    prompt: str = ""
    negative: str = ""

    @model_validator(mode="after")
    def _no_style(self):
        """Stil tek yerden yönetilir (değişmez kural 6): ışık ve hareket dili çekim bazında yazılmaz."""
        styled = sorted(set(self.model_extra or {}) & {"style", "lighting", "lighting_prefix", "motion_style", "palette"})
        if styled:
            raise ValueError(f"Stil çekim bazında yazılmaz ({', '.join(styled)}); stil kılavuzunu kullan.")
        return self


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
    sequence_id: UUID | None = None          # verilmezse projenin son sekansı (yoksa SEQ01 açılır)


class ScenePatch(BaseModel):
    description: str | None = None
    emotion: Emotion | None = None
    camera: CameraIn | None = None
    action_complexity: float | None = Field(None, ge=0, le=1)
    duration_s: float | None = Field(None, ge=2, le=15)          # H3 Max en çok 15 sn üretir
    prompt_spec: PromptSpecIn | None = None
    engine: Engine | None = None
    # fal video (H3 Max / Seedance 2.5): çözünürlük ve model sesi; auto: Ayarlar'daki varsayılan
    video_resolution: Literal["auto", "480P", "768P", "1080P", "480p", "720p", "1080p"] | None = None
    video_audio: Literal["auto", "true", "false"] | None = None
    video_audio_ref: Literal["on", "off"] | None = None     # ses-önce: diyalog karışımı video isteğine ses referansı
    h3_mode: Literal["fl2va", "ref2va", "auto"] | None = None          # auto: otomatik seçim (core/h3mode.py)
    face_expected: Literal["auto", "true", "false"] | None = None
    transition_in: Literal["auto", "continuous", "cut", "new_scene"] | None = None   # çekime geçiş (auto: senaryodan)
    entry_mode: Literal["cut", "walk_in"] | None = None     # yeni karakter girişi: kesme (kamera değişir) | devam + giriş
    cut_method: Literal["auto", "cut_in", "angles", "wide", "compose"] | None = None   # kesme karesi yöntemi (auto: otomatik)
    # Yönetmen istemi (referans modu): parçanın istemi olduğu gibi gönderilir, yalnızca referans yazımı motora göre
    # çevrilir ([ImageN] / @ImageN / Image N). Boş metin siler.
    prompt_override: str | None = Field(None, max_length=20000)


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


class IdleRefundIn(BaseModel):
    """[start, end] aralığındaki boşta bekleme kayıtlarının iadesi (RunPod'un faturalamadığı hayalet boşta süre)."""
    start: datetime
    end: datetime
    reason: str = Field(min_length=3, max_length=500)


class ScriptIn(BaseModel):
    text: str = Field(min_length=10, max_length=200_000)
    auto_render: bool = True
    auto_music: bool = True


# ---------- varlıklar (Faz 12) ----------

AssetType = Literal["character", "location", "prop"]


class AssetIn(BaseModel):
    type: AssetType
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    render_style: str | None = Field(None, max_length=200)     # clean_3d | painterly | stylized_2d | serbest metin


class AssetPatch(BaseModel):
    description: str | None = Field(None, max_length=2000)
    render_style: str | None = Field(None, max_length=200)
    voice_id: str | None = Field(None, max_length=200)         # yalnızca karakter: ses kimliği karakterde tutulur
    character_kind: Literal["human", "non_human"] | None = None  # yalnızca karakter: kimlik kontrolünün türü
    invariants: list[str] | None = Field(None, max_length=12)  # tasarım değişmezleri (her kare ve video istemine eklenir)
    scale: str | None = Field(None, max_length=200)          # gerçek boyut (ör. "tiny, palm-sized; much smaller than a hen")

    @field_validator("invariants")
    @classmethod
    def _invariants(cls, v):
        if v is None:
            return v
        out = [" ".join(x.split()) for x in v if x and x.strip()]
        if any(len(x) > 200 for x in out):
            raise ValueError("Her değişmez en çok 200 karakter olmalı")
        return out


class SceneAssetIn(BaseModel):
    """Sahneye mekân ya da nesne bağlar: mevcut bir varlık (`asset_id`) ya da adıyla (yoksa taslak olarak oluşturulur)."""
    asset_id: UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=100)
    type: Literal["location", "prop"] = "prop"

    @model_validator(mode="after")
    def _one(self):
        if not self.asset_id and not (self.name or "").strip():
            raise ValueError("asset_id ya da name gerekli")
        return self


# ---------- stil kılavuzu (Faz 13) ----------

HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class StyleIn(BaseModel):
    """Stil kılavuzunun yeni sürümü. Etkinleştirilene dek hiçbir çekimi etkilemez."""
    lighting_prefix: str = Field("", max_length=2000)
    motion_style: str = Field("", max_length=2000)
    rules: dict[str, str] = {}                       # varlık türü -> render stili (clean_3d | painterly | stylized_2d | serbest)
    negative: str = Field("", max_length=2000)
    palette: list[str] = Field([], max_length=12)
    notes: str | None = Field(None, max_length=4000)
    medium: Literal["2d", "3d"] = "2d"               # çizim türü: video istemindeki ortam cümlesi ve varsayılan render kuralları

    @field_validator("rules")
    @classmethod
    def _rules(cls, v: dict) -> dict:
        unknown = set(v) - set(AssetType.__args__)
        if unknown:
            raise ValueError(f"Bilinmeyen varlık türü: {', '.join(sorted(unknown))} (geçerli: character, location, prop)")
        clean = {k: " ".join(style.split()) for k, style in v.items()}
        if any(not style or len(style) > 200 for style in clean.values()):
            raise ValueError("Render stili boş olamaz ve en çok 200 karakter olabilir")
        return clean

    @field_validator("palette")
    @classmethod
    def _palette(cls, v: list) -> list:
        bad = [c for c in v if not HEX_COLOR.match(c)]
        if bad:
            raise ValueError(f"Palet renkleri #RRGGBB biçiminde olmalı: {', '.join(bad)}")
        return [c.upper() for c in v]


class StyleVersionIn(BaseModel):
    version: int = Field(ge=1)


class StyleActivateIn(StyleVersionIn):
    render: bool = False                             # etkilenen çekimleri hemen yeniden üret


class VoiceLockIn(BaseModel):
    locked: bool                                     # true: sekansın sesi kilitlenir; false: kilit açılır


class OnTwosIn(BaseModel):
    enabled: bool                                    # kare tutma (on twos): renk aşamasında her çizim iki kare


# ---------- sekanslar ve film düzeyinde ses (Faz 11) ----------

class SequencePatch(BaseModel):
    """Sekans başlığı ve bütçe tavanı. Tavan null: kaldırılır (yalnızca proje limiti geçerli)."""
    title: str | None = Field(None, min_length=1, max_length=200)
    budget_cap_usd: float | None = Field(None, gt=0, le=1e7)


class TrackIn(BaseModel):
    """Prosedürel sekans (ya da film) müziği. replace: kapsamdaki önceki prosedürel müzik kanallarının yerini alır;
    yüklenen stem'lere dokunulmaz."""
    kind: Literal["music"] = "music"
    mood: Emotion
    gain_db: float = Field(-8.0, ge=-60, le=20)
    start_s: float = Field(0.0, ge=0, le=36000)
    end_s: float | None = Field(None, gt=0, le=36000)
    fade_in_s: float = Field(1.0, ge=0, le=60)
    fade_out_s: float = Field(1.5, ge=0, le=60)
    replace: bool = True

    @model_validator(mode="after")
    def _span(self):
        if self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError("end_s, start_s'den büyük olmalı")
        return self


class TrackPatch(BaseModel):
    start_s: float | None = Field(None, ge=0, le=36000)
    end_s: float | None = Field(None, gt=0, le=36000)
    gain_db: float | None = Field(None, ge=-60, le=20)
    fade_in_s: float | None = Field(None, ge=0, le=60)
    fade_out_s: float | None = Field(None, ge=0, le=60)


# ---------- storyboard ve taslak geçiş (Faz 14) ----------

class BoardIn(BaseModel):
    status: Literal["approved", "changes_requested", "pending"]
    note: str | None = Field(None, max_length=2000)


class UpgradeIn(BaseModel):
    requested: bool = True


class ProjectPatch(BaseModel):
    """Proje adı, bütçesi ve ayarları; verilen ayarlar mevcutlarla birleştirilir (ProjectIn ile aynı doğrulama)."""
    title: str | None = Field(None, min_length=1, max_length=200)
    budget_limit_usd: float | None = Field(None, gt=0, le=1e7)
    settings: dict = {}

    _settings = field_validator("settings")(ProjectIn._settings.__func__)


class EstimateIn(BaseModel):
    """Tahmini maliyet: senaryodan (text; proje oluşturulmadan) ya da projenin çekimlerinden (project_id). Ön ayar ve ayarlar
    projenin ayarlarının üstüne yazılır (yalnızca hesap için; kaydedilmez)."""
    text: str | None = Field(None, min_length=10, max_length=200_000)
    project_id: UUID | None = None
    preset: str | None = None
    settings: dict = {}

    _settings = field_validator("settings")(ProjectIn._settings.__func__)


# ---------- üretim yöntemi ve elle düzeltme (Faz 15) ----------

class MethodIn(BaseModel):
    method: Literal["generate", "previs_guide", "external"]


class RevisionActiveIn(BaseModel):
    revision_id: UUID | None = None          # null: orijinal üretime dön


# ---------- renk (Faz 16) ----------

class GradeIn(BaseModel):
    """Çekim renk ayarı. Etkisiz değerler: exposure 0, contrast/saturation/gamma 1, temperature 6500."""
    exposure: float = Field(0.0, ge=-3, le=3)            # EV
    contrast: float = Field(1.0, ge=0.5, le=2)
    saturation: float = Field(1.0, ge=0, le=3)
    gamma: float = Field(1.0, ge=0.3, le=3)
    temperature: int = Field(6500, ge=2000, le=12000)    # Kelvin; düşük değer ısıtır
