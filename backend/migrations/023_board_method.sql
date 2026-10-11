-- Onaylı kesme karesinin gerçekte hangi yolla çizildiği (cut_in | angles | wide | anchor): denetçide gösterilir; kırp +
-- iyileştir karesinde video ve son kare istemleri kamerayı döndürmez (kare aynı kameranın yakın kırpımıdır).
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS board_method TEXT;
