-- Kesme kamerası, karakter girişi ve otomatik renk eşleştirme.
--   camera_plan: kesmede otomatik atanan kamera (30° kuralı: önceki çekimden en az bir ölçek adımı ya da >= 30° açı farkı);
--     boşsa senaryodaki kamera. Senaryo kamerası (camera) sınıflamada kullanılır, kare ve video istemi etkin kamerayı alır.
--   entry_mode: önceki çekimde olmayan karakter aynı mekâna giriyorsa: cut (varsayılan, belirgin kamera değişikliğiyle
--     kesme) | walk_in (devam çekimi ve açık giriş eylemi).
--   grade_auto: otomatik çekim eşleştirmesi {applied, previous, disabled, reference, delta_e}.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS camera_plan JSONB;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS entry_mode TEXT;
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS grade_auto JSONB;
