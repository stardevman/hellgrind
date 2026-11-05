-- Ücretli ek işlem (dudak senkronu, görsel, seslendirme, LLM, upscale) yönetmenin iznini bekliyor: çekim bu durumda
-- durur, izin verilince yalnızca eksik aşamalarıyla sürer (app/budget/consent.py).
ALTER TYPE scene_status ADD VALUE IF NOT EXISTS 'awaiting_consent';
