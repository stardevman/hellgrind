"""Zaman çizelgesi: tamamlanan sahneleri sırayla tek bir filme birleştirir (ücretsiz, FFmpeg).

Her klip önce aynı biçime çevrilir (1280x720, 24 fps, yuv420p, aac 48k stereo; sesi yoksa sessiz ses eklenir), sonra
concat demuxer ile yeniden kodlanmadan (-c copy) birleştirilir. Akış parametreleri birebir aynı olduğu için bu güvenlidir.
Çıktılar içerik-adreslidir: aynı sahneler, videolar ve açıklamalar aynı anahtarı üretir ve iş atlanır.

Film düzeyinde ses (Faz 11): sahne klipleri yalnızca kendi sesleriyle (diyalog ve varsa sahneye özel efekt) birleştirilir;
sonra zaman çizelgesi kanalları (sekans müziği, yüklenen stem'ler) sekans aralıklarına yerleştirilir, diyalog müziği
sidechain ile bastırır ve en sonda loudnorm uygulanır. Müzik çekimler arasında kesilmeden akar. Kanalların içerik özetleri
ve zamanlamaları film anahtarına girer: yalnızca müzik değişince çekimler değil, yalnızca film yeniden birleştirilir.
"""
import json
import os
import subprocess
import tempfile

from sqlalchemy import select

from app import storage
from app.config import project_settings, settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import Project, Scene, Sequence, TimelineTrack
from app.post.mux import AudioInput, mux
from app.quality.anomaly import probe_duration

MIN_TRACK_S = 0.05

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


def track_spec(t: TimelineTrack, sequence_code: str | None) -> list:
    """Kanalın filmi belirleyen her şeyi (anahtar özetine girer)."""
    return [sequence_code, t.kind, t.source, t.content_hash, round(t.start_s, 3),
            None if t.end_s is None else round(t.end_s, 3), round(t.gain_db, 3), round(t.fade_in_s, 3),
            round(t.fade_out_s, 3)]


def place_tracks(tracks: list[dict], spans: dict, total: float) -> list[dict]:
    """Kanalları film süresine yerleştirir: zamanlar sekansın başına göredir (sekanssızsa filmin), bitiş sekansın sonunu
    aşamaz. Filme girmeyen sekansın kanalı atlanır. [{**kanal, "at": mutlak başlangıç, "length": süre}], filmdeki
    başlangıca göre sıralı (aynı anda başlayanlarda önce film düzeyindeki, sonra sekans kodu)."""
    placed = []
    for t in tracks:
        span = spans.get(t["sequence"]) if t["sequence"] else (0.0, total)
        if span is None:
            continue
        start = span[0] + t["start_s"]
        end = span[1] if t["end_s"] is None else min(span[1], span[0] + t["end_s"])
        if end - start >= MIN_TRACK_S:
            placed.append({**t, "at": round(start, 3), "length": round(end - start, 3)})
    return sorted(placed, key=lambda p: (p["at"], p["sequence"] or "", p["id"]))


def _track_file(t: dict, work: str) -> str:
    ext = os.path.splitext(t["file_key"])[1] if t["source"] == "upload" else ".wav"
    path = os.path.join(work, f"track_{t['id']}{ext}")
    if t["source"] == "procedural":
        from app.post.pipeline import music_file          # pipeline -> timeline döngüsünü önlemek için geç import
        music_file(t["params"].get("mood", "neutral"), t["length"], path)
        return path
    return storage.download(t["file_key"], path)


