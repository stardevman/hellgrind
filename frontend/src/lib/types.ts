/* API yanıtlarının panelin kullandığı alanları. Kaynak: backend/app/api/*.py */

export type Emotion = "neutral" | "joy" | "anger" | "fear" | "sadness" | "surprise" | "disgust" | "tension";

export interface QualityMeta { mode: "dry" | "real" | "invalid"; ready: boolean; reason: string | null }

export interface Preset { probe_n?: number; full_n?: number; wave_size?: number; max_correction_rounds?: number }

export interface Meta {
  dry_run: boolean;
  hermes_mode: string;
  tts_engine: string;
  quality?: QualityMeta;
  presets: Record<string, Preset>;
  defaults: Record<string, number | string | boolean> & { probe_n: number; full_n: number };
  sample_script: string;
  trial_script?: string;
}

export interface ProjectListItem { id: string; title: string; status: string; spent_usd: number; budget_limit_usd: number }

export interface ProjectSettings {
  soft_budget_ratio: number;
  require_board_approval: boolean;
  asset_min_status: string;
  lint_strictness: string;
  [key: string]: unknown;
}

export interface ContinuityPair {
  from: string; to: string; to_id: string; jump: boolean;
  delta: { brightness: number; warmth: number; saturation: number };
  suggestion?: Record<string, number> | null;
}

export interface Project {
  id: string; title: string; status: string;
  budget_limit_usd: number; spent_usd: number; reserved_usd: number;
  frozen: boolean; soft_limited: boolean; settings: ProjectSettings; dry_run: boolean;
  style_version: number | null; lut: boolean;
  continuity: { pairs: ContinuityPair[] } | null;
  asset_counts: Record<string, number>;
}

export interface Measure { alignment: Record<string, number>; identity: Record<string, number>; faces_seen: number }

export interface Variation {
  id: string; tier: "probe" | "full"; wave_no: number; status: string; seed: number;
  score: number | null; rank: number | null; cost_usd: number; anomalies: string[];
  reason: string | null; stage: string | null; measure: Measure | null;
  thumb_url: string | null; video_url: string | null;
  source: string; revisions: number; revision: number | null;
}

export interface Track {
  id: string; kind: string; source: string; text: string | null; emotion: string | null;
  start_offset_s: number; gain_db: number; version: number;
  character_id: string | null; character: string | null; mood: string | null;
}

export interface SceneAssetRef { id: string | null; type: string; name: string; status: string | null }

export interface Grade { exposure: number; contrast: number; saturation: number; gamma: number; temperature: number }

export interface Scene {
  id: string; scene_code: string; status: string; engine: string | null; sequence_id: string | null;
  board_status: string; board_note: string | null; board_url: string | null;
  upgrade_requested: boolean; cut_tier: string | null; draft_url: string | null;
  method: string; method_hint: boolean; external_url: string | null; guide_url: string | null;
  grade: Grade; graded: boolean; lipsync_ready: boolean;
  heading: string; description: string | null; emotion: string;
  camera: { shot?: string; movement?: string; angle?: string; lens_mm?: number } | null;
  duration_s: number; action_complexity: number; spent_usd: number; last_error: string | null;
  correction_count: number; characters: string[]; final_url: string | null;
  assets: SceneAssetRef[]; missing: SceneAssetRef[]; tracks: Track[]; variations: Variation[];
  /** MiniMax H3 koşulu: fl2va (ilk kareden) | ref2va (karakter referans setiyle). */
  h3_mode: "fl2va" | "ref2va"; board_score: number | null; board_source: string | null;
  keyframe_source: "upload" | "board" | null; keyframe_url: string | null;
  h3_refs: { name: string; url: string | null }[];
}

export interface TimelineTrack {
  id: string; sequence_id: string | null; kind: string; source: string; mood: string | null;
  filename: string | null; duration_s: number | null; url: string | null;
  start_s: number; end_s: number | null; gain_db: number; fade_in_s: number; fade_out_s: number; is_active: boolean;
}

