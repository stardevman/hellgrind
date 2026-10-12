-- Sahne istemi yazıcı (LLM) önbelleği: girdi özeti değişmedikçe istem yeniden yazılmaz (ücretli çağrı tekrar etmez).
-- prompt NULL ise yazım doğrulamadan geçmedi (errors); plan kurallar istemine düşer, yönetmen yeniden yazdırabilir.
CREATE TABLE IF NOT EXISTS prompt_cache (
    input_hash    text PRIMARY KEY,
    project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,
    engine        text NOT NULL,
    model         text NOT NULL,
    prompt        text,
    errors        jsonb NOT NULL DEFAULT '[]',
    attempts      integer NOT NULL DEFAULT 1,
    input_tokens  integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    usd           numeric(12, 6) NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);
