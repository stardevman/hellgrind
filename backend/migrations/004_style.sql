-- Faz 13: stil kılavuzu, ışık dili ve hareket tarzı
-- Stil tek yerden yönetilir: çekim bazında elle yazılmaz. Her değişiklik yeni bir sürümdür; proje başına tek sürüm etkindir.

CREATE TABLE style_bibles (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version         INT  NOT NULL,
  lighting_prefix TEXT NOT NULL DEFAULT '',
  rules           JSONB NOT NULL DEFAULT '{}'::jsonb,   -- varlık türü -> render stili, ör. {"prop": "clean_3d", "location": "painterly"}
  negative        TEXT NOT NULL DEFAULT '',
  motion_style    TEXT NOT NULL DEFAULT '',
  palette         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- ["#E4A23E", ...]
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_active       BOOLEAN NOT NULL DEFAULT false,
  UNIQUE (project_id, version)
);
CREATE UNIQUE INDEX style_bibles_active_uq ON style_bibles (project_id) WHERE is_active;

-- Varlığın render_style alanı artık yalnızca bilinçli bir istisnadır; boşsa kılavuzun tür kuralı geçerlidir.
-- Faz 12'nin yazdığı varsayılanlar (kılavuzun varsayılanlarıyla aynı) boşaltılır. Görsel özete sürüm girdiği için
-- hiçbir çekimin özeti değişmez.
UPDATE assets SET render_style = NULL
WHERE (type = 'character' AND render_style = 'clean_3d')
   OR (type = 'prop'      AND render_style = 'clean_3d')
   OR (type = 'location'  AND render_style = 'painterly');
