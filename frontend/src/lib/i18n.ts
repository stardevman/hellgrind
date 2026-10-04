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
  completed: "Tamamlandı",
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

export const KIND_TR: Record<string, string> = {
  fal_wan: "Wan", seedance: "Seedance", higgsfield: "Higgsfield", flux_lora: "Flux kareleri",
  v2v: "Previs aktarımı", fal: "Dudak senkronu", elevenlabs: "ElevenLabs", anthropic: "Hermes (LLM)",
};

export const ALIGN_TR: Record<string, string> = { style: "stil", content: "tarif", generic: "genel" };

export const GRADE_FIELDS = ["exposure", "contrast", "saturation", "gamma", "temperature"] as const;
