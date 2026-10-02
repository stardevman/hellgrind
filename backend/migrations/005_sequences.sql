-- Faz 11: sekans hiyerarşisi ve film düzeyinde ses
-- Film sekanslardan oluşur (Havalimanı, Taksi, Polis...). Müzik sekans ya da film düzeyinde yaşar ve çekimler arasında
-- kesintisiz akar; her sekansın isteğe bağlı bir bütçe tavanı vardır.

CREATE TABLE sequences (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id     UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  code           TEXT NOT NULL,                                   -- SEQ01
  title          TEXT NOT NULL,
  seq_order      INT  NOT NULL,
  budget_cap_usd NUMERIC(12,2) CHECK (budget_cap_usd > 0),        -- boş: tavan yok (yalnızca proje limiti)
  spent_usd      NUMERIC(12,4) NOT NULL DEFAULT 0,
  reserved_usd   NUMERIC(12,4) NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','capped')),
  settings       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, code)
);
CREATE TRIGGER sequences_touch BEFORE UPDATE ON sequences FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

ALTER TABLE scenes ADD COLUMN IF NOT EXISTS sequence_id UUID REFERENCES sequences(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS scenes_sequence_idx ON scenes (sequence_id);
ALTER TABLE cost_ledger ADD COLUMN IF NOT EXISTS sequence_id UUID REFERENCES sequences(id) ON DELETE SET NULL;

-- Film ya da sekans düzeyindeki fon kanalları. start_s/end_s sekansın başına göredir (sekanssızsa filmin başına);
-- end_s boşsa sekansın (ya da filmin) sonuna kadar çalar.
CREATE TABLE timeline_tracks (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  sequence_id  UUID REFERENCES sequences(id) ON DELETE CASCADE,
  kind         audio_kind NOT NULL CHECK (kind <> 'dialogue'),
  source       TEXT NOT NULL CHECK (source IN ('procedural','upload')),
  file_key     TEXT,
  params       JSONB NOT NULL DEFAULT '{}'::jsonb,
  start_s      REAL NOT NULL DEFAULT 0 CHECK (start_s >= 0),
  end_s        REAL CHECK (end_s IS NULL OR end_s > start_s),
  gain_db      REAL NOT NULL DEFAULT 0,
  fade_in_s    REAL NOT NULL DEFAULT 0 CHECK (fade_in_s >= 0),
  fade_out_s   REAL NOT NULL DEFAULT 0 CHECK (fade_out_s >= 0),
  content_hash TEXT NOT NULL,
  is_active    BOOLEAN NOT NULL DEFAULT true,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX timeline_tracks_active_idx ON timeline_tracks (project_id, sequence_id) WHERE is_active;

-- Mevcut projeler: tüm sahneler tek sekansta. Sahne müzikleri yerinde kalır (film düzeyine taşınmaz: çift müzik olmasın).
INSERT INTO sequences (project_id, code, title, seq_order)
SELECT DISTINCT project_id, 'SEQ01', 'Sekans 1', 1 FROM scenes
ON CONFLICT (project_id, code) DO NOTHING;
UPDATE scenes sc SET sequence_id = q.id
FROM sequences q WHERE q.project_id = sc.project_id AND q.code = 'SEQ01' AND sc.sequence_id IS NULL;
UPDATE sequences q SET spent_usd = COALESCE((SELECT SUM(spent_usd) FROM scenes WHERE sequence_id = q.id), 0);
UPDATE cost_ledger l SET sequence_id = sc.sequence_id FROM scenes sc WHERE sc.id = l.scene_id AND l.sequence_id IS NULL;
