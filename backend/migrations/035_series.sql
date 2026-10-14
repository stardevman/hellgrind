-- Dizi hiyerarşisi (docs/dizi-hiyerarsisi.md): Dizi › Sezon › Bölüm › Sekans › Çekim. Bölüm, bugünkü projedir:
-- bütçe, Kill-Switch, sürümler, müşteri kapıları ve teslim proje başına çalışmaya devam eder. Sezonu olmayan proje
-- (season_id boş) bağımsız bir filmdir; geriye dönük taşıma yok.
CREATE TABLE IF NOT EXISTS series (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title          text NOT NULL,
    settings       jsonb NOT NULL DEFAULT '{}',
    budget_cap_usd numeric(12, 2),
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seasons (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id      uuid NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    number         integer NOT NULL CHECK (number >= 1),
    title          text,
    budget_cap_usd numeric(12, 2),
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (series_id, number)
);

ALTER TABLE projects ADD COLUMN IF NOT EXISTS season_id uuid REFERENCES seasons(id) ON DELETE SET NULL;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS episode_no integer CHECK (episode_no >= 1);
CREATE UNIQUE INDEX IF NOT EXISTS projects_season_episode ON projects (season_id, episode_no) WHERE season_id IS NOT NULL;
