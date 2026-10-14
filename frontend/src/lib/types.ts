/* API yanıtlarının panelin kullandığı alanları. Kaynak: backend/app/api/*.py */

export type Emotion = "neutral" | "joy" | "anger" | "fear" | "sadness" | "surprise" | "disgust" | "tension";

export interface QualityMeta { mode: "dry" | "real" | "invalid"; ready: boolean; reason: string | null }

export interface Preset {
  probe_n?: number; full_n?: number; wave_size?: number; max_correction_rounds?: number; refined_k?: number;
  probe_resolution?: string; full_resolution?: string;
}

export interface Meta {
  dry_run: boolean;
  /** Kendi barındırdığımız MiniMax H3'ün platformu: kapali (varsayılan; video fal.ai'de), runpod ya da vast (deneysel). */
  h3_platform?: H3Platform;
  /** Storyboard ve zincir karelerinin çizildiği yer (fal: Qwen-Image / Qwen-Image-Edit). */
  keyframe_platform?: string;
  /** Video motorları (fal.ai): taslak ve final motoru, seçilebilir motorlar ve çözünürlükler (Ayarlar'daki varsayılanlar). */
  video?: VideoMeta;
  hermes_mode: string;
  tts_engine: string;
  quality?: QualityMeta;
  presets: Record<string, Preset>;
  defaults: Record<string, number | string | boolean> & { probe_n: number; full_n: number };
  sample_script: string;
  trial_script?: string;
  /** Video girdisi: reference (varsayılan; kare üretilmez, sahne referanslarla tek istekte) ya da keyframe. */
  video_input_mode?: "reference" | "keyframe";
  /** Referans modunda sahnenin üretimi: whole_scene (aynı kodlu çekimler tek parça) ya da per_shot. */
  scene_render_mode?: "whole_scene" | "per_shot";
}

export type H3Platform = "runpod" | "vast" | "kapali";

/** Video motoru: h3max (MiniMax H3 Max, taslak), seedance25 (Seedance 2.5, final), seedance (2.0 fast), h3 (RunPod). */
export interface VideoEngine { id: string; label: string; disabled: boolean }

export interface VideoMeta {
  draft_engine: string; final_engine: string; engines: VideoEngine[];
  h3max_resolutions: string[]; seedance25_resolutions: string[];
  h3max_resolution: string; seedance25_resolution: string; seedance25_audio: boolean;
  /** Seedance 2.5 taslağı (draft: true, 480p önizleme; 7 gün içinde 1080p'ye tamamlanabilir) Ayarlar'da açık mı. */
  seedance25_draft?: boolean;
  /** Upscale (1080p) uç noktası (Ayarlar'daki varsayılan) ve seçilebilir uç noktalar. */
  upscale_endpoint?: string; upscale_endpoints?: string[];
}

/* ---------- bitirme: Seedance taslağını tamamlama ve upscale ---------- */

/** Çekimin Seedance 2.5 taslağı (overview): draft_id 7 gün geçerlidir; remaining_s ve expired okuma anında hesaplanır. */
export interface SeedanceDraft {
  draft_id: string; created_at: number; expires_at: number; part: string[]; part_hash: string;
  variations: Record<string, string>; duration_s: number; local: boolean; completed_at?: number | null;
  remaining_s: number; expired: boolean;
}
/** Upscale tahmini (pricing.upscale_cost): çıktı 1920x1080, kaynak fps; warning: uç noktanın süre sınırı. */
export interface UpscaleCost {
  endpoint: string; usd: number; reserve_usd: number; seconds: number; fps: number; target: string; tier: string; warning?: string;
}
/** Taslağı 1080p'ye tamamlamanın tahmini (pricing.seedance25_complete_cost): reserve_usd %20 paylıdır. */
export interface CompleteCost { usd: number; reserve_usd: number; tokens: number; duration_s?: number; margin?: number; [key: string]: unknown }
/** GET /scenes/{id}/draft: tamamlamanın uygunluğu, tahmini ve daha ucuz seçenek (upscale). */
export interface DraftPreview {
  available: boolean; reason: string | null; draft?: SeedanceDraft; part?: string[] | null; seconds?: number;
  cost?: CompleteCost; label?: string; remaining_usd?: number; budget_ok?: boolean;
  cheaper?: { kind: "upscale"; endpoint: string; cost: UpscaleCost; label: string };
}
/** POST /scenes/{id}/seedance/complete. */
export interface CompleteResult { queued: boolean; part: string[]; total_usd: number; reserve_usd: number; draft_id: string }
export type UpscaleScope = "variation" | "scene" | "project";
export interface UpscaleItem { variation_id: string; scene_id: string; scene_code: string; seconds: number; fps: number; from: string | null; cost: UpscaleCost }
/** POST …/upscale (kuru ya da onaylı): klip başına ve toplam tahmin, diğer uç noktaların toplamı, kalan bütçe. */
export interface UpscalePlan {
  scope: UpscaleScope; endpoint: string; target: string; fps: string | number; items: UpscaleItem[]; total_usd: number;
  alternatives: Record<string, number>; body: Record<string, unknown> | null; remaining_usd: number | null; budget_ok: boolean;
  warnings: string[]; queued?: number; network?: false;
}

