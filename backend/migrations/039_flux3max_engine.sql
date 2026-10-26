-- fal.ai'de FLUX 3 Max image-to-video (Black Forest Labs): yalnızca başlangıç karesiyle (image_url zorunlu, referans
-- görseli almaz). Varsayılan değildir; projenin draft_engine ayarıyla seçilir. Mevcut çekimlere dokunulmaz.
ALTER TYPE engine_kind ADD VALUE IF NOT EXISTS 'flux3max';
