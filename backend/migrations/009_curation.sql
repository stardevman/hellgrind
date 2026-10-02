-- Faz 17: kürasyon kayıtları
-- Yönetmenin her kararı (seçim, eleme, iyileştirme işareti, onay...) kaydedilir: ileride projeye özel kalite modelinin
-- eğitim verisidir (değişmez kural 7).

CREATE TABLE curation_events (
  id           BIGSERIAL PRIMARY KEY,
  project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id     UUID REFERENCES scenes(id) ON DELETE SET NULL,
  variation_id UUID REFERENCES variations(id) ON DELETE SET NULL,
  action       TEXT NOT NULL,
  payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX curation_project_time_idx ON curation_events (project_id, created_at);
CREATE INDEX curation_action_idx ON curation_events (project_id, action);