/** Çekimin fal.ai işi (bekleyen ya da son): kimlik, uç nokta, durum, hata ve sonucun özeti. */
export interface FalRequest {
  request_id?: string; endpoint?: string; engine?: string; mode?: string;
  status?: "submitted" | "completed" | "failed"; error?: string;
  submitted_at?: string | number; completed_at?: string | number;
  result?: { seed?: number; draft_id?: string; billed?: number; endpoint?: string };
}

/** fal.ai video tahmini (pricing.fal_video_cost): reserve_usd bütçe kapısının ayırdığı tutardır. */
export interface FalCost { usd?: number; reserve_usd: number; base_usd?: number; [key: string]: unknown }

export interface FalReference { image: number; token: string; kind: string; name: string; key: string; description: string }
/** Referans modunda görseli olmayan varlık (video üretilemez) ve referans sınırında düşen. */
export interface PlanMissing { kind: string; name: string; asset_id: string | null }
/** Parçanın ilk çekiminin geçişi ve önceki klibin son karesi (yalnızca "continuous" geçişte verilir). */
export interface PartContinuity {
  transition: string; previous: string | null; frame_key: string | null; expected: boolean; winner_id: string | null; note: string | null;
  /** Sahne içi model değişimi: önceki çekim başka motorla üretildi (model değişimi sahne sınırında önerilir). */
  model_switch?: boolean;
}
/** Parça klibinin çekimlere bölünmesi: klipte bulunan kesmeler (sn), her iç sınırın kaynağı (klipteki kesme ya da
    planlı sınır) ve uyarı. fal-plan'da kazanan klibin gerçek ve planlı segmenti de gelir. */
export interface PartSplit {
  cuts: number[]; sources: string[]; warning: string | null;
  segment?: [number, number] | null; segment_planned?: [number, number] | null;
}
/** Karma üretim: kazanan klip, whole_scene parçasının kardeşlerinden başka bir üretimden (geçiş tutarsız olabilir). */
export interface Generation {
  consistent: boolean; part: string; engine: string; variation_id: string; mismatched: string[]; message: string;
}
/** Karma üretimin tek tık seçenekleri (promote yanıtı): adopt (bütün sahne bu üretime) ya da shot (çekim ayrı üretilir). */
export interface MixOption { action: string; label: string; path: string }
/** Yükseltmenin bir parçası (tek istek): çekimleri, süresi ve tahmini. */
export interface UpgradePart { part: string[]; duration_s: number; usd: number; endpoint: string; plan_hash?: string }
/** "Seedance 2.5 ile yükselt"in tahmini (yazılmaz): referans modunda bütün sahne. */
export interface UpgradePreview {
  available: boolean; reason: string | null; engine: string; engine_label: string;
  scope?: "scene" | "shot"; shots?: string[]; parts?: UpgradePart[]; total_usd?: number; cost?: FalCost;
  remaining_usd?: number; budget_ok?: boolean; mode?: string; endpoint?: string;
}
export interface FalUpload { field: string; key: string; path?: string; upload?: string; sha256?: string }

/** GET /scenes/{id}/fal-plan ve POST /scenes/{id}/render?dry_run=true: fal.ai'ye gidecek istek, gönderilmeden. */
export type PromptSource = "llm" | "rules" | "override";
/** Sahne istemi yazıcının (LLM) planla gelen durumu: önbellekteki yazım, yönetmen istemi, yazılmadıysa tahmin. */
export interface PromptWriterMeta {
  writer?: "llm" | "rules"; input_hash?: string; writing?: boolean;
  llm?: { ok: boolean; errors: string[]; model: string; input_tokens: number; output_tokens: number; usd: number; attempts: number } | null;
  override?: { saved_at?: number; stale: string[]; part?: string[] } | null;
  estimate?: { input_tokens: number; output_tokens: number; usd: number } | null;
}
/** POST /scenes/{id}/prompt/rerender: parçanın şu anki istemiyle (yönetmen istemi) yeniden üretim; dry_run tahmin. */
export interface PromptRerender {
  dry_run?: boolean; part: string[]; engine: string; source: PromptSource; source_label: string; prompt_chars: number;
  total_usd: number; remaining_usd: number; budget_ok: boolean; blocked: boolean;
  dispatched?: Record<string, StageResult & { busy?: string }>;
}
/** POST /scenes/{id}/prompt/write: LLM yazıcı kuyruğa verildi (ya da dry_run tahmini). */
export interface PromptWriteResult {
  dry_run?: boolean; queued?: boolean; note?: string; part: string[]; available: boolean; reason: string | null;
  estimate: { input_tokens: number; output_tokens: number; usd: number }; remaining_usd: number;
}

