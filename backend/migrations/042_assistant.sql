-- Panel asistanı (Claude): sohbet oturumları, mesaj geçmişi ve onay bekleyen eylemler. Mesajların içeriği API'ye
-- gönderilen biçimdedir (yalnızca eklenir, sonradan değiştirilmez: düşünme blokları sohbete bağlıdır). Görseller
-- nesne deposu anahtarıyla saklanır ve istek kurulurken aynı baytlarla açılır.
CREATE TABLE IF NOT EXISTS assistant_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT,
    allow_all   BOOLEAN NOT NULL DEFAULT FALSE,     -- "Bu oturum için tümüne izin ver"
    busy        BOOLEAN NOT NULL DEFAULT FALSE,     -- yanıt yazılıyor
    error       TEXT,
    spent_usd   NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_sessions_project ON assistant_sessions (project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS assistant_messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES assistant_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     JSONB NOT NULL,                     -- API içerik blokları
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb, -- panel için: kullanıcı metni, araç özetleri, belirteçler, bedel
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_messages_session ON assistant_messages (session_id, id);

CREATE TABLE IF NOT EXISTS assistant_actions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id   UUID NOT NULL REFERENCES assistant_sessions(id) ON DELETE CASCADE,
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scene_id     UUID REFERENCES scenes(id) ON DELETE CASCADE,
    tool         TEXT NOT NULL,
    input        JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary      TEXT NOT NULL,
    estimate_usd NUMERIC(12, 4),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'declined', 'done', 'failed')),
    result       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS assistant_actions_session ON assistant_actions (session_id, created_at);
