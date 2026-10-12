-- Eski stil işareti: stil sürümü değişince tamamlanmış çekim yeniden üretilmez, yalnızca işaretlenir.
-- {"version": eski sürüm, "visual_hash": çekimin o anki görsel özeti, "fresh": yeni stilde olacak görsel özet}
-- visual_hash sahnenin güncel özetiyle eşleşmiyorsa (çekim yeniden üretildi) işaret geçersizdir.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS style_stale jsonb;
