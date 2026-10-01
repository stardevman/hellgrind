-- Hell Grind çekirdek şema (Kademe 1)
-- PostgreSQL bu dosyayı yalnızca veri klasörü ilk oluşturulduğunda çalıştırır.
-- Şemayı sıfırlamak için: docker compose down -v && docker compose up -d

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE project_status   AS ENUM ('draft','parsing','producing','paused','killed','completed');
CREATE TYPE scene_status     AS ENUM ('pending','parsed','probing','generating','correcting','refined',
                                      'needs_review','audio','lipsync','muxing','done','failed','frozen');
CREATE TYPE engine_kind      AS ENUM ('fal_wan','seedance','higgsfield');
CREATE TYPE variation_tier   AS ENUM ('probe','full');
CREATE TYPE variation_status AS ENUM ('queued','running','generated','passed','rejected','purged','refined','failed');
CREATE TYPE audio_kind       AS ENUM ('dialogue','music','sfx','ambience');
CREATE TYPE emotion_tag      AS ENUM ('neutral','joy','anger','fear','sadness','surprise','disgust','tension');
CREATE TYPE ledger_kind      AS ENUM ('reserve','commit','release');

CREATE TABLE projects (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title            TEXT NOT NULL,
  status           project_status NOT NULL DEFAULT 'draft',
  budget_limit_usd NUMERIC(12,2) NOT NULL CHECK (budget_limit_usd > 0),
  spent_usd        NUMERIC(12,4) NOT NULL DEFAULT 0,
  reserved_usd     NUMERIC(12,4) NOT NULL DEFAULT 0,
  settings         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE characters (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id         UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name               TEXT NOT NULL,
  description        TEXT,
  lora_url           TEXT,
  lora_trigger       TEXT,
  lora_scale         REAL NOT NULL DEFAULT 1.0,
  reference_images   JSONB NOT NULL DEFAULT '[]'::jsonb,
  voice_id           TEXT,
  pinecone_namespace TEXT NOT NULL,
  embedding_version  INT  NOT NULL DEFAULT 1,
  style_constraints  JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);

CREATE TABLE scenes (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id        UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_code        TEXT NOT NULL,
  seq_order         INT  NOT NULL,
  script_text       TEXT NOT NULL DEFAULT '',
  description       TEXT,
  emotion           emotion_tag NOT NULL DEFAULT 'neutral',
  camera            JSONB NOT NULL DEFAULT '{}'::jsonb,
  action_complexity REAL NOT NULL DEFAULT 0 CHECK (action_complexity BETWEEN 0 AND 1),
  engine            engine_kind,
  duration_s        REAL NOT NULL DEFAULT 5,
  prompt_spec       JSONB NOT NULL DEFAULT '{}'::jsonb,
  prompt_revision   INT  NOT NULL DEFAULT 0,
  correction_count  INT  NOT NULL DEFAULT 0,
  status            scene_status NOT NULL DEFAULT 'pending',
  visual_hash       TEXT,
  dialogue_hash     TEXT,
  bed_hash          TEXT,
  mux_hash          TEXT,
  lipsync_video_key TEXT,
  final_video_key   TEXT,
  spent_usd         NUMERIC(10,4) NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, scene_code)
);
CREATE INDEX scenes_order_idx  ON scenes (project_id, seq_order);
CREATE INDEX scenes_status_idx ON scenes (project_id, status);

CREATE TABLE scene_characters (
  scene_id        UUID NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  character_id    UUID NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
  role            TEXT NOT NULL DEFAULT 'primary',
  screen_position TEXT,
  dialogue        JSONB NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (scene_id, character_id)
);

CREATE TABLE variations (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scene_id            UUID NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  tier                variation_tier NOT NULL,
  wave_no             INT  NOT NULL DEFAULT 0,
  engine              engine_kind NOT NULL,
  seed                BIGINT NOT NULL,
  prompt_revision     INT  NOT NULL,
  params              JSONB NOT NULL DEFAULT '{}'::jsonb,
  external_request_id TEXT,
  keyframe_key        TEXT,
  video_key           TEXT,
  status              variation_status NOT NULL DEFAULT 'queued',
  clip_score          REAL,
  face_score          REAL,
  composite_score     REAL,
  anomalies           JSONB NOT NULL DEFAULT '[]'::jsonb,
  refined_rank        SMALLINT CHECK (refined_rank BETWEEN 1 AND 3),
  cost_usd            NUMERIC(10,4) NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX variations_scene_status_idx ON variations (scene_id, status);
CREATE UNIQUE INDEX variations_rank_uq ON variations (scene_id, refined_rank) WHERE refined_rank IS NOT NULL;
CREATE UNIQUE INDEX variations_ext_uq  ON variations (external_request_id) WHERE external_request_id IS NOT NULL;

CREATE TABLE audio_tracks (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scene_id       UUID NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  character_id   UUID REFERENCES characters(id) ON DELETE SET NULL,
  kind           audio_kind NOT NULL,
  source         TEXT NOT NULL,
  text           TEXT,
  emotion        emotion_tag,
  params         JSONB NOT NULL DEFAULT '{}'::jsonb,
  content_hash   TEXT NOT NULL,
  file_key       TEXT,
  duration_s     REAL,
  start_offset_s REAL NOT NULL DEFAULT 0,
  gain_db        REAL NOT NULL DEFAULT 0,
  is_active      BOOLEAN NOT NULL DEFAULT true,
  version        INT NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audio_active_idx ON audio_tracks (scene_id, kind) WHERE is_active;

CREATE TABLE cost_ledger (
  id           BIGSERIAL PRIMARY KEY,
  project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id     UUID REFERENCES scenes(id) ON DELETE SET NULL,
  variation_id UUID REFERENCES variations(id) ON DELETE SET NULL,
  provider     TEXT NOT NULL,
  kind         ledger_kind NOT NULL,
  amount_usd   NUMERIC(10,4) NOT NULL,
  reservation  UUID NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_project_time_idx ON cost_ledger (project_id, created_at DESC);
CREATE INDEX ledger_reservation_idx  ON cost_ledger (reservation);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$ LANGUAGE plpgsql;
CREATE TRIGGER projects_touch BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER scenes_touch   BEFORE UPDATE ON scenes   FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
