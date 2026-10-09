-- Kimlik kapısı: çekimde karakterin yüzü görünmeli mi. NULL: otomatik (karakter varsa ve kamera/tarif yüzü gizlemiyorsa
-- evet); true/false: yönetmenin seçimi. Yüz beklenen çekimde yüzü görünmeyen ya da kimlik eşiğinin (proje ayarı
-- identity_threshold) altında kalan aday rafine havuza giremez.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS face_expected BOOLEAN;
