"""Zaman çizelgesi: tamamlanan sahneleri sırayla tek bir filme birleştirir (ücretsiz, FFmpeg).

Her klip önce aynı biçime çevrilir (1280x720, 24 fps, yuv420p, aac 48k stereo; sesi yoksa sessiz ses eklenir), sonra
concat demuxer ile yeniden kodlanmadan (-c copy) birleştirilir. Akış parametreleri birebir aynı olduğu için bu güvenlidir.
Çıktılar içerik-adreslidir: aynı sahneler, videolar ve açıklamalar aynı anahtarı üretir ve iş atlanır.
"""
import json
import os
import subprocess
import tempfile

from sqlalchemy import select

from app import storage
from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import Project, Scene
from app.quality.anomaly import probe_duration

WIDTH, HEIGHT, FPS = 1280, 720, 24
SAMPLE_RATE = 48000


def has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    return bool(out)


def video_duration(path: str) -> float:
    """Görüntü akışının süresi; akışta yoksa dosya süresi."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return probe_duration(path)


def normalize(src: str, dst: str) -> str:
    """Klibi ortak biçime çevirir. Ses, görüntü süresine eşitlenir: kısaysa sessizlikle tamamlanır, uzunsa kesilir.
    Eşitleme -t ile yapılır; -shortest kare hassasiyetli değildir (ses görüntüden yarım saniye uzun çıkabilir) ve
    concat -c copy her dosyayı süresi kadar ileri taşıdığından, her klipte ses biraz daha kayardı."""
    video = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
             f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    if has_audio(src):
        cmd += ["-vf", video, "-af", "apad", "-map", "0:v", "-map", "0:a"]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo", "-vf", video, "-map", "0:v", "-map", "1:a"]
    cmd += ["-t", f"{video_duration(src):.4f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", str(SAMPLE_RATE), "-ac", "2", "-b:a", "192k", dst]
    subprocess.run(cmd, check=True)
    return dst


def concat(paths: list[str], out: str) -> str:
    listing = out + ".txt"
    with open(listing, "w") as f:
        for p in paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    try:
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", listing, "-c", "copy", "-movflags", "+faststart", out], check=True)
    finally:
        os.remove(listing)
    return out


def assemble(project_id: str) -> dict:
    """Tamamlanan (done) sahneleri seq_order sırasıyla filme çevirir. Henüz tamamlanan yoksa {"error": ...} döner."""
    with session_scope() as s:
        # "Tamamlanan" = done: yeniden üretilen ya da başarısız bir sahnenin eski videosu filme girmez
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "done",
                                               Scene.final_video_key.is_not(None))
                           .order_by(Scene.seq_order, Scene.scene_code)).all()
        items = [(sc.scene_code, sc.final_video_key, sc.description, sc.emotion) for sc in scenes]
    if not items:
        return {"error": "Henüz tamamlanmış sahne yok"}

    digest = stable_hash([[WIDTH, HEIGHT, FPS], items])        # kesme listesinin içeriği anahtarla her zaman tutarlı
    key, cutlist_key = f"timeline/{project_id}/{digest}.mp4", f"timeline/{project_id}/{digest}.json"
    if not (storage.exists(key) and storage.exists(cutlist_key)):          # yarım kalmış yüklemeyi de tamamlar
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as work:
            clips, normalized, t = [], [], 0.0
            for i, (code, video_key, description, emotion) in enumerate(items):
                src = storage.download(video_key, os.path.join(work, f"src{i}.mp4"))
                path = normalize(src, os.path.join(work, f"n{i:04d}.mp4"))
                duration = probe_duration(path)
                clips.append({"scene_code": code, "in_s": round(t, 3), "out_s": round(t + duration, 3),
                              "description": description, "emotion": emotion, "source": video_key})
                normalized.append(path)
                t += duration
            film = concat(normalized, os.path.join(work, "film.mp4"))
            cutlist = os.path.join(work, "cut.json")
            with open(cutlist, "w") as f:
                json.dump({"duration_s": round(t, 3), "clips": clips}, f, ensure_ascii=False, indent=2)
            storage.put_file(film, key)
            storage.put_file(cutlist, cutlist_key, "application/json")      # kesme listesi, film yüklendikten sonra

    with session_scope() as s:
        s.get(Project, project_id).timeline_key = key
    return {"key": key, "cutlist_key": cutlist_key, "scenes": len(items)}
