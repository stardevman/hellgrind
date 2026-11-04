-- Anlatım (dış seslendirme) film/sekans kanalı olarak yüklenebilir: zaman çizelgesi kanallarında 'dialogue' türü
-- yalnızca yüklenen dosya için serbest (prosedürel diyalog yoktur). Anlatım filmde müziği sidechain ile bastırır ve
-- teslim paketinde diyalog stem'ine girer (M&E'ye girmez).
ALTER TABLE timeline_tracks DROP CONSTRAINT IF EXISTS timeline_tracks_kind_check;
ALTER TABLE timeline_tracks ADD CONSTRAINT timeline_tracks_kind_check CHECK (kind <> 'dialogue' OR source = 'upload');
