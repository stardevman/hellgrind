-- Karakter türü: human (yüz kimliği SFace ile) | non_human (robot, hayvan, yaratık: görsel benzerlik OpenCLIP ile; yüz
-- aranmaz). Varlıkların referans geçmişi: yeni referans eskisinin yerine geçince eskisi silinmez, burada tutulur.
ALTER TABLE characters ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'human';
DO $$ BEGIN
  ALTER TABLE characters ADD CONSTRAINT characters_kind_check CHECK (kind IN ('human', 'non_human'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS reference_history JSONB NOT NULL DEFAULT '[]'::jsonb;
