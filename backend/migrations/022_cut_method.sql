-- Kesme karesinin yöntemi (yönetmenin seçimi; boşsa otomatik): cut_in (kırp + iyileştir: ölçek daralıyor, açı <= 15°) |
-- angles (Qwen-Image-Edit Multiple Angles LoRA: açı >= 30°) | wide (geniş plandan çizim: ölçek genişliyor).
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS cut_method TEXT;
