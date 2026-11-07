-- Kukla rehberi: çekimin sahneleme planından çizilen düz renkli rehber video, video modeline hareket referansı olarak
-- gönderilir (H3 Max reference_video_urls, Seedance 2.5 video_urls). Varsayılan kapalı: ek referans token'ı ücretlidir.
ALTER TABLE scenes ADD COLUMN IF NOT EXISTS motion_guide BOOLEAN NOT NULL DEFAULT false;
