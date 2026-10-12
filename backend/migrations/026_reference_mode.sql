-- Referans + senaryo ile video (VIDEO_INPUT_MODE=reference): kare çizilmez; yönetmen render'dan önce sahnenin kuru
-- çalıştırma planını onaylar (require_plan_approval). Onay planın özetine bağlıdır: istem, referanslar, süre ya da motor
-- değişince yeniden istenir.
ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'plan_review';   -- plan yönetmen onayını bekliyor
-- {"hash", "usd", "at", "part": [çekim kodları]}
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS plan_approval JSONB;
