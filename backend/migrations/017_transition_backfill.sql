-- Eski çekimlerin geçişi (016'dan önce ayrıştırılmış senaryolar): sahne başlığı (script_text'in ilk satırı) bir önceki
-- çekimle aynıysa continuous, değilse (ya da sekansın ilk çekimiyse) new_scene. "CUT TO:" bilgisi eski kayıtlarda yok;
-- senaryo yeniden işlenince yazılır. Yönetmenin seçimi (transition_in) değişmez.
WITH h AS (
  SELECT id,
         split_part(coalesce(script_text, ''), E'\n', 1) AS heading,
         lag(split_part(coalesce(script_text, ''), E'\n', 1))
           OVER (PARTITION BY project_id, sequence_id ORDER BY seq_order, scene_code) AS previous
  FROM scenes
)
UPDATE scenes s
SET transition_auto = CASE WHEN h.previous IS NULL OR h.previous <> h.heading THEN 'new_scene' ELSE 'continuous' END
FROM h
WHERE h.id = s.id AND s.transition_auto IS NULL;
