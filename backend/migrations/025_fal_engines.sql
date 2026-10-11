-- fal.ai video motorları: MiniMax H3 Max (taslak, varsayılan) ve Seedance 2.5 (final). RunPod'daki "h3" seçilebilir kalır
-- ama varsayılan değildir. Mevcut çekimlerin motoruna dokunulmaz (görsel özetleri değişmez).
ALTER TYPE engine_kind ADD VALUE IF NOT EXISTS 'h3max';
ALTER TYPE engine_kind ADD VALUE IF NOT EXISTS 'seedance25';
-- fal kuyruğundaki iş (submit -> status -> result): panel kapanıp açılsa da bekleyen iş request_id'den sürdürülür.
-- {"request_id", "endpoint", "engine", "mode", "job", "status", "submitted_at", "error"?, "result"?}
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS fal_request JSONB;
-- Çekim bazında fal video ayarları (boşsa Ayarlar'daki varsayılan): {"resolution": "768P", "audio": false}
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS video_opts JSONB;