export interface FalPlan {
  dry_run: boolean; scene_id?: string; scene_code: string; engine: string; engine_label: string; stages: string[];
  paid_mode: boolean; network: false;
  mode?: "i2v" | "ref"; mode_label?: string; reason?: string; endpoint?: string;
  body?: Record<string, unknown>; uploads?: FalUpload[]; references?: FalReference[];
  resolution?: string; audio?: boolean; duration?: number; cost?: FalCost;
  unmapped_negatives?: string[]; notes?: string[];
  upgrade?: UpgradePreview;
  disabled?: boolean; message?: string; suggest?: string; note?: string;
  /** Referans modu: sahnenin parçası (tek istek), zaman çizelgesi, referanslar ve onay. */
  input_mode?: "reference" | "keyframe"; render_mode?: "whole_scene" | "per_shot";
  part?: string[]; part_ids?: string[]; timeline?: { code: string; start: number; end: number }[];
  dropped?: { kind: string; name: string }[]; missing?: PlanMissing[]; continuity?: PartContinuity;
  duration_s?: number; warnings?: string[]; plan_hash?: string; part_hash?: string; approved?: boolean; seed?: number;
  /** Eksik referans var: plan onaylanamaz, video üretilemez. */
  blocked?: boolean;
  approval?: { required: boolean; approved: boolean; plan_hash: string };
  /** Seedance 2.5 taslağı (480p önizleme) ile mi üretilecek; istemin uzunluğu ve motorun sınırı (H3 Max 50000). */
  draft?: boolean; prompt_chars?: number; prompt_limit?: number | null;
  /** İstemin kaynağı (yönetmen istemi > LLM istemi > kurallar istemi), kurallar istemi ve yazıcının durumu. */
  prompt_source?: PromptSource; prompt_source_label?: string; prompt_rules?: string; prompt_writer?: PromptWriterMeta;
  /** 1080p'ye upscale tahmini (uç nokta başına), taslağı tamamlama tahmini ve çekimin taslağı (varsa). */
  upscale_estimate?: Record<string, UpscaleCost>; complete_estimate?: CompleteCost; seedance_draft?: DraftPreview;
  /** Referans modu: kazanan klibin bölünmesi ve karma üretim uyarısı. */
  split?: PartSplit | null; generation?: Generation | null;
  /** Ses referansı (ses-önce): diyalog karışımının anahtarı, istekteki alan, replik sayısı, süre, uyarılar. */
  audio_ref?: AudioRef | null;
}

export interface AudioRef { key: string | null; field: string; lines: number; missing: number; duration_s: number; warnings: string[] }

/** POST /scenes/{id}/plan/approve: parçanın planı onaylandı (render=true ise çekimler üretime verildi). */
export interface PlanApproval {
  approved: boolean; plan_hash: string; part: string[]; total_usd: number;
  dispatched: Record<string, StageResult & { busy?: string }>;
}

/** GET /projects/{id}/render-plan?dry_run=true: her çekimin kuru planı ve toplamlar. */
export interface RenderPlan {
  dry_run: boolean; network: false; paid_mode: boolean; scenes: FalPlan[];
  total_usd: number; upgrade_total_usd: number; engines: { draft: string; final: string };
  /** Ayrı istek sayısı (referans modunda bir parça bir istektir) ve kalan bütçe. */
  requests?: number; remaining_usd?: number; input_mode?: string; render_mode?: string;
}

/** POST /scenes/{id}/upgrade-final: "Seedance 2.5 ile yükselt". */
export interface UpgradeFinal extends StageResult {
  engine: string; from_engine: string | null; draft_variation_id: string; awaiting_board?: boolean;
  /** Yükseltilen çekimler (referans modunda bütün sahne), parçalar, toplam ve çekim başına sonuç. */
  shots?: string[]; parts?: UpgradePart[]; total_usd?: number; dispatched?: Record<string, StageResult & { busy?: string }>;
}

export interface ProjectListItem {
  id: string; title: string; status: string; spent_usd: number; budget_limit_usd: number;
  /** Panelde görünen durum: "producing" yalnızca işlenen bir çekim varken; bitince completed ya da idle. */
  display_status?: string;
  /** Dizi bölümüyse sezonu ve numarası (bağımsız filmde boş); teslim edilip kilitlendiyse locked. */
  season_id?: string | null; episode_no?: number | null; locked?: boolean;
}

