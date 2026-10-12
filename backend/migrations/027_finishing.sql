-- Seedance 2.5 taslağı (draft: true -> 480p önizleme + draft_id): 7 gün içinde draft/complete ile 1080p'ye tamamlanır.
-- {"draft_id", "created_at", "expires_at", "part": [çekim kodları], "part_hash", "variations": {çekim: varyasyon},
--  "duration_s", "local", "completed_at"?}
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS seedance_draft JSONB;
