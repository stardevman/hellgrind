-- Sahneler arası süreklilik (anahtar kare zinciri):
--   transition_auto: senaryodan (aynı sahne başlığı altında art arda: continuous; "CUT TO:" sonrası: cut; yeni başlık:
--   new_scene); transition_in: yönetmenin seçimi (boş: otomatik).
--   board_sha: çekimin ilk karesinin (yazısız) SHA-256'sı; board_parent: kare zincirle çizildiyse önceki çekimin karesinin
--   board_sha'sı. Önceki kare değişince bu kare "eski" sayılır.
-- assets.invariants: karakter tasarımının değişmezleri (her kare ve video istemine eklenir).
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS transition_auto TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS transition_in TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS board_sha TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS board_parent TEXT;
ALTER TABLE assets ADD COLUMN IF NOT EXISTS invariants JSONB NOT NULL DEFAULT '[]'::jsonb;