/* ---------- dizi hiyerarşisi: Dizi › Sezon › Bölüm (bölüm = proje) ---------- */

export interface EpisodeItem { id: string; title: string; episode_no: number | null; status: string; locked: boolean; locked_label: string | null }
export interface SeasonItem { id: string; number: number; title: string | null; episodes: EpisodeItem[] }
export interface SeriesItem { id: string; title: string; library: number; seasons: SeasonItem[] }

/** Projenin dizideki yeri ve teslim kilidi ("Sürüm sabit · v004"). Bağımsız filmde series/season boş. */
export interface EpisodeInfo {
  series: { id: string; title: string } | null;
  season: { id: string; number: number; title: string | null } | null;
  episode_no: number | null;
  locked: { at: string; label: string | null } | null;
}

/** Bölümde yeni sürümü olan dizi varlığı: bölüm `pinned` sürümünde sabit, kütüphanede `current`. */
export interface SeriesUpdate { asset_id: string; name: string; type: string; pinned: number; current: number }

/** Yeni sürüme geçişin etkisi: yeniden üretilecek çekimler ve tahmini sanal maliyet. */
export interface UpgradeImpact { asset_id: string; name: string; pinned: number | null; current: number; affected: string[]; expected_usd: number; worst_usd: number }

export interface ProjectSettings {
  soft_budget_ratio: number;
  /** Kare tutma (on twos, 12 çizim/sn görünümü): renk aşamasında uygulanır. */
  on_twos?: boolean;
  /** Ses-önce: sesi kilitlenmemiş sekansta görüntü üretimi başlamaz. */
  voice_first?: boolean;
  require_board_approval: boolean;
  asset_min_status: string;
  lint_strictness: string;
  [key: string]: unknown;
}

export interface ContinuityPair {
  from: string; to: string; to_id: string; from_id?: string; jump: boolean;
  delta: { brightness: number; warmth: number; saturation: number; E?: number };
  /** Sıçrama puanı: 1 eşik (ΔL 3 ya da ΔE 4, gevşeklikle çarpılı); üstü sıçrama. */
  score?: number;
  suggestion?: Record<string, number> | null; suggestion_score?: number | null;
  /** Kamerası değişen kesmede ölçü bölgesi (strips | outer) ve eşik gevşekliği (kamera 1,5; yeni sahne 2). */
  region?: string; slack?: number;
  /** Geçişin türü: continuous (aynı andan devam), cut (kesme), new_scene (yeni sahne başlığı). */
  seam?: "continuous" | "cut" | "new_scene";
}

export interface Project {
  id: string; title: string; status: string; display_status?: string;
  budget_limit_usd: number; spent_usd: number; reserved_usd: number;
  frozen: boolean; soft_limited: boolean; settings: ProjectSettings; dry_run: boolean;
  style_version: number | null; lut: boolean;
  /** Etkin stil kılavuzunun çizim türü (kılavuz yoksa 3d: eski davranış). */
  style_medium?: Medium;
  /** Dudak senkronunun bu projedeki durumu (ücretsiz modda atlanır). */
  lipsync?: { mode: "free" | "off" | "no_key" | "paid"; text: string };
  continuity: { pairs: ContinuityPair[] } | null;
  asset_counts: Record<string, number>;
  /** Dizi › sezon › bölüm ve teslim kilidi. */
  episode?: EpisodeInfo;
  /** Yeni sürümü olan dizi varlıkları (bölüm eski sürüme sabit). */
  series_updates?: SeriesUpdate[];
}

export interface Measure { alignment: Record<string, number>; identity: Record<string, number>; faces_seen: number }

/** Referans modunun video kapıları (params.score_detail.video_gates): 0,5 sn'de bir örnek karede kimlik, ölçek ve yüz
    sayısı. scorer "dry": ücretsiz modun sahte skoru (kare ölçülmedi). */
export interface VideoGates {
  step: number; frames?: number; scorer?: string;
  identity?: { identity?: Record<string, number>; look?: Record<string, number>; look_dino?: Record<string, number>; faces_seen?: number; [key: string]: unknown };
  scale?: Record<string, { max: number; measured: number; over: number[]; peak: number; ok: boolean }>;
  face_counts?: number[] | null; passed: boolean; anomalies: string[];
}

