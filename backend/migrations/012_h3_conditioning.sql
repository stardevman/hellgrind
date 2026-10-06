-- MiniMax H3 koşullandırma: kompozisyon seed'le değil koşulla kontrol edilir.
-- h3_mode: NULL ya da 'fl2va' = storyboard karesi (ya da yüklenen ilk kare) FL2VA'ya ilk kare olarak verilir;
--          'ref2va' = karakter referans seti (ve varsa ilk kare) Ref2VA'ya verilir. Yalnızca 'ref2va' görsel özete girer.
-- keyframe_key/hash: yönetmenin yüklediği ilk kare (storyboard karesinin yerine geçer; görsel özete girer).
-- board_score: storyboard karesinin kalite kapısı skoru (ücretli modda; ücretsiz modda boş).
-- board_source: karenin kaynağı: local (prosedürel) | flux (fal.ai) | comfyui (H3 makinesindeki görsel iş akışı) | upload.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS h3_mode TEXT CHECK (h3_mode IN ('fl2va', 'ref2va'));
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS keyframe_key TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS keyframe_hash TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS board_score REAL;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS board_source TEXT;
