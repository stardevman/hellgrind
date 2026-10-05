-- Ücretli servis geçmişi: her ücretli (ücretsiz modda sanal) çağrı için tek satır. Hangi servise, hangi modelle, ne için
-- ve ne kadar harcandığı; sağlayıcının istek kimliği, süre ve hata. Bütçe defterine (cost_ledger) rezervasyon kimliğiyle
-- bağlıdır ve onunla aynı işlemde yazılır: reserve satırı açar, commit ya da release kapatır.
-- Proje silinse de satır kalır (gerçek para harcanmıştır): bağlar boşalır, proje başlığı ve çekim kodu satırda saklanır.

CREATE TABLE service_calls (
  id            BIGSERIAL PRIMARY KEY,
  reservation   UUID NOT NULL UNIQUE,
  project_id    UUID REFERENCES projects(id) ON DELETE SET NULL,
  sequence_id   UUID REFERENCES sequences(id) ON DELETE SET NULL,
  scene_id      UUID REFERENCES scenes(id) ON DELETE SET NULL,
  variation_id  UUID REFERENCES variations(id) ON DELETE SET NULL,
  asset_id      UUID REFERENCES assets(id) ON DELETE SET NULL,
  project_title TEXT,
  subject       TEXT,                     -- kayıt anındaki çekim kodu ya da varlık adı
  service       TEXT NOT NULL,            -- fal | elevenlabs | anthropic
  operation     TEXT NOT NULL,            -- video, first_frame, storyboard, reference, motion_test, previs, revision, lipsync, tts, script_parse, other
  label         TEXT NOT NULL,            -- defterdeki etiket (cost_ledger.provider)
  model         TEXT,                     -- çağrılan model ya da uç nokta; birden çok adımda " + " ile
  request_id    TEXT,                     -- sağlayıcının istek kimliği (son adım); bütün adımlar details.steps içinde
  dry_run       BOOLEAN,                  -- true: sanal (ücretsiz mod), false: gerçek harcama, NULL: bilinmiyor
  status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
  estimate_usd  NUMERIC(10,4) NOT NULL,   -- rezerve edilen tahmin
  amount_usd    NUMERIC(10,4),            -- işlenen tutar: başarısızda 0, sürerken boş
  error         TEXT,
  details       JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ
);
CREATE INDEX service_calls_time_idx    ON service_calls (started_at DESC);
CREATE INDEX service_calls_project_idx ON service_calls (project_id, started_at DESC);
CREATE INDEX service_calls_scene_idx   ON service_calls (scene_id);

-- Geçmiş sisteminden önceki defter kayıtları. Mod yalnızca kesin olduğunda yazılır: yerel sağlayıcının istek kimliği
-- sanal, ilk kare (önce görsel) yalnızca ücretli modda olur. Kalanların modu ve modeli API açılışında ayar değişiklik
-- kaydından tamamlanır (app.budget.history.complete_backfill).
INSERT INTO service_calls (reservation, project_id, sequence_id, scene_id, variation_id, asset_id, project_title, subject,
                           service, operation, label, request_id, dry_run, status, estimate_usd, amount_usd, details,
                           started_at, finished_at)
SELECT r.reservation, r.project_id, r.sequence_id, r.scene_id, r.variation_id, v.asset_id, p.title,
       COALESCE(sc.scene_code, a.name),
       CASE split_part(r.provider, ':', 1) WHEN 'elevenlabs' THEN 'elevenlabs' WHEN 'anthropic' THEN 'anthropic' ELSE 'fal' END,
       CASE
         WHEN r.provider = 'elevenlabs' THEN 'tts'
         WHEN r.provider = 'anthropic' THEN 'script_parse'
         WHEN r.provider ~ ':lipsync$' THEN 'lipsync'
         WHEN r.provider ~ ':first_frame' THEN 'first_frame'
         WHEN r.provider ~ ':storyboard$' THEN 'storyboard'
         WHEN r.provider ~ ':reference$' THEN 'reference'
         WHEN r.provider ~ ':motion_test$' THEN 'motion_test'
         WHEN r.provider ~ ':previs$' THEN 'previs'
         WHEN r.provider ~ ':revision$' THEN 'revision'
         WHEN split_part(r.provider, ':', 1) IN ('fal_wan', 'seedance', 'higgsfield') THEN 'video'
         ELSE 'other'
       END,
       r.provider,
       CASE WHEN e.kind = 'commit' THEN
         CASE WHEN r.provider ~ ':first_frame' THEN v.params->>'first_frame_request_id'
              WHEN NOT starts_with(v.external_request_id, 'local-') THEN v.external_request_id END
       END,
       CASE WHEN starts_with(v.external_request_id, 'local-') THEN true
            WHEN r.provider ~ ':first_frame' THEN false
            WHEN v.external_request_id IS NOT NULL THEN false
       END,
       CASE e.kind WHEN 'commit' THEN 'succeeded' WHEN 'release' THEN 'failed' ELSE 'running' END,
       r.amount_usd,
       CASE e.kind WHEN 'commit' THEN e.amount_usd WHEN 'release' THEN 0 END,
       '{"backfilled": true}'::jsonb,
       r.created_at, e.created_at
FROM cost_ledger r
LEFT JOIN LATERAL (SELECT x.kind, x.amount_usd, x.created_at FROM cost_ledger x
                   WHERE x.reservation = r.reservation AND x.kind IN ('commit', 'release')
                   ORDER BY x.id LIMIT 1) e ON true
LEFT JOIN projects p ON p.id = r.project_id
LEFT JOIN scenes sc ON sc.id = r.scene_id
LEFT JOIN variations v ON v.id = r.variation_id
LEFT JOIN assets a ON a.id = v.asset_id
WHERE r.kind = 'reserve'
ORDER BY r.id                                        -- kimlik sırası zaman sırası olsun
ON CONFLICT (reservation) DO NOTHING;
