-- Faz 15: çekim üretim yöntemi, dış çekimler ve elle düzeltme
-- Karmaşık ya da çok özel hareketler tarif ederek üretilmez: previs (3D taslak) rehberiyle ya da dışarıda bitirilip
-- yüklenir. Üretilen klipler elle düzeltilebilir; her düzeltme sürümlenir, orijinal korunur.

ALTER TABLE scenes
  ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'generate'
    CHECK (method IN ('generate','previs_guide','external')),
  ADD COLUMN IF NOT EXISTS external_key TEXT,          -- external/{sahne}/{sha}.mp4
  ADD COLUMN IF NOT EXISTS external_hash TEXT,         -- dosyanın içerik özeti: görsel özetin yerine geçer
  ADD COLUMN IF NOT EXISTS guide_key TEXT,             -- guides/{sahne}/{sha}.mp4
  ADD COLUMN IF NOT EXISTS guide_hash TEXT;

CREATE TABLE revisions (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  variation_id UUID NOT NULL REFERENCES variations(id) ON DELETE CASCADE,
  scene_id     UUID REFERENCES scenes(id) ON DELETE CASCADE,
  version      INT  NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('video','first_frame')),
  source_key   TEXT NOT NULL,                          -- yüklenen dosya (video ya da ilk kare görseli)
  video_key    TEXT NOT NULL,                          -- kullanılan video (ilk karede kareden üretilen klip)
  content_hash TEXT NOT NULL,
  note         TEXT,
  anomalies    JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (variation_id, version)
);
CREATE INDEX revisions_variation_idx ON revisions (variation_id);

-- Varyasyonun etkin revizyonu; boşsa orijinal üretim kullanılır.
ALTER TABLE variations ADD COLUMN IF NOT EXISTS revision_id UUID REFERENCES revisions(id) ON DELETE SET NULL;
