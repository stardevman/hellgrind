-- MiniMax H3: yönetmenin kendi GPU sunucusunda (ör. vast.ai) çalışan video motoru. Yeni çekimlerin varsayılan motorudur;
-- Seedance (fal.ai) yalnızca yönetmen seçince kullanılır. Mevcut çekimlerin motoruna dokunulmaz (görsel özetleri değişmez).
ALTER TYPE engine_kind ADD VALUE IF NOT EXISTS 'h3';
