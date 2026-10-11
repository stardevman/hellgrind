-- Varlığın gerçek boyutu (ör. Pip: "tiny, palm-sized; much smaller than a hen"): referans görseli yakın plan olsa da karede
-- bu boyutta çizilir; istemlere ve ölçek kapısına girer.
ALTER TABLE assets ADD COLUMN IF NOT EXISTS scale TEXT;