export interface Variation {
  id: string; tier: "probe" | "full"; wave_no: number; status: string; seed: number;
  score: number | null; rank: number | null; cost_usd: number; anomalies: string[];
  reason: string | null; stage: string | null; measure: Measure | null; stale?: string | null; review?: string[] | null;
  /** FL2VA last_frame: klibin son karesi hedefe (next: sonraki çekimin karesi, end: çekimin son karesi) ne kadar vardı. */
  arrival?: { score: number; dino: number; identity: number | null; target?: "next" | "end" } | null;
  last_frame_kind?: "next" | "end" | null;
  thumb_url: string | null; video_url: string | null;
  source: string; revisions: number; revision: number | null;
  /** Yönetmen seçimi: kapıdan kalmış olsa da eski olmayan aday seçilebilir; override: seçilmiş aday. */
  overridable?: boolean; override?: boolean;
  /** Referans modu: klibin video kapıları, parçanın çekimleri ve parça klibindeki yeri (sn). */
  gates?: VideoGates | null; part?: string[] | null; segment?: [number, number] | null;
  /** Klibin çözünürlüğü ("480P", "720p", "1080p"); upscale edilmiş ya da taslaktan tamamlanmışsa kaynak varyasyon. */
  resolution?: string | null; upscaled_from?: string | null; completed_from?: string | null;
}

export interface Track {
  id: string; kind: string; source: string; text: string | null; emotion: string | null;
  start_offset_s: number; gain_db: number; version: number;
  character_id: string | null; character: string | null; mood: string | null;
  /** Ölçülen süre (sentez ya da yükleme sonrası) ve dosyanın adresi; seslendirilmemişse null. */
  duration_s?: number | null; file_url?: string | null;
}

export interface SceneAssetRef { id: string | null; type: string; name: string; status: string | null }

export interface Grade { exposure: number; contrast: number; saturation: number; gamma: number; temperature: number }

export interface Scene {
  id: string; scene_code: string; status: string; engine: string | null; sequence_id: string | null;
  /** Motorun Türkçe adı; engine_disabled: motor h3 ve RunPod kapalı (üretilemez). */
  engine_label?: string | null; engine_disabled?: boolean;
  /** Kazanan (rank 1) klibin motoru; çekimin motorundan farklıysa yükseltme bu çekimde tamamlanmamıştır. */
  winner_engine?: string | null;
  /** Karma üretim (null: tutarlı) ve kazanan parça klibinin bölünmesi (kesmeler, sınır kaynakları, uyarı). */
  generation?: Generation | null; split?: PartSplit | null;
  /** fal video seçenekleri (yoksa Ayarlar'daki varsayılan): çözünürlük ve modelin ürettiği ses. */
  video_opts?: { resolution?: string; audio?: boolean; audio_ref?: boolean };
  fal_request?: FalRequest | null;
  /** "Seedance 2.5 ile yükselt" uygun: taslak motorla üretilmiş ve final motorunda değil. */
  upgrade_available?: boolean;
  board_status: string; board_note: string | null; board_url: string | null;
  upgrade_requested: boolean; cut_tier: string | null; draft_url: string | null;
  method: string; method_hint: boolean; external_url: string | null; guide_url: string | null;
  grade: Grade; graded: boolean; lipsync_ready: boolean;
  /** Renk önizleme adreslerinin önbellek anahtarı (kaynak video değişince değişir). */
  grade_rev?: string | null;
  heading: string; description: string | null; emotion: string;
  camera: { shot?: string; movement?: string; angle?: string; lens_mm?: number; side?: number } | null;
  duration_s: number; action_complexity: number; spent_usd: number; last_error: string | null;
  /** Çekim süresinin kaynağı: script (senaryodan tahmin) ya da voice (sekans ses kilidinde ölçülen sesten). */
  duration_source?: "script" | "voice";
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
  /** Kesme karesinin yöntemi: yönetmenin seçimi (null: otomatik), etkin yöntem ve denemelerin yöntemleri. */
  cut_method?: string | null; cut_method_effective?: string | null; cut_attempts?: string[];
  /** Karenin gerçekte çizildiği yol (son çizim) ve açı LoRA'sının tokenleri. */
  board_method?: string | null; angle_tokens?: string | null;
  /** Kesme karesinin sıfırdan kurulma gerekçesi: tarif ve nottaki poz/eylem fiilleri, gerçek boyutu yazılı karakter. */
  pose_words?: string[]; scaled?: boolean; compose_hint?: string[];
  /** Kazanan klibin varış skoru (son kare -> sonraki çekimin karesi ya da çekimin son karesi). */
  arrival?: { score: number; dino: number; identity: number | null; target?: "next" | "end" } | null;
  /** Zincirden gelen karakterler: önceki çekimin karesinde kalanlar (referans, değişmez ve kimlik kapısıyla). */
  chain_cast?: string[];
  /** Çekim sonu karesi (Kn_end): sonraki çekim kesmeyle geliyorsa video bu karede biter. */
  end_board_url?: string | null; end_board_note?: string | null; end_board_score?: number | null; end_board_current?: boolean;
  edit_refs?: { name: string; kind: "character" | "location"; url: string | null }[];
  /** Referans modu: onaylanan plan (özet, tahmin, zaman, parça, motor); plan değişince geçersizdir. */
  plan_approval?: { hash: string; usd: number; at: number; part: string[]; engine: string } | null;
  /** Eski stil: klip etkin stil sürümünden önce üretildi; stil değişikliği yeniden üretmez, yönetmen ayrıca ister. */
  style_stale?: { version: number | null } | null;
  /** Seedance 2.5 taslağı (draft_id, kalan süre); yoksa null. */
  seedance_draft?: SeedanceDraft | null;
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
  /** Ses-önce: sekansın sesi kilitlendiği an (epoch sn); null = kilitsiz. */
  voice_locked_at?: number | null;
}

