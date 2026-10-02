-- Faz 16: renk aşaması ve süreklilik kontrolü
-- Film geneli LUT + çekim bazında renk ayarı (GRADE aşaması: dudak senkronu ile montaj arasında) ve ardışık çekimler
-- arasındaki renk sıçramalarının tespiti.

ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'grading';

ALTER TABLE scenes
  ADD COLUMN IF NOT EXISTS grade JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {exposure, contrast, saturation, gamma, temperature}
  ADD COLUMN IF NOT EXISTS grade_hash TEXT,                            -- son uygulanan renk ayarının özeti (LUT dahil)
  ADD COLUMN IF NOT EXISTS graded_video_key TEXT;                      -- graded/{sahne}/{özet}.mp4; boşsa ayar yok

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS lut_key TEXT,                               -- luts/{proje}/{sha}.cube
  ADD COLUMN IF NOT EXISTS lut_hash TEXT,
  ADD COLUMN IF NOT EXISTS continuity JSONB;                           -- son süreklilik raporu
