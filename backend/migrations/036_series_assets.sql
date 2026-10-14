-- Dizi kütüphanesi: varlık ve karakter ya bir bölüme (project_id) ya da diziye (series_id) aittir; ikisinden tam biri dolu.
-- Dizi varlığı bölüme kopyalanmaz, bağlanır (episode_assets). pinned_version boşsa bölüm kütüphanedeki son sürümü
-- kullanır; doluysa o sürüme sabittir (görsel özet sabit sürümü alır). pinned_embedding karakterin vektör sürümünü
-- aynı biçimde sabitler. Sürüm artınca çekimi üretilmiş bölümler eski sürümde sabitlenir; yeni sürüme geçiş önizlemeyle.
ALTER TABLE assets ADD COLUMN IF NOT EXISTS series_id uuid REFERENCES series(id) ON DELETE CASCADE;
ALTER TABLE assets ALTER COLUMN project_id DROP NOT NULL;
DO $$ BEGIN
    ALTER TABLE assets ADD CONSTRAINT assets_owner_check CHECK ((project_id IS NULL) <> (series_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS assets_series_name ON assets (series_id, type, name) WHERE series_id IS NOT NULL;

ALTER TABLE characters ADD COLUMN IF NOT EXISTS series_id uuid REFERENCES series(id) ON DELETE CASCADE;
ALTER TABLE characters ALTER COLUMN project_id DROP NOT NULL;
DO $$ BEGIN
    ALTER TABLE characters ADD CONSTRAINT characters_owner_check CHECK ((project_id IS NULL) <> (series_id IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS characters_series_name ON characters (series_id, name) WHERE series_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS episode_assets (
    project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_id         uuid NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    pinned_version   integer,
    pinned_embedding integer,
    added_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, asset_id)
);
