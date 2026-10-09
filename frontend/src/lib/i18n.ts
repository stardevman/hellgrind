/* Arka ucun gönderdiği değerlerin Türkçe karşılıkları. backend/tests/test_panel_next.py her enum değerinin burada
   karşılığı olduğunu denetler: arka uca yeni bir değer eklenince panel ham İngilizce göstermesin. */

export const STATUS_TR: Record<string, string> = {
  pending: "Sırada", parsed: "Ayrıştırıldı", probing: "Test turu", generating: "Tam tur", correcting: "Düzeltme",
  refined: "Rafine", needs_review: "İnceleme gerekli", audio: "Ses", lipsync: "Dudak senkronu", muxing: "Montaj",
  done: "Hazır", failed: "Hata", frozen: "Donduruldu", blocked: "Engellendi",
  storyboard: "Storyboard", board_review: "Kare onay bekliyor", grading: "Renk",
};

export const WORKING = new Set(["storyboard", "probing", "generating", "correcting", "audio", "lipsync", "grading", "muxing"]);

export const PROJECT_STATUS_TR: Record<string, string> = {
  draft: "Taslak", parsing: "Ayrıştırılıyor", producing: "Üretimde", paused: "Durduruldu", killed: "İptal edildi",
  completed: "Tamamlandı", idle: "Beklemede",
};

export const TYPE_TR: Record<string, string> = { character: "Karakter", location: "Mekân", prop: "Nesne" };

export const ASTATUS_TR: Record<string, string> = {
  draft: "Taslak", still_approved: "Durağan onaylı", motion_tested: "Hareket testi onaylı", locked: "Kilitli",
};
export const ASTATUS_ORDER = ["draft", "still_approved", "motion_tested", "locked"];

export const REF_TR: Record<string, string> = {
  still: "Durağan görsel", turnaround: "Dönüş videosu (360°)", expressions: "İfade sayfası", voice_sample: "Ses örneği",
};
export const REF_KINDS: Record<string, string[]> = {
  character: ["turnaround", "still", "expressions", "voice_sample"], location: ["still", "turnaround"], prop: ["still", "turnaround"],
};
export const REF_ACCEPT: Record<string, string> = {
  still: "image/*", expressions: "image/*", turnaround: "video/*", voice_sample: "audio/*",
};

export const TEST_TR: Record<string, string> = {
  queued: "Hareket testi sırada…", running: "Hareket testi üretiliyor…",
  passed: "Kusur bulunmadı: izleyip onaylayabilirsin.", rejected: "Kusurlu", failed: "Üretilemedi",
};

export const BOARD_TR: Record<string, string> = {
  pending: "Onay bekliyor", approved: "Onaylı", changes_requested: "Değişiklik istendi",
};

export const EMOTION_TR: Record<string, string> = {
  neutral: "Nötr", joy: "Sevinç", anger: "Öfke", fear: "Korku", sadness: "Hüzün", surprise: "Şaşkınlık",
  disgust: "Tiksinti", tension: "Gerilim",
};
export const EMOTION_COLOR: Record<string, string> = {
  neutral: "#A39E95", joy: "#E4A23E", anger: "#C2573F", fear: "#86A5B8", sadness: "#6F8CA0", surprise: "#D9B77A",
  disgust: "#8FA66A", tension: "#C9895A",
};

export const ANOMALY_TR: Record<string, string> = {
  black: "siyah kare", freeze: "donmuş görüntü", low_clip: "düşük kalite skoru", empty: "boş dosya",
  no_face: "karakterin yüzü görünmedi", low_identity: "kimlik eşiğin altında", low_likeness: "karakter benzerliği düşük",
  continuity_jump: "önceki kareyle mekân farklı", transition_jump: "son kare sonraki çekimin karesine uymuyor",
  unreadable: "okunamayan dosya", flat: "tek renk kare",
};

export const SHOT_TR: Record<string, string> = {
  extreme_wide: "çok geniş", wide: "geniş", pov: "öznel", medium: "orta", over_shoulder: "omuz üstü",
  close_up: "yakın", extreme_close_up: "çok yakın",
};
export const MOVE_TR: Record<string, string> = {
  static: "sabit", dolly_in: "yaklaşma", dolly_out: "uzaklaşma", pan: "pan", tilt: "tilt", crane: "vinç",
  orbit: "yörünge", tracking: "takip", handheld: "el kamerası",
};

export const METHOD_TR: Record<string, string> = {
  generate: "Yapay zekâ üretimi", previs_guide: "Previs rehberi", external: "Dış çekim",
};

export const PURGE_TR: Record<string, string> = {
  quality_gate: "Kalite kontrolünde elendi", not_top_k: "Rafine havuza giremedi", superseded: "Yeni üretimle değişti",
  interrupted: "Yarıda kesildi", frozen: "Üretim durdurulunca kesildi", failed: "Üretilemedi", unknown: "Nedeni kayıtsız",
};

