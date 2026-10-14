-- Teslim şartnamesi, QC raporu, teslim paketi ve hak zinciri.
-- projects.delivery: teslim şartnamesi {resolution, fps, codec, loudness_lufs, srt, me_mix, textless, preset}; boşsa
--   film eski sabitlerle birleştirilir (1280x720 ya da upscale edilmişse 1920x1080, 24 fps).
-- projects.qc: son QC raporu (filmin anahtarına bağlı; film değişince eskir).
-- projects.delivery_key: son teslim paketinin (zip) anahtarı.
-- rights_items: hak zinciri belgeleri (senaryo hak devri, ses oyuncusu sözleşmesi/rızası, müzik lisansı, model kullanım
--   koşulları); yapay zekâ kullanım beyanı servis geçmişinden otomatik üretilir, burada satırı yoktur.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS delivery jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS qc jsonb;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS delivery_key text;

CREATE TABLE IF NOT EXISTS rights_items (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('script_rights', 'voice_consent', 'music_license', 'model_terms')),
    filename    text NOT NULL,
    file_key    text NOT NULL,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rights_items_project ON rights_items (project_id, kind);