export interface CutClip {
  scene_code: string; in_s: number; out_s: number; description?: string; emotion?: string;
  /** Animatik: kare (onaylı storyboard karesi ya da çekim kartı) ve sürenin kaynağı (ses ya da çekim süresi). */
  still?: "board" | "card"; timing?: "voice" | "shot"; sequence?: string;
}
export interface CutSequence { code: string; title: string; in_s: number; out_s: number }
export interface Film {
  url: string;
  cut: { clips?: CutClip[]; sequences?: CutSequence[]; duration_s?: number; voiced?: number; boards?: number } | null;
}

export interface PanelEvent { kind: string; ts: number; scene_id?: string; [key: string]: unknown }

export interface Overview {
  project: Project; scenes: Scene[]; sequences: Sequence[]; events: PanelEvent[];
  timeline: Film | null; draft_timeline: Film | null;
  /** Animatik: onaylı kareler (yoksa çekim kartı) sesle, sesin süresine göre. */
  animatic?: Film | null;
  /** Müşteri onay döngüsü özeti: kapılar (Yapılacaklar çipleri) ve müşteri notları (Kurgu işaretçileri). */
  client?: { gates: ClientGateSummary[]; notes: ClientNote[] };
  /** Teslim özeti: şartname var mı, QC sonucu ve sorunları (Kurgu işaretçileri), eksik hak zinciri maddeleri. */
  delivery?: DeliverySummary;
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
  /** Gerçek boyut (ör. "tiny, palm-sized; much smaller than a hen"): istemlere ve ölçek kapısına girer. */
  scale?: string | null;
  /** Dizi kütüphanesindeyse dizinin kimliği (bölüme özel varlıkta boş). */
  series_id?: string | null;
  /** Yalnızca dizi kütüphanesi listesinde: bu bölüme bağlı mı, hangi sürüme sabit. */
  link?: { pinned: number | null; current: number; behind: boolean } | null;
}

/* ---------- stil ---------- */

/** Stil kılavuzunun çizim türü: video istemindeki ortam cümlesi ve varlıkların varsayılan render kuralları. */
export type Medium = "2d" | "3d";

export interface StyleVersion {
  version: number; is_active: boolean; lighting_prefix: string | null; motion_style: string | null;
  rules: Record<string, string>; negative: string | null; palette: string[]; notes: string | null;
  created_at: number | null; prefix: string;
  /** Çizim türü (030 öncesi sürümler 3d) ve video istemine giren stil cümlesi (ışık + hareket + ortam cümlesi). */
  medium: Medium; video_line: string;
}

