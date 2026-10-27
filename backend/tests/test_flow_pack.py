"""Google Flow köprüsü: çekim başına elle üretim paketi (Flow'un API'si yok; klipler dış çekim olarak döner).

Kabul:
  - Paket hiçbir ücretli servisi çağırmaz ve bütçeye hiçbir şey yazmaz.
  - Her çekim için istem, Flow modu, Flow süresi ve kredi tahmini çıkar; zip açılabilir.
  - Flow'dan dönen klip dış çekim olarak yüklenince çekim biter ve bir sonraki pakete girmez.
"""
import io
import zipfile

import pytest
from sqlalchemy import select, text

from app.db import session_scope
from app.engine import flow_pack
from app.hermes import rules
from app.models import Scene
from tests.media_utils import make_video

SMALL = dict(probe_n=2, full_n=2, wave_size=2, refined_k=2, max_correction_rounds=1, max_spend_per_10min=10_000)


# ---------- birim (veritabanı gerekmez) ----------

@pytest.mark.parametrize("duration, quality, seconds, noted", [
    (3.0, "fast", 4, False), (4.0, "fast", 4, False), (5.2, "lite", 6, False), (7.9, "fast", 8, False),
    (9.5, "fast", 8, True), (3.0, "quality", 8, False), (None, "fast", 6, False),
])
def test_flow_length_is_the_shortest_option_that_covers_the_shot(duration, quality, seconds, noted):
    sec, note = flow_pack.flow_seconds(duration, quality)
    assert sec == seconds and bool(note) == noted


@pytest.mark.parametrize("name, code", [
    ("S001_SH02.mp4", "S001_SH02"), ("s002_sh01_flow2.mp4", "S002_SH01"), ("Flow - S010_SH12 (1).mp4", "S010_SH12"),
    ("kovalama.mp4", None),
])
def test_clips_are_matched_to_shots_by_the_code_in_their_name(name, code):
    assert flow_pack.code_of(name) == code


# ---------- uçtan uca (veritabanı ve nesne deposu) ----------

@pytest.fixture
def sample(api, api_project, fast, eager):
    def make(**cfg):
        pid = api_project(1000.0, **{**SMALL, **cfg})
        assert api.post(f"/api/projects/{pid}/script", json={"text": rules.SAMPLE_SCRIPT, "auto_render": False}).status_code == 200
        with session_scope() as s:
            ids = {sc.scene_code: str(sc.id) for sc in s.scalars(select(Scene).where(Scene.project_id == pid))}
        return pid, ids
    return make


def test_the_flow_pack_lists_every_open_shot_without_spending(api, sample, s3):
    pid, ids = sample()
    r = api.get(f"/api/projects/{pid}/flow?quality=fast")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["code"] for x in body["shots"]] == sorted(ids)
    assert body["per_clip"] == 20 and body["credits"] == 20 * len(ids)
    for shot in body["shots"]:
        assert shot["prompt"] and shot["mode"] in ("frames", "ingredients", "text") and shot["seconds"] in (4, 6, 8)
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM cost_ledger WHERE project_id = :p"), {"p": pid}).scalar() == 0

    z = api.get(f"/api/projects/{pid}/flow.zip?quality=lite")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "BENIOKU.txt" in names and "plan.csv" in names
    assert all(f"{code}/istem.txt" in names for code in ids)
    readme = zipfile.ZipFile(io.BytesIO(z.content)).read("BENIOKU.txt").decode()
    assert f"{len(ids)} çekim x 10 = {10 * len(ids)} kredi" in readme


def test_a_clip_back_from_flow_closes_its_shot(api, sample, s3, tmp_path):
    pid, ids = sample(lint_strictness="block", require_board_approval=True)
    clip = make_video(tmp_path / "S002_SH01_flow.mp4", seconds=3.0, size="320x180")
    sid = ids[flow_pack.code_of(str(clip))]
    r = api.post(f"/api/scenes/{sid}/external?filename=S002_SH01_flow.mp4", content=open(clip, "rb").read(),
                 headers={"content-type": "video/mp4"})
    assert r.status_code == 200, r.text
    codes = [x["code"] for x in api.get(f"/api/projects/{pid}/flow").json()["shots"]]
    assert "S002_SH01" not in codes and len(codes) == len(ids) - 1
    assert "S002_SH01" in [x["code"] for x in api.get(f"/api/projects/{pid}/flow?scope=all").json()["shots"]]
