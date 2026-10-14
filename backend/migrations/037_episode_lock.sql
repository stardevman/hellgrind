-- Teslim edilmiş bölüm "sürüm sabit": Kabul kapısı onaylanınca bölüm o film sürümüne ve o anki dizi varlığı sürümlerine
-- sabitlenir. Kilitli bölümde üretim, stil etkinleştirme ve bölüme özel varlık değişikliği 423 döner; kilidi yönetici açar.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS locked_at timestamptz;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS locked_version_id uuid REFERENCES film_versions(id) ON DELETE SET NULL;
