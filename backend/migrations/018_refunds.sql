-- İade: işlenmiş (commit) bir harcamanın geri alınması (ör. RunPod'un faturalamadığı "hayalet" boşta bekleme kayıtları).
-- Defterde ayrı bir satır (kind = refund) olarak durur; servis geçmişinde çağrı "refunded" görünür.
ALTER TYPE ledger_kind ADD VALUE IF NOT EXISTS 'refund';
ALTER TABLE service_calls DROP CONSTRAINT IF EXISTS service_calls_status_check;
ALTER TABLE service_calls ADD CONSTRAINT service_calls_status_check
  CHECK (status IN ('running', 'succeeded', 'failed', 'refunded'));
