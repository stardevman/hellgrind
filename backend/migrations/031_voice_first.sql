-- Ses-önce akışı: diyalog animasyondan önce kaydedilir; çekim süresi ve tempo sesten çıkar.
-- sequences.voice_locked_at: sekansın sesi kilitlendi (replik düzenleme kapalı; voice_first açıkken görüntü üretimi
--   ancak kilitten sonra başlar). NULL = kilitsiz.
-- scenes.duration_source: çekim süresinin kaynağı; script (senaryodan tahmin) | voice (kilitte ölçülen sesten).
-- projects.animatic_key: storyboard karelerinin geçici ya da final sesle dizildiği animatik (kesme listesi .json yanında).
ALTER TABLE sequences ADD COLUMN IF NOT EXISTS voice_locked_at timestamptz;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS duration_source text NOT NULL DEFAULT 'script';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS animatic_key text;
