/* API yanıtlarının panelin kullandığı alanları. Kaynak: backend/app/api/*.py */

export type Emotion = "neutral" | "joy" | "anger" | "fear" | "sadness" | "surprise" | "disgust" | "tension";

export interface QualityMeta { mode: "dry" | "real" | "invalid"; ready: boolean; reason: string | null }

export interface Preset {
  probe_n?: number; full_n?: number; wave_size?: number; max_correction_rounds?: number; refined_k?: number;
  probe_resolution?: string; full_resolution?: string;
}

export interface Meta {
  dry_run: boolean;
  /** MiniMax H3'ün çalıştığı platform: runpod (varsayılan) ya da vast (deneysel). */
  h3_platform?: "runpod" | "vast";
  hermes_mode: string;
  tts_engine: string;
  quality?: QualityMeta;
  presets: Record<string, Preset>;
  defaults: Record<string, number | string | boolean> & { probe_n: number; full_n: number };
  sample_script: string;
  trial_script?: string;
}

export interface ProjectListItem {
  id: string; title: string; status: string; spent_usd: number; budget_limit_usd: number;
  /** Panelde görünen durum: "producing" yalnızca işlenen bir çekim varken; bitince completed ya da idle. */
  display_status?: string;
}

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
  id: string; title: string; status: string; display_status?: string;
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
  reason: string | null; stage: string | null; measure: Measure | null; stale?: string | null; review?: string[] | null;
  /** FL2VA last_frame: klibin son karesi hedefe (next: sonraki çekimin karesi, end: çekimin son karesi) ne kadar vardı. */
  arrival?: { score: number; dino: number; identity: number | null; target?: "next" | "end" } | null;
  last_frame_kind?: "next" | "end" | null;
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
  camera: { shot?: string; movement?: string; angle?: string; lens_mm?: number; side?: number } | null;
  duration_s: number; action_complexity: number; spent_usd: number; last_error: string | null;
  correction_count: number; characters: string[]; final_url: string | null;
  assets: SceneAssetRef[]; missing: SceneAssetRef[]; tracks: Track[]; variations: Variation[];
  /** MiniMax H3 koşulu: fl2va (ilk kareden) | ref2va (karakter referans setiyle). */
  /** Geçerli H3 koşulu (yönetmenin seçimi ya da otomatik); h3_mode_explicit: seçim (null = otomatik). */
  h3_mode: "fl2va" | "ref2va"; h3_mode_explicit?: "fl2va" | "ref2va" | null; h3_mode_warning?: string | null;
  /** Kimlik kapısı: yüz görünmeli mi (geçerli) ve yönetmenin seçimi (null = otomatik). */
  face_expected?: boolean; face_expected_explicit?: boolean | null;
  board_score: number | null; board_source: string | null;
  keyframe_source: "upload" | "board" | null; keyframe_url: string | null;
  h3_refs: { name: string; url: string | null }[];
  keyframe_model?: "qwen_edit" | "qwen";
  transition?: "continuous" | "cut" | "new_scene"; transition_explicit?: string | null;
  chain_parent?: string | null; chain_stale?: boolean; chain_redraw?: string[];
  /** Otomatik sınıflamanın gerekçesi ("kadro aynı: Mila+Bolt"), senaryonun zorladığı geçiş (cut | continuous). */
  transition_reason?: string | null; transition_hint?: string | null;
  /** Otomatik renk eşleştirmesi (referans çekime göre çözülen renk ayarı; geri alınabilir). */
  grade_auto?: { applied?: boolean; disabled?: boolean; reference?: string; delta_e_before?: number; delta_e_after?: number } | null;
  /** Kesme kamerası (30° kuralı) ve karakter girişi seçeneği. */
  camera_plan?: { shot?: string; movement?: string; angle?: string; lens_mm?: number; side?: number } | null;
  camera_effective?: { shot?: string; movement?: string; angle?: string; lens_mm?: number; side?: number } | null;
  entry_mode?: string | null; entry_options?: string[] | null; entering?: string[] | null;
  /** Kazanan klibin varış skoru (son kare -> sonraki çekimin karesi ya da çekimin son karesi). */
  arrival?: { score: number; dino: number; identity: number | null; target?: "next" | "end" } | null;
  /** Zincirden gelen karakterler: önceki çekimin karesinde kalanlar (referans, değişmez ve kimlik kapısıyla). */
  chain_cast?: string[];
  /** Çekim sonu karesi (Kn_end): sonraki çekim kesmeyle geliyorsa video bu karede biter. */
  end_board_url?: string | null; end_board_note?: string | null; end_board_score?: number | null; end_board_current?: boolean;
  edit_refs?: { name: string; kind: "character" | "location"; url: string | null }[];
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
  character: { id: string; voice_id: string | null; embedding_version: number; kind: "human" | "non_human"; kind_suggested: "human" | "non_human" } | null;
  reference_history?: { kind: string; version: number; replaced_at: number; entry: Reference }[];
  invariants?: string[];
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
  cost_per_film_minute_usd: number | null; spend_mode?: string; spent_usd: number; film_minutes: number | null; film_minutes_source: string;
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

