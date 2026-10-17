-- Görsel stüdyosu: yönetmenin elle istediği görseller (metinden ya da referans görsellerden). Üretilen görsel bir
-- varlığa referans olarak atanabilir; böylece varlık görseli için dışarıya çıkılmaz. Ücretsiz modda yerel taslak çizilir
-- ve ücretli karşılığının fiyatı sanal olarak bütçeden düşer.
CREATE TABLE IF NOT EXISTS studio_images (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_id        uuid NOT NULL,
    prompt          text NOT NULL DEFAULT '',
    composed_prompt text NOT NULL DEFAULT '',
    negative        text NOT NULL DEFAULT '',
    mode            text NOT NULL DEFAULT 'text',          -- text: Qwen-Image-2512 | edit: Qwen-Image-Edit-2511
    aspect          text NOT NULL DEFAULT 'square',
    kind            text NOT NULL DEFAULT 'free',          -- free | character | location | prop (varlıksız istemde)
    width           integer NOT NULL,
    height          integer NOT NULL,
    seed            bigint NOT NULL,
    sources         jsonb NOT NULL DEFAULT '[]',           -- [{key, label}] düzenleme kaynakları (image 1..3)
    target_asset_id uuid REFERENCES assets(id) ON DELETE SET NULL,   -- istem bu varlık için kuruldu
    apply_style     boolean NOT NULL DEFAULT true,
    input_hash      text NOT NULL,
    status          text NOT NULL DEFAULT 'queued',        -- queued | running | done | failed
    file_key        text,
    thumb_key       text,
    cost_usd        numeric(10, 4) NOT NULL DEFAULT 0,
    engine          text,                                  -- local | fal_qwen
    error           text,
    favorite        boolean NOT NULL DEFAULT false,
    hidden          boolean NOT NULL DEFAULT false,
    assignments     jsonb NOT NULL DEFAULT '[]',           -- [{asset_id, name, kind, version, at}]
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS studio_images_project ON studio_images (project_id, created_at DESC);
