"""Zaman çizelgesi: tamamlanan sahneleri sırayla tek bir filme birleştirir (ücretsiz, FFmpeg)."""
import json
import os
import subprocess
import uuid

from sqlalchemy import select

from app import storage
from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import Project, Scene
from app.quality.anomaly import probe_duration

W, H, FPS = 1280, 720, 24


def _has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return bool(out)


def normalize(src: str, dst: str) -> str:
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={FPS},format=yuv420p")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if _has_audio(src):
        cmd += ["-vf", vf, "-map", "0:v", "-map", "0:a"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf", vf, "-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-b:a", "192k", dst]
    subprocess.run(cmd, check=True)
    return dst


def concat(paths: list[str], out: str) -> str:
    lst = out + ".txt"
    with open(lst, "w") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", "-movflags", "+faststart", out], check=True)
    os.remove(lst)
    return out


def assemble(project_id: str) -> dict:
    with session_scope() as s:
        # "Tamamlanan" = done: yeniden üretilen ya da başarısız bir sahnenin eski videosu filme girmez
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "done",
                                               Scene.final_video_key.is_not(None)).order_by(Scene.seq_order)).all()
        items = [(sc.scene_code, sc.final_video_key, sc.description) for sc in scenes]
    if not items:
        return {"error": "Henüz tamamlanmış sahne yok"}
    h = stable_hash(items)          # kod + video + açıklama: kesme listesinin içeriği anahtarla her zaman tutarlı
    key, cut_key = f"timeline/{project_id}/{h}.mp4", f"timeline/{project_id}/{h}.json"

    if not (storage.exists(key) and storage.exists(cut_key)):      # yarım kalmış yüklemeyi de tamamlar
        work = os.path.join(settings.work_dir, f"tl-{uuid.uuid4()}")
        os.makedirs(work, exist_ok=True)
        norm, cut, t = [], [], 0.0
        try:
            for i, (code, k, desc) in enumerate(items):
                src = storage.download(k, os.path.join(work, f"src{i}.mp4"))
                n = normalize(src, os.path.join(work, f"n{i:04d}.mp4"))
                d = probe_duration(n)
                cut.append({"scene_code": code, "in_s": round(t, 3), "out_s": round(t + d, 3), "source": k,
                            "description": desc})
                t += d
                norm.append(n)
            out = concat(norm, os.path.join(work, "film.mp4"))
            storage.put_file(out, key)
            with open(os.path.join(work, "cut.json"), "w") as f:
                json.dump({"duration_s": round(t, 3), "clips": cut}, f, ensure_ascii=False, indent=2)
            storage.put_file(os.path.join(work, "cut.json"), cut_key, "application/json")
        finally:
            for fn in os.listdir(work):
                os.remove(os.path.join(work, fn))
            os.rmdir(work)

    with session_scope() as s:
        s.get(Project, project_id).timeline_key = key
    return {"key": key, "cutlist_key": cut_key, "scenes": len(items)}
