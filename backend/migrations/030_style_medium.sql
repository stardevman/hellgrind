-- Stil kılavuzunun çizim türü: 2d | 3d. Video istemindeki ortam cümlesi ve varlıkların varsayılan render kuralları
-- buradan gelir (eskiden sabit "same 3D animated style" cümlesiydi). Mevcut sürümler o cümleyle üretildiği için 3d
-- olarak işaretlenir; yeni sürümlerin varsayılanı 2d (animasyon dizisi). Değişiklik yeni bir sürümdür: etki önizlemesi
-- ve onay akışı aynen çalışır.
ALTER TABLE style_bibles ADD COLUMN IF NOT EXISTS medium text NOT NULL DEFAULT '3d';
ALTER TABLE style_bibles ALTER COLUMN medium SET DEFAULT '2d';
DO $$ BEGIN
    ALTER TABLE style_bibles ADD CONSTRAINT style_bibles_medium_check CHECK (medium IN ('2d', '3d'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
