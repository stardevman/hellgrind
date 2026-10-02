-- Faz 14: storyboard onayı ve taslak geçiş (kaba kurgu)
-- Revizyonu en ucuz aşamada yapmak: önce storyboard, sonra yalnızca test turuyla kaba kurgu, sonra yalnızca gereken
-- çekimlerin tam turla iyileştirilmesi.

ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'storyboard';     -- storyboard karesi üretiliyor
ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'board_review';   -- kare yönetmen onayını bekliyor

ALTER TABLE scenes
  ADD COLUMN IF NOT EXISTS board_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (board_status IN ('pending','approved','changes_requested')),
  ADD COLUMN IF NOT EXISTS board_note TEXT,
  ADD COLUMN IF NOT EXISTS board_key TEXT,             -- storyboard/{sahne}/{görsel özet}.jpg
  ADD COLUMN IF NOT EXISTS board_hash TEXT,            -- karenin çizildiği görsel özet; değişince kare yenilenir
  ADD COLUMN IF NOT EXISTS upgrade_requested BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS cut_tier TEXT CHECK (cut_tier IN ('draft','final')),   -- sahnenin videosu hangi geçişten
  ADD COLUMN IF NOT EXISTS draft_video_key TEXT;       -- taslak geçişin montajlı klibi (kaba kurgu bunu kullanır)

ALTER TABLE projects ADD COLUMN IF NOT EXISTS draft_timeline_key TEXT;

-- Mevcut üretimler tam turdan gelmiştir.
UPDATE scenes SET cut_tier = 'final' WHERE final_video_key IS NOT NULL AND cut_tier IS NULL;