def assemble(project_id: str) -> dict:
    """Tamamlanan (done) sahneleri seq_order sırasıyla filme çevirir ve film düzeyindeki kanalları ekler. Henüz tamamlanan
    yoksa {"error": ...} döner."""
    with session_scope() as s:
        # "Tamamlanan" = done: yeniden üretilen ya da başarısız bir sahnenin eski videosu filme girmez
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id, Scene.status == "done",
                                               Scene.final_video_key.is_not(None))
                           .order_by(Scene.seq_order, Scene.scene_code)).all()
        sequences = {q.id: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
        items = [(sc.scene_code, sc.final_video_key, sc.description, sc.emotion,
                  sequences[sc.sequence_id].code if sc.sequence_id in sequences else None) for sc in scenes]
        titles = {q.code: q.title for q in sequences.values()}
        rows = s.scalars(select(TimelineTrack).where(TimelineTrack.project_id == project_id, TimelineTrack.is_active)
                         .order_by(TimelineTrack.created_at, TimelineTrack.id)).all()
        tracks = [{"id": str(t.id), "sequence": sequences[t.sequence_id].code if t.sequence_id else None,
                   "kind": t.kind, "source": t.source, "file_key": t.file_key, "params": t.params or {},
                   "start_s": t.start_s, "end_s": t.end_s, "gain_db": t.gain_db, "fade_in_s": t.fade_in_s,
                   "fade_out_s": t.fade_out_s, "spec": track_spec(t, sequences[t.sequence_id].code if t.sequence_id else None)}
                  for t in rows if t.sequence_id is None or t.sequence_id in sequences]
        loudness = project_settings(s.get(Project, project_id).settings)["loudness_lufs"]
    if not items:
        return {"error": "Henüz tamamlanmış sahne yok"}

    # Kesme listesinin içeriği anahtarla her zaman tutarlı. Kanal yoksa (Faz 11 öncesi) yalnızca sahneler özetlenir.
    basis = [[WIDTH, HEIGHT, FPS], items, titles]
    if tracks:
        basis.append(sorted(t["spec"] for t in tracks) + [loudness])
    digest = stable_hash(basis)
    key, cutlist_key = f"timeline/{project_id}/{digest}.mp4", f"timeline/{project_id}/{digest}.json"
    if not (storage.exists(key) and storage.exists(cutlist_key)):          # yarım kalmış yüklemeyi de tamamlar
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as work:
            clips, normalized, t = [], [], 0.0
            spans: dict[str, list[float]] = {}
            for i, (code, video_key, description, emotion, seq) in enumerate(items):
                src = storage.download(video_key, os.path.join(work, f"src{i}.mp4"))
                path = normalize(src, os.path.join(work, f"n{i:04d}.mp4"))
                duration = probe_duration(path)
                clips.append({"scene_code": code, "sequence": seq, "in_s": round(t, 3), "out_s": round(t + duration, 3),
                              "description": description, "emotion": emotion, "source": video_key})
                if seq:
                    spans.setdefault(seq, [t, t + duration])[1] = t + duration
                normalized.append(path)
                t += duration
            film = concat(normalized, os.path.join(work, "cut.mp4"))     # yalnızca sahnelerin kendi sesleri
            placed = place_tracks(tracks, {k: tuple(v) for k, v in spans.items()}, t)
            if placed:                                                  # sekans müziği: diyalog altında kısılır
                beds = [AudioInput(_track_file(p, work), p["at"], p["gain_db"], p["length"], p["fade_in_s"],
                                   p["fade_out_s"]) for p in placed]
                film = mux(film, [AudioInput(film)], beds, os.path.join(work, "film.mp4"), loudness)
            cutlist = os.path.join(work, "cut.json")
            with open(cutlist, "w") as f:
                json.dump({"duration_s": round(t, 3), "clips": clips,
                           "sequences": [{"code": c, "title": titles.get(c, c), "in_s": round(a, 3), "out_s": round(b, 3)}
                                         for c, (a, b) in spans.items()],
                           "tracks": [{"id": p["id"], "sequence": p["sequence"], "kind": p["kind"], "source": p["source"],
                                       "mood": p["params"].get("mood"), "in_s": p["at"],
                                       "out_s": round(p["at"] + p["length"], 3), "gain_db": p["gain_db"]}
                                      for p in placed]},
                          f, ensure_ascii=False, indent=2)
            storage.put_file(film, key)
            storage.put_file(cutlist, cutlist_key, "application/json")      # kesme listesi, film yüklendikten sonra

    with session_scope() as s:
        s.get(Project, project_id).timeline_key = key
    return {"key": key, "cutlist_key": cutlist_key, "scenes": len(items)}
