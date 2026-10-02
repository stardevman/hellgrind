-- Faz 12: varlık kütüphanesi ve referans eksiksizliği
-- Model eksik bilgiyi uydurur: çekimde geçen her karakter, mekân ve nesnenin onaylı referansı yoksa üretim başlamaz.

ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'blocked';

CREATE TYPE asset_type   AS ENUM ('character','location','prop');
CREATE TYPE asset_status AS ENUM ('draft','still_approved','motion_tested','locked');

CREATE TABLE assets (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id               UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type                     asset_type NOT NULL,
  name                     TEXT NOT NULL,
  description              TEXT,
  render_style             TEXT,                     -- clean_3d | painterly | stylized_2d | serbest metin
  status                   asset_status NOT NULL DEFAULT 'draft',
  version                  INT NOT NULL DEFAULT 1,   -- referans ya da tarif değişince artar (görsel özete girer)
  "references"             JSONB NOT NULL DEFAULT '{}'::jsonb,
  motion_test_variation_id UUID,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, type, name)
);
CREATE TRIGGER assets_touch BEFORE UPDATE ON assets FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Karakterin görsel kimliği varlıktadır; ses alanları (voice_id) karakterde kalır.
ALTER TABLE characters ADD COLUMN IF NOT EXISTS asset_id UUID UNIQUE REFERENCES assets(id) ON DELETE SET NULL;

-- Mekân ve nesne bağları. source: script (Hermes yazar, yeniden yüklemede yenilenir) | manual (panelden, korunur).
CREATE TABLE scene_assets (
  scene_id UUID NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  asset_id UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  role     TEXT NOT NULL,
  source   TEXT NOT NULL DEFAULT 'manual',
  PRIMARY KEY (scene_id, asset_id)
);
CREATE INDEX scene_assets_asset_idx ON scene_assets (asset_id);

-- Hareket testi bir varyasyondur, ama bir sahneye değil bir varlığa aittir.
ALTER TABLE variations ALTER COLUMN scene_id DROP NOT NULL;
ALTER TABLE variations ADD COLUMN IF NOT EXISTS asset_id UUID REFERENCES assets(id) ON DELETE CASCADE;
ALTER TABLE variations ADD CONSTRAINT variations_owner_ck CHECK (scene_id IS NOT NULL OR asset_id IS NOT NULL);
CREATE INDEX variations_asset_idx ON variations (asset_id) WHERE asset_id IS NOT NULL;
ALTER TABLE assets ADD CONSTRAINT assets_motion_test_fk
  FOREIGN KEY (motion_test_variation_id) REFERENCES variations(id) ON DELETE SET NULL;

-- Mevcut her karakter, taslak durumunda bir karakter varlığı alır (henüz onaylı referansı yok).
INSERT INTO assets (project_id, type, name, description, render_style)
SELECT project_id, 'character', name, description, 'clean_3d' FROM characters WHERE asset_id IS NULL
ON CONFLICT (project_id, type, name) DO NOTHING;
UPDATE characters c SET asset_id = a.id
FROM assets a
WHERE c.asset_id IS NULL AND a.project_id = c.project_id AND a.type = 'character' AND a.name = c.name;
