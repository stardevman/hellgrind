-- Yönetmenin kare notu ("Değişiklik iste"): istemlere "Director note: …" olarak girer. Görsel özete girmez: yönetmen
-- notlu bir kareyi yeniden çizdirmeden onaylayabilmeli; değişiklik istenen kareyi "Kareleri hazırla" yeni seed'lerle
-- yeniden çizer ve yeniden çizilen kare ona bağlı klipleri zaten eskitir.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS director_note TEXT;
