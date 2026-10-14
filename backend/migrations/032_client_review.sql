-- Müşteri onay döngüsü: onay kapıları, müşteriye gönderilen film sürümleri (v001, v002…), süreli salt okunur inceleme
-- bağlantıları ve zaman kodlu müşteri notları.
-- client_gates: kapı başına durum (pending | approved | changes_requested) ve revizyon turu sayısı (değişiklik istekleri).
--   Kapılar: script (Senaryo), design (Tasarım: varlıklar), animatic (Animatik), draft (Taslak kurgu), final, acceptance (Kabul).
-- film_versions: müşteriye gönderilen her birleştirilmiş film. Video anahtarı içerik-adreslidir; sürüm gönderildiği anki
--   filmi gösterir (sonradan birleştirilen film onu değiştirmez). spent_usd: gönderimdeki proje harcaması (tur maliyeti).
-- review_links: belirteçli, süreli, salt okunur bağlantı (/panel/review?token=…); bütçe, motor ve maliyet göstermez.
-- client_notes: müşterinin zaman kodlu notu; filmdeki zamandan çekime bağlanır; panelde çözüldü / çözülmedi.
CREATE TABLE IF NOT EXISTS client_gates (
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    gate        text NOT NULL CHECK (gate IN ('script', 'design', 'animatic', 'draft', 'final', 'acceptance')),
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'changes_requested')),
    rounds      integer NOT NULL DEFAULT 0,
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, gate)
);

CREATE TABLE IF NOT EXISTS film_versions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number      integer NOT NULL,
    label       text NOT NULL,
    gate        text NOT NULL,
    kind        text NOT NULL,                      -- animatic | draft | final
    video_key   text NOT NULL,
    cut         jsonb,
    spent_usd   numeric(12, 4) NOT NULL DEFAULT 0,
    status      text NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'approved', 'changes_requested')),
    decided_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, number)
);

CREATE TABLE IF NOT EXISTS review_links (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_id  uuid NOT NULL REFERENCES film_versions(id) ON DELETE CASCADE,
    token       text NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client_notes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_id  uuid REFERENCES film_versions(id) ON DELETE CASCADE,
    gate        text NOT NULL,
    at_s        real,
    shot_offset_s real,                              -- notun çekim içindeki yeri (Kurgu zaman çizelgesinde işaretçi)
    scene_id    uuid REFERENCES scenes(id) ON DELETE SET NULL,
    scene_code  text,
    text        text NOT NULL,
    resolved    boolean NOT NULL DEFAULT false,
    resolved_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS client_notes_project ON client_notes (project_id, created_at);