/** Video motorları: panelde görünen ad ve nerede çalıştığı. */
export const ENGINE_TR: Record<string, string> = {
  h3: "MiniMax H3", seedance: "Seedance · fal.ai", fal_wan: "Wan 2.2 · fal.ai (eski)", higgsfield: "Higgsfield",
};
/** MiniMax H3 koşulu: kompozisyon seed'le değil koşulla kontrol edilir. */
export const H3_MODE_TR: Record<string, string> = {
  fl2va: "İlk kareden (FL2VA): storyboard karesi", ref2va: "Karakter referanslarıyla (Ref2VA)",
};
export const BOARD_SOURCE_TR: Record<string, string> = {
  local: "yerel çizim", flux: "Flux (fal.ai)", comfyui: "Qwen-Image (metinden)", qwen_edit: "Qwen-Image-Edit (karakter referansından)",
  upload: "yüklenen kare",
};
/** Çekime geçiş (core/chain.py). */
export const TRANSITION_TR: Record<string, string> = {
  continuous: "Devam (aynı sahne): kare önceki çekimin karesinden", cut: "Kesme: açı değişir, mekân ve durum korunur",
  new_scene: "Yeni sahne: zincir kırılır, kare referanslardan",
};
/** Çekimin ilk kare modeli (core/h3mode.keyframe_model). */
export const KEYFRAME_MODEL_TR: Record<string, string> = {
  qwen_edit: "Qwen-Image-Edit-2511, karakter referansından", qwen: "Qwen-Image, yalnızca metinden",
};
/** ComfyUI iş akışları (Ayarlar › MiniMax H3). */
export const COMFY_KIND_TR: Record<string, string> = {
  t2v: "Metinden video (t2v)", i2v: "İlk kareden video (i2v)", r2v: "Referanslarla video (r2v)", keyframe: "Storyboard ilk karesi (görsel)",
  edit_keyframe: "Karakterli ilk kare (Qwen-Image-Edit)",
};

/** Yönetmenin çekim başına seçebildiği motorlar (ilki varsayılan). */
export const ENGINE_CHOICES = ["h3", "seedance"] as const;

export const KIND_TR: Record<string, string> = {
  h3: "MiniMax H3", fal_wan: "Wan", seedance: "Seedance", higgsfield: "Higgsfield", flux_lora: "Flux kareleri", flux_kontext: "Referanslı kareler",
  v2v: "Previs aktarımı", fal: "Dudak senkronu", elevenlabs: "ElevenLabs", anthropic: "Hermes (LLM)",
};

export const ALIGN_TR: Record<string, string> = { style: "stil", content: "tarif", generic: "genel" };

export const GRADE_FIELDS = ["exposure", "contrast", "saturation", "gamma", "temperature"] as const;

/* ---------- servis geçmişi ---------- */

export const CALL_MODE_TR: Record<string, string> = { real: "Gerçek", virtual: "Sanal", unknown: "Bilinmiyor" };
export const CALL_STATUS_TR: Record<string, string> = { running: "Sürüyor", succeeded: "Başarılı", failed: "Başarısız" };
export const TIER_TR: Record<string, string> = { probe: "Test turu", full: "Tam tur" };
/** Sağlayıcıya giden isteğin kayda geçen parametreleri (app/live: vast_h3, fal_video.NOTED_ARGUMENTS, elevenlabs, hermes_llm). */
export const STEP_TR: Record<string, string> = {
  unet_variant: "H3 DiT türü", gpu: "GPU", idle_workers: "Boşta worker", resolution: "Çözünürlük", num_frames: "Kare sayısı", frames_per_second: "Kare hızı", duration: "Süre (sn)",
  image_size: "Görsel boyutu", num_images: "Görsel sayısı", seed: "Tohum", strength: "Rehbere bağlılık", sync_mode: "Senkron modu",
  aspect_ratio: "En-boy oranı", generate_audio: "Yerleşik ses", loras: "LoRA", characters: "Karakter sayısı", voice_id: "Ses kimliği",
  emotion: "Duygu", voice: "Ses ayarları", http_status: "HTTP durumu", input_tokens: "Girdi belirteci",
  output_tokens: "Çıktı belirteci", reused: "Önceki denemeden", steps: "Adım sayısı", references: "Referans görsel", task: "Görev",
  negative_dropped: "Negatif istem gönderilmedi", gpu_seconds: "GPU süresi (sn)", backend: "Arka uç",
  keyframe: "İlk kareden", negatives_unmapped: "Kısıta çevrilemeyen negatifler", idle_seconds: "Boşta süre (sn)",
};