export interface Sequence {
  id: string; code: string; title: string; seq_order: number; status: string;
  budget_cap_usd: number | null; spent_usd: number; reserved_usd: number;
  shots: number; done: number; tracks: TimelineTrack[];
}

export interface CutClip { scene_code: string; in_s: number; out_s: number; description?: string; emotion?: string }
export interface CutSequence { code: string; title: string; in_s: number; out_s: number }
export interface Film { url: string; cut: { clips?: CutClip[]; sequences?: CutSequence[] } | null }

export interface PanelEvent { kind: string; ts: number; scene_id?: string; [key: string]: unknown }

export interface Overview {
  project: Project; scenes: Scene[]; sequences: Sequence[]; events: PanelEvent[];
  timeline: Film | null; draft_timeline: Film | null;
}

/* ---------- varlıklar ---------- */

export interface Reference {
  url: string | null; thumb_url: string | null; frames?: string[]; frame_urls?: string[];
  [key: string]: unknown;
}

export interface MotionTest {
  id: string; status: string; seed: number; cost_usd: number; anomalies: string[]; current: boolean;
  video_url: string | null; thumb_url: string | null;
}

export interface Asset {
  id: string; type: "character" | "location" | "prop"; name: string; description: string | null;
  render_style: string | null; status: string; version: number; effective_style: string;
  references: Record<string, Reference>; motion_test: MotionTest | null; scenes: string[];
  character: { id: string; voice_id: string | null; embedding_version: number } | null;
}

/* ---------- stil ---------- */

export interface StyleVersion {
  version: number; is_active: boolean; lighting_prefix: string | null; motion_style: string | null;
  rules: Record<string, string>; negative: string | null; palette: string[]; notes: string | null;
  created_at: number | null; prefix: string;
}

export interface StyleState {
  active: StyleVersion | null; versions: StyleVersion[]; defaults: Record<string, string>;
  render_styles: Record<string, string>; example: Partial<StyleVersion>;
}

export interface StyleImpact {
  version: number; affected: { scene_code: string; style_only: boolean }[]; pending: unknown[];
  expected_usd: number; worst_usd: number;
}

/* ---------- verimlilik ---------- */

export interface Metrics {
  variations: number; accepted_shots: number; variations_per_accepted_shot: number | null;
  cost_per_film_minute_usd: number | null; spent_usd: number; film_minutes: number | null; film_minutes_source: string;
  probe: { success_rate: number | null; succeeded: number; rounds: number; mean_correction_rounds: number | null; scenes: number };
  draft: { upgrade_ratio: number | null; upgraded: number; drafted: number };
  sequences: {
    code: string; title: string; variations: number; accepted_shots: number; variations_per_accepted_shot: number | null;
    spent_usd: number; variation_share: number | null; spend_share: number | null;
  }[];
  purge_reasons: Record<string, number>;
  spent_by_kind: Record<string, number>;
}

export interface CurationItem { id: number; at: string | null; action: string; action_tr: string; scene_code: string | null }

export interface Revision { id: string; version: number; kind: string; note: string | null; video_url: string }
export interface RevisionList { variation_id: string; active: string | null; rank: number | null; original_url: string | null; revisions: Revision[] }

/* ---------- eylem yanıtları ---------- */

export interface Blocked { missing: { name: string }[] }
export interface StageResult { stages: string[]; blocked?: Blocked | null; hint?: string }
export interface DispatchAll {
  planned: Record<string, string[]>; busy: string[]; blocked: Record<string, unknown>;
  capped: string[]; awaiting_board?: string[]; frozen: boolean;
}

/* ---------- ayarlar (ücretli servisler) ---------- */