export interface StyleState {
  active: StyleVersion | null; versions: StyleVersion[]; defaults: Record<string, string>;
  render_styles: Record<string, string>; example: Partial<StyleVersion>;
  /** Çizim türüne göre varsayılan render kuralları, örnek doldurma, Türkçe ad ve video istemindeki ortam cümlesi. */
  defaults_by_medium: Record<Medium, Record<string, string>>; examples: Record<Medium, Partial<StyleVersion>>;
  media: Record<Medium, string>; medium_default: Medium; video_lines: Record<Medium, string>;
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

/** Eksik referans: lint (onaysız varlık) ya da referans modu (status "no_image": görseli yok). */
export interface Blocked { missing: { name: string; asset_id?: string | null; type?: string; status?: string | null }[]; message?: string }
export interface StageResult {
  stages: string[]; blocked?: Blocked | null; hint?: string; awaiting_board?: boolean;
  /** Referans modu: plan onaylanmadı; çekim plan_review'da bekler. */
  awaiting_plan?: boolean; plan?: { part: string[]; plan_hash: string; usd: number };
  /** Ses-önce: sekansın sesi kilitlenmedi (sequence: sekans kodu). */
  awaiting_voice?: boolean; sequence?: string;
}
/** POST /scenes/{id}/promote/{vid}: seçilen klip başka bir üretimdense uyarı ve seçenekler. */
export interface PromoteResult extends StageResult { warning?: string; mixed?: Generation; options?: MixOption[] }
/** POST /scenes/{id}/generation/adopt: parçanın bütün çekimleri seçilen üretime geçti (yeniden üretim ve ücret yok). */
export interface AdoptResult { adopted: boolean; engine: string; part: string[]; stages: string[]; usd: number }
/** POST /scenes/{id}/scene-engine?dry_run=true: bütün sahnenin o motorla tahmini (parça başına), kalan bütçe. */
export interface SceneEnginePlan {
  engine: string; shots: string[]; total_usd: number; parts: { part: string[]; usd: number; endpoint: string }[];
  remaining_usd: number; budget_ok: boolean;
}
/** POST /scenes/{id}/scene-engine?expected_usd=: bütün sahne o motorla yeniden üretiliyor. */
export interface SceneEngineResult { engine: string; shots: string[]; total_usd: number; dispatched: Record<string, StageResult & { busy?: string }> }
/** POST /scenes/{id}/shot-engine: çekim ayrı üretilir; sonraki çekim son kareyle yeniden bağlanır. */
export interface ShotEngineResult { engine: string; relinked: string[]; dispatched: Record<string, StageResult & { busy?: string }>; note: string; total_usd?: number }
/** POST /scenes/{id}/shot-engine?dry_run=true: ayrı üretimin tahmini (yeniden üretilecek parçalar), kalan bütçe. */
export interface ShotEnginePlan extends SceneEnginePlan { relinked: string[] }
export interface DispatchAll {
  planned: Record<string, string[]>; busy: string[]; blocked: Record<string, unknown>;
  capped: string[]; awaiting_board?: string[]; frozen: boolean;
  /** Referans modu: planı onay bekleyen çekimler. */
  awaiting_plan?: string[];
  /** Ses-önce: sekansının sesi kilitlenmediği için görüntüsü bekleyen çekimler. */
  awaiting_voice?: string[];
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
export interface ComfyState { backend: string; platform?: H3Platform; dir: string; workflows: ComfyWorkflow[] }

/** RunPod Serverless: son okunan durum (kayıttan; RunPod çağrılmaz) ve Ayarlar'daki istenen uç nokta ayarı. */
export interface RunpodConfig {
  name?: string | null; datacenters: string[]; volumes: string[]; pools: string[];
  workers_min: number | null; workers_max: number | null; idle_timeout: number | null; flashboot?: unknown; at: number;
}
export interface RunpodState {
  platform: H3Platform; endpoint_id: string | null;
  /** RunPod kullanılmıyor (platform runpod değil); disabled_message: kapali iken gösterilecek metin. */
  disabled?: boolean; disabled_message?: string | null; datacenter: string; allowed_datacenters: string[];
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
  /** Kesme karesinin yöntemi ve kalemleri (kırp + iyileştir; açı LoRA'sı; ara kare). */
  cut_method?: string; cut_attempts?: string[]; cut_place_usd?: number; cut_in_usd?: number | null; angles_usd?: number | null;
}
export interface CostEstimate {
  shots: CostEstimateShot[]; model_loads: number; cold_start_usd: number; expected_usd: number; max_usd: number;
  end_frames?: number; end_frame_usd?: number; cut_in_frames?: number; angles_frames?: number;
  settings: { probe_n: number; probe_resolution: string; full_n: number; full_resolution: string; max_correction_rounds: number };
  platform: string; dry_run: boolean;
}

/** GET /admin: yönetici şifresi tanımlı mı, bu tarayıcı yönetici mi (şifre yoksa her zaman). */
export interface AdminState { required: boolean; admin: boolean }

/* ---------- ses-önce ---------- */

export interface VoiceLine {
  track_id: string; n: number; character_id: string | null; character: string | null; text: string | null; emotion: string | null;
  source: string; final: boolean; engine: string | null; url: string | null; duration_s: number | null;
  start_offset_s: number; slot_s: number; drift: boolean; filename: string | null; version: number;
  /** Ses denetimi (Duyu-small): ses yazıya döküldü ve metinle karşılaştırıldı; cer = harf hata oranı (0-1). */
  check: { ok: boolean; cer: number; heard: string } | null;
}
export interface VoiceShot {
  scene_id: string; scene_code: string; heading: string; description: string | null; duration_s: number;
  duration_source: "script" | "voice"; voice_s: number | null; voice_clamped: number | null; has_video: boolean;
  status: string; drift: boolean; drift_text: string | null; lines: VoiceLine[];
}
export interface VoiceSequence {
  id: string | null; code: string; title: string; locked: boolean; locked_at: number | null; shots: VoiceShot[];
  lines: number; final: number;
}
/** GET /projects/{id}/voice: Ses sayfası. */
export interface VoiceState {
  sequences: VoiceSequence[]; voice_first: boolean;
  /** Ses denetimi açık ve Duyu-small modeli kurulu. */
  checker: boolean;
  counts: { lines: number; final: number; temp: number; unmeasured: number; drift: number; unlocked: number;
    mismatch: number; unchecked: number };
  pads: { in: number; gap: number; out: number; drift: number }; drift_text: string;
}
/** POST /sequences/{id}/voice-lock. */
export interface VoiceLockResult { sequence_id: string; code: string; locked: boolean; locked_at: number | null; retimed?: string[]; drift?: string[]; clamped?: string[] }
/** POST /projects/{id}/voice/upload. */
export interface VoiceUpload { key: string; duration_s: number; filename: string; match: { scene_code?: string; n?: number } }

/* ---------- müşteri onay döngüsü ---------- */

export interface ClientGateSummary { gate: string; status: string; rounds: number; limit: number; sent?: boolean }
export interface ClientNote {
  id: string; version_id: string | null; gate: string; gate_tr: string; at_s: number | null; shot_offset_s: number | null;
  scene_id: string | null; scene_code: string | null; text: string; resolved: boolean; created_at: number | null; label?: string | null;
}
export interface FilmVersionRow {
  id: string; number: number; label: string; gate: string; gate_tr: string; kind: string; url: string; status: string;
  created_at: number | null; decided_at: number | null; cost_usd: number | null; duration_s?: number | null;
}
export interface ReviewLinkRow { id: string; token: string; url: string; version_id?: string; label: string | null; expires_at: number; revoked?: boolean; expired?: boolean }
export interface ClientGateRow extends ClientGateSummary {
  label: string; over_limit: boolean; note: string | null; video: boolean; versions: FilmVersionRow[]; cost_usd: number;
}
/** GET /projects/{id}/client: Teslim › Müşteri. */
export interface ClientState { gates: ClientGateRow[]; versions: FilmVersionRow[]; round_limit: number; links: ReviewLinkRow[]; notes: ClientNote[] }
/** GET /review/{token}: müşterinin gördüğü her şey (bütçe, motor, maliyet yok). */
export interface ReviewState {
  title: string; label: string; gate: string; gate_tr: string; status: string; video_url: string; expires_at: number;
  sent_at: number | null; clips: { scene_code: string; in_s: number; out_s: number }[];
  notes: { id: string; at_s: number | null; scene_code: string | null; text: string; created_at: number | null }[];
}

/* ---------- teslim: şartname, QC, paket, hak zinciri ---------- */

export interface DeliverySpec {
  resolution: string; fps: string; codec: string; loudness_lufs: number; srt: boolean; me_mix: boolean; textless: boolean;
  preset: string | null;
}
export interface QcIssue { start: number; end: number; tc: string; scene_code: string | null; detail: string }
export interface QcCheck { id: string; name: string; passed: boolean; summary: string; issues: QcIssue[] }
export interface QcReport {
  duration_s: number; fps: number; target_lufs: number; passed: boolean; checks: QcCheck[]; film_key: string; checked_at: string;
  stale?: boolean;
}
export interface RightsFile { id: string; filename: string; note: string | null; url: string; created_at: number | null }
export interface RightsKind { kind: string; label: string; files: RightsFile[]; auto?: boolean; rows?: number }
export interface PackageFile { path: string; label: string; ready: boolean }
/** GET /projects/{id}/delivery. */
export interface DeliveryState {
  spec: DeliverySpec; custom: boolean; presets: Record<string, Partial<DeliverySpec> & { label: string }>;
  options: { resolution: string[]; fps: string[]; codec: Record<string, string> };
  film: { key: string | null; url: string | null }; qc: QcReport | null;
  rights: { items: RightsKind[]; missing: string[]; ai_rows: number };
  package: { files: PackageFile[]; url: string | null };
}
export interface DeliverySummary {
  spec: boolean; package: boolean; rights_missing: string[];
  qc: { passed: boolean; stale: boolean; failed: string[]; issues: (QcIssue & { check: string; name: string })[] } | null;
}

/* ---------- renk betiği ---------- */

export interface Swatch { hex: string; weight: number }
/** GET /projects/{id}/color-script: sekansların baskın paleti (çekimlerinkiyle), stil kılavuzunun hedef paleti. */
export interface ColorScript {
  sequences: { id: string; code: string; title: string; palette: Swatch[]; shots: { code: string; palette: Swatch[] }[]; distance: number | null }[];
  target: string[]; style_version: number | null;
}