/** "Kareleri hazırla"nın planı (GET /projects/{id}/storyboard/plan): onaydan önce gösterilir. */
export interface StoryboardPlan {
  requested: string[]; followers: string[]; stale: string[]; ends: string[]; frames: string[]; clips: string[];
  estimate_usd: number;
}

/** Storyboard önce: şeridin tek tıkla onayı (POST /projects/{id}/boards/approve). */
export interface StripApproval extends Partial<DispatchAll> {
  approved: string[]; review: string[]; missing: string[]; frozen: boolean;
}

/* ---------- ayarlar (ücretli servisler) ---------- */

export interface ServiceField {
  env: string; label: string; kind: "secret" | "model" | "url" | "text" | "choice" | "number";
  set?: boolean; masked?: string | null;                                  // secret
  value?: string; default?: string; choices?: string[]; url?: string | null;   // model, url, choice
  minimum?: number | null;                                                // number: izin verilen en küçük değer
}

export interface Service {
  id: "runpod" | "vast" | "fal" | "elevenlabs" | "anthropic"; name: string; key_env: string; purpose: string; console: string; used_by: string[];
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

export interface ReplacementOffer {
  offer_id: number; gpu: string; dph: number; geolocation: string | null; ram_gb: number; disk_gb: number;
  reliability: number | null; inet_mbps: number | null; reason: string; old_instance_id: string | null; at: number;
  /** initial: "Yeni makine kirala" (hiçbir makine silinmez); replacement: açılamayan makinenin yerine. */
  kind?: "initial" | "replacement";
}
export interface ReplacementJob {
  state: string; offer?: ReplacementOffer; kind?: "initial" | "replacement"; old_instance_id?: string | null;
  new_instance_id?: string | null; url?: string | null; log_url?: string | null; message?: string | null;
  started_at?: number; updated_at?: number;
  /** Aşamaya giriş zamanları (sn, epoch): kurulum süresini ölçmek için. */
  steps?: Record<string, number>;
  /** Eski makine silinmedi: ürettiği çıktıların bir kısmı yerel depoda yok. */
  retained?: { instance_id: string; missing: number } | null;
}
/** Çıktı güvencesi: makine başına üretilen çıktıların yerel depodaki durumu. */
export interface OutputMachine {
  instance_id: string | null; current: boolean; stored: number; unsafe: number; failed: number; lost: number; stored_bytes: number;
}
export interface UnsafeOutput {
  id: number; instance_id: string | null; kind: "video" | "keyframe"; task: string | null; job_id: string;
  status: "submitted" | "completed"; error: string | null; submitted_at: string;
}
export interface RetrievalState {
  state: string; instance_id: string; destroy_after: boolean; message: string | null; url?: string;
  counts?: { stored: number; failed: number; running: number; missing: number; error: number; remaining: number } | null;
}
export interface TlsState {
  mode: "verify" | "pin" | "off"; endpoint: string | null; pin: { sha256: string; pinned_at: string } | null;
  fallback: { endpoint: string; reason: string; at: number } | null;
  mismatch: { endpoint: string; expected: string; seen: string; at: number } | null;
}
export interface ReplacementState {
  mode: string; proposal: ReplacementOffer | null; job: ReplacementJob | null; states: Record<string, string>;
  conditions?: string; instance_id?: string | null;
  outputs?: { machines: OutputMachine[]; unsafe: UnsafeOutput[] };
  retrieval?: RetrievalState | null; retrieval_states?: Record<string, string>; tls?: TlsState;
}
/** Kiralanan makinenin kurulum günlüğü (setup.sh). */
export interface SetupLog {
  available: boolean; lines: string[]; message?: string; step?: string | null; done?: boolean; ok?: boolean | null;
  total_s?: number | null; phases?: { name: string; seconds: number }[];
}

export interface ComfyWorkflow { kind: string; present: boolean; required: string[]; placeholders: string[]; error: string | null }
export interface ComfyState { backend: string; platform?: "runpod" | "vast"; dir: string; workflows: ComfyWorkflow[] }

/** RunPod Serverless: son okunan durum (kayıttan; RunPod çağrılmaz) ve Ayarlar'daki istenen uç nokta ayarı. */
export interface RunpodConfig {
  name?: string | null; datacenters: string[]; volumes: string[]; pools: string[];
  workers_min: number | null; workers_max: number | null; idle_timeout: number | null; flashboot?: unknown; at: number;
}
export interface RunpodState {
  platform: "runpod" | "vast"; endpoint_id: string | null; datacenter: string; allowed_datacenters: string[];
  balance: { usd: number; spend_per_hr?: number | null; spend_limit?: number | null; at: number } | null;
  balance_low: boolean; min_balance: number; config: RunpodConfig | null;
  health: { jobs: Record<string, number>; workers: Record<string, number>; at: number } | null;
  tick: { idle_workers: number; charged_usd: number; at: number } | null;
  region_problem: string | null; desired: Record<string, unknown> | null; desired_error: string | null;
  outputs?: { machines: OutputMachine[]; unsafe: UnsafeOutput[] }; retrieval?: RetrievalState | null;
  retrieval_states?: Record<string, string>; instance: string | null;
}

/** Kendi GPU sunucusunda çalışan motor (vast.ai'de MiniMax H3): saatlik ücret ve video saniyesi başına GPU saniyesi. */
export interface SelfHosted { gpu_hourly_usd: number; render_seconds: Record<string, number>; keyframe_seconds?: number }

/** RunPod Serverless: GPU tipine göre saniye ücreti. */
export interface Serverless {
  per_second: Record<string, number>; pools?: Record<string, string>;
  /** koşul (fl2va | ref2va) -> çözünürlük -> video saniyesi başına GPU saniyesi (eski düz biçim de okunur) */
  render_seconds: Record<string, Record<string, number>> | Record<string, number>;
  keyframe_seconds?: number; keyframe_cold_seconds?: number; cold_start_seconds?: number;
}

export interface PricingTable {
  self_hosted?: Record<string, SelfHosted>;
  serverless?: { runpod?: Serverless };
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

/** POST /estimate: çekim başına ve toplam tahmini maliyet. */
export interface CostEstimateShot {
  code: string; duration_s: number; billed_seconds: number; engine: string; mode: string; frame: string | null;
  frame_usd: number; edit_mode?: string | null; probe_clip_usd: number; full_clip_usd: number; clips: number; expected_usd: number; max_usd: number;
  /** Çekim sonu karesi (Kn_end, Qwen-Image-Edit Lightning): sonraki çekim kesmeyle geliyorsa. */
  end_frame?: boolean; end_frame_usd?: number;
}
export interface CostEstimate {
  shots: CostEstimateShot[]; model_loads: number; cold_start_usd: number; expected_usd: number; max_usd: number;
  end_frames?: number; end_frame_usd?: number;
  settings: { probe_n: number; probe_resolution: string; full_n: number; full_resolution: string; max_correction_rounds: number };
  platform: string; dry_run: boolean;
}
