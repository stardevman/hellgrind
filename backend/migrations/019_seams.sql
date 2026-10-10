-- Çekim içi "yolculuk" düzeltmesi:
--   transition_hint: senaryodan zorlanan geçiş ("CUT TO:" -> cut, "CONTINUOUS" -> continuous); otomatik sınıflama
--   (aynı mekân + aynı kadro + aynı kamera ölçeği -> continuous, değilse cut) bunun yoksa uygulanır.
--   end_board_*: çekim sonu karesi (Kn_end): Kn'den Qwen-Image-Edit Lightning ile, çekimin eylemi tamamlanmış hâli. Sonraki
--   çekim continuous değilse videonun son karesi (FL2VA last_frame) budur.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS transition_hint TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS end_board_key TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS end_board_sha TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS end_board_parent TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS end_board_score DOUBLE PRECISION;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS end_board_note TEXT;
--   transition_reason: otomatik sınıflamanın gerekçesi (panelde: "kadro aynı: Mila+Bolt", "kadro değişti: ...").
--   scene_characters.role = 'chain': zincir karesinden gelen karakter (önceki çekimde vardı, çıktığı yazılmadı).
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS transition_reason TEXT;
