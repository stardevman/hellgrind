-- Yeni sürüm gönderilince kapının kararı verilmemiş eski sürümleri "superseded" olur: eski inceleme bağlantısı yalnızca
-- izlemeye açık kalır, kararı yeni sürümün kapısını ezemez.
ALTER TABLE film_versions DROP CONSTRAINT IF EXISTS film_versions_status_check;
ALTER TABLE film_versions ADD CONSTRAINT film_versions_status_check
    CHECK (status IN ('sent', 'approved', 'changes_requested', 'superseded'));
