-- Yönetmenin filmden çıkardığı çekim: videosu ve kayıtları durur, montaj ve dışa aktarma onu atlar. Varsayılan dahil.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS in_cut boolean NOT NULL DEFAULT true;
