-- Hareketli animatik: çekimin sahneleme planı (yönetmenin düzenlediği; boşsa kurallardan çıkarılır, post/staging.py).
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS staging JSONB;