export interface ServiceField {
  env: string; label: string; kind: "secret" | "model" | "url" | "text" | "choice";
  set?: boolean; masked?: string | null;                                  // secret
  value?: string; default?: string; choices?: string[]; url?: string | null;   // model, url, choice
}

export interface Service {
  id: "vast" | "fal" | "elevenlabs" | "anthropic"; name: string; key_env: string; purpose: string; console: string; used_by: string[];
  fields: ServiceField[]; active: boolean; key_set: boolean;
  /** Gerçek harcama; sanal (ücretsiz mod) ve modu bilinmeyen ayrı. */
  spent_usd: number; virtual_usd: number; unknown_usd: number;
  needed: boolean; not_needed_reason: string; missing_packages?: string[];
}

export interface AuditEntry { ts: number; field: string; action: string; detail: string }

export interface SettingsState {
  dry_run: boolean; editable: boolean; env_file: string | null; confirmation: string; busy: string[];
  services: Service[]; spent_usd: number; virtual_usd: number; unknown_usd: number; audit: AuditEntry[]; pricing_file: string;
  scorer: { ready: boolean; message: string };
  translator?: { ready: boolean; message: string };
  comfy?: ComfyState;
}

export interface ComfyWorkflow { kind: string; present: boolean; required: string[]; placeholders: string[]; error: string | null }
export interface ComfyState { backend: string; dir: string; workflows: ComfyWorkflow[] }

/** Kendi GPU sunucusunda çalışan motor (vast.ai'de MiniMax H3): saatlik ücret ve video saniyesi başına GPU saniyesi. */
export interface SelfHosted { gpu_hourly_usd: number; render_seconds: Record<string, number>; keyframe_seconds?: number }

export interface PricingTable {
  self_hosted?: Record<string, SelfHosted>;
  video_per_second: Record<string, Record<string, number>>;
  image_per_megapixel: Record<string, number>;
  image_per_image?: Record<string, number>;
  lipsync_per_minute: number; tts_per_1k_chars: number;
  llm_per_million_tokens: { input: number; output: number };
}

export interface ProbeResult { ok: boolean | null; status: number | null; message: string }

/* ---------- servis geçmişi ---------- */

export type CallMode = "real" | "virtual" | "unknown";
export type CallStatus = "running" | "succeeded" | "failed";

/** Sağlayıcıya giden tek istek: model, istek kimliği ve açıklayıcı parametreler (adresler ve anahtarlar yazılmaz). */
export interface CallStep { model: string; request_id?: string; prompt?: string; [param: string]: unknown }

export interface ServiceCall {
  id: number; reservation: string; started_at: string; finished_at: string | null; duration_s: number | null;
  service: string; service_name: string; console: string | null; operation: string; operation_title: string;
  label: string; model: string | null; model_inferred: boolean; request_id: string | null;
  mode: CallMode; mode_inferred: boolean; backfilled: boolean; status: CallStatus;
  estimate_usd: number; amount_usd: number | null; error: string | null; error_type: string | null;
  attempt: number | null; resolution: string | null; steps: CallStep[];
  project_id: string | null; project_title: string | null; subject: string | null; scene_id: string | null;
  variation_id: string | null; asset_id: string | null; asset_type: string | null; tier: string | null; wave_no: number | null;
}

export interface CallPage { items: ServiceCall[]; next: number | null }

export interface CallFigures {
  calls: number; failed: number; running: number; real_calls: number; virtual_calls: number;
  real_usd: number; virtual_usd: number; unknown_usd: number; reserved_usd: number;
}

export interface CallSummary {
  totals: CallFigures; first_at: string | null; last_at: string | null;
  by_service: (CallFigures & { service: string; name: string })[];
  by_model: (CallFigures & { service: string; model: string | null })[];
  by_operation: (CallFigures & { operation: string; title: string })[];
  by_project: (CallFigures & { project_id: string | null; title: string | null; deleted: boolean })[];
  by_day: (CallFigures & { day: string })[];
}
