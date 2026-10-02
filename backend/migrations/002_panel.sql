-- Kademe 1b: panel ve zaman çizelgesi
ALTER TABLE projects ADD COLUMN IF NOT EXISTS timeline_key TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS script_source TEXT;
