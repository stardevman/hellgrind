-- MiniMax H3 çıktı güvencesi: makinede üretilen her çıktının (video, storyboard karesi) yerel depoya indirilip
-- doğrulandığının kaydı. Eski makine yalnızca kendi kayıtlarının hepsi 'stored' (ya da üretilmemiş: 'failed', yönetmenin
-- kabulüyle 'lost') iken silinir.
CREATE TABLE IF NOT EXISTS h3_outputs (
  id           BIGSERIAL PRIMARY KEY,
  instance_id  TEXT,                       -- vast.ai makine kimliği (yönetim kapalıysa boş)
  server       TEXT NOT NULL,              -- sunucu adresi (iş bu adresten indirilir)
  backend      TEXT NOT NULL,              -- comfyui | sglang
  job_id       TEXT NOT NULL,              -- sunucunun iş kimliği (ComfyUI prompt_id, SGLang video id)
  kind         TEXT NOT NULL,              -- video | keyframe
  task         TEXT,
  project_id   UUID REFERENCES projects(id) ON DELETE SET NULL,
  status       TEXT NOT NULL DEFAULT 'submitted'
               CHECK (status IN ('submitted', 'completed', 'stored', 'failed', 'lost')),
  remote       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- sunucudaki çıktı ve iş meta verisi (ComfyUI dosya adı, GPU süresi)
  size_bytes   BIGINT,
  sha256       TEXT,
  storage_key  TEXT,                       -- yerel depodaki doğrulanmış kopya
  meta_key     TEXT,                       -- iş meta verisinin (sunucu yanıtı, kaynak bilgisi) doğrulanmış kopyası
  error        TEXT,
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  stored_at    TIMESTAMPTZ,
  UNIQUE (server, job_id)
);
CREATE INDEX IF NOT EXISTS h3_outputs_instance_idx ON h3_outputs (instance_id, status);

-- Portal sertifikası sabitleme (TOFU): ilk başarılı bağlantıdaki SHA-256 parmak izi, sonraki bağlantılarda aranır.
CREATE TABLE IF NOT EXISTS h3_tls_pins (
  endpoint   TEXT PRIMARY KEY,             -- host:port
  sha256     TEXT NOT NULL,
  pinned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
