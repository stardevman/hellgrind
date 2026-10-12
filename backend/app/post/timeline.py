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


def normalize(src: str, dst: str, size: tuple[int, int] | None = None) -> str:
    """Klibi ortak biçime çevirir. Ses, görüntü süresine eşitlenir: kısaysa sessizlikle tamamlanır, uzunsa kesilir.
    Eşitleme -t ile yapılır; -shortest kare hassasiyetli değildir (ses görüntüden yarım saniye uzun çıkabilir) ve
    concat -c copy her dosyayı süresi kadar ileri taşıdığından, her klipte ses biraz daha kayardı."""
    width, height = size or (WIDTH, HEIGHT)
    video = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
             f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
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


def crossfade_join(paths: list[str], durations: list[float], seams: list[bool], frames: int, out: str) -> list[float]:
    """Klipleri birleştirir; seams[i] (i. klip ile öncesi arasında continuous dikiş) için `frames` karelik geçiş
    (görüntüde xfade, seste acrossfade), diğer dikişler düz kesme. Yeniden kodlar. Her klibin filmdeki başlangıcı döner
    (geçiş, klibi öncekinin son karelerinin üstüne bindirir)."""
    d = frames / FPS
    inputs, parts, starts = [], [], []
    for p in paths:
        inputs += ["-i", p]
    v, a, t = "[0:v]", "[0:a]", durations[0]
    starts.append(0.0)
    for i in range(1, len(paths)):
        nv, na = f"[v{i}]", f"[a{i}]"
        if seams[i]:
            parts.append(f"{v}[{i}:v]xfade=transition=fade:duration={d:.4f}:offset={max(0.0, t - d):.4f}{nv}")
            parts.append(f"{a}[{i}:a]acrossfade=d={d:.4f}{na}")
            starts.append(max(0.0, t - d))
            t += durations[i] - d
        else:
            parts.append(f"{v}{a}[{i}:v][{i}:a]concat=n=2:v=1:a=1{nv}{na}")
            starts.append(t)
            t += durations[i]
        v, a = nv, na
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", ";".join(parts),
                    "-map", v, "-map", a, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", str(SAMPLE_RATE), "-ac", "2", "-b:a", "192k", "-movflags", "+faststart", out],
                   check=True)
    return starts


def track_spec(t: TimelineTrack, sequence_code: str | None) -> list:
    """Kanalın filmi belirleyen her şeyi (anahtar özetine girer)."""
    return [sequence_code, t.kind, t.source, t.content_hash, round(t.start_s, 3),
            None if t.end_s is None else round(t.end_s, 3), round(t.gain_db, 3), round(t.fade_in_s, 3),
            round(t.fade_out_s, 3)]


def active_tracks(s, project_id: str, sequences: dict) -> list[dict]:
    """Projenin aktif film/sekans kanalları, yerleştirmeye hazır sözlükler olarak (sequences: {kimlik: Sequence})."""
    rows = s.scalars(select(TimelineTrack).where(TimelineTrack.project_id == project_id, TimelineTrack.is_active)
                     .order_by(TimelineTrack.created_at, TimelineTrack.id)).all()
    out = []
    for t in rows:
        if t.sequence_id is not None and t.sequence_id not in sequences:
            continue
        code = sequences[t.sequence_id].code if t.sequence_id else None
        out.append({"id": str(t.id), "sequence": code, "kind": t.kind, "source": t.source, "file_key": t.file_key,
                    "params": t.params or {}, "start_s": t.start_s, "end_s": t.end_s, "gain_db": t.gain_db,
                    "fade_in_s": t.fade_in_s, "fade_out_s": t.fade_out_s, "spec": track_spec(t, code)})
    return out


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


def track_file(t: dict, work: str) -> str:
    """Kanalın yerel dosyası: prosedürel müzik içerik-adresli anahtardan (yerleşim süresince), yüklenen stem depodan."""
    ext = os.path.splitext(t["file_key"])[1] if t["source"] == "upload" else ".wav"
    path = os.path.join(work, f"track_{t['id']}{ext}")
    if t["source"] == "procedural":
        from app.post.pipeline import music_file          # pipeline -> timeline döngüsünü önlemek için geç import
        music_file(t["params"].get("mood", "neutral"), t["length"], path)
        return path
    return storage.download(t["file_key"], path)


def _clip_of(sc: Scene, kind: str) -> str | None:
    """Filme girecek klip. final: tamamlanmış (done) sahnenin montajı; yeniden üretilen ya da başarısız bir sahnenin
    eski videosu girmez. draft (kaba kurgu): taslak geçişin klibi; taslağı olmayan tamamlanmış sahnenin montajı."""
    if kind == "draft" and sc.draft_video_key:
        return sc.draft_video_key
    return sc.final_video_key if sc.status == "done" else None


def assemble(project_id: str, kind: str = "final") -> dict:
    """Sahneleri seq_order sırasıyla filme çevirir ve film düzeyindeki kanalları ekler. kind="draft": kaba kurgu
    (projenin draft_timeline_key alanına yazılır). Filme girecek sahne yoksa {"error": ...} döner."""
    from app.core.chain import transition_of
    with session_scope() as s:
        ordered = list(s.scalars(select(Scene).where(Scene.project_id == project_id)
                                 .order_by(Scene.seq_order, Scene.scene_code)))
        scenes = [(sc, clip) for sc in ordered if (clip := _clip_of(sc, kind))]
        index = {sc.id: i for i, sc in enumerate(ordered)}
        # continuous dikiş: çekim bir öncekinin hemen ardından, aynı sekansta ve geçişi continuous
        seams = [i > 0 and transition_of(sc) == "continuous" and index[sc.id] == index[scenes[i - 1][0].id] + 1
                 and sc.sequence_id == scenes[i - 1][0].sequence_id for i, (sc, _) in enumerate(scenes)]
        sequences = {q.id: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
        items = [(sc.scene_code, clip, sc.description, sc.emotion,
                  sequences[sc.sequence_id].code if sc.sequence_id in sequences else None) for sc, clip in scenes]
        tiers = {sc.scene_code: ("draft" if clip == sc.draft_video_key else sc.cut_tier or "final") for sc, clip in scenes}
        titles = {q.code: q.title for q in sequences.values()}
        tracks = active_tracks(s, project_id, sequences)
        cfg = project_settings(s.get(Project, project_id).settings)
        loudness = cfg["loudness_lufs"]
        fade = int(cfg.get("seam_crossfade_frames") or 0) if any(seams) else 0
    if not items:
        return {"error": "Henüz tamamlanmış sahne yok"}

    # Kesme listesinin içeriği anahtarla her zaman tutarlı. Kanal yoksa (Faz 11 öncesi) yalnızca sahneler özetlenir.
    basis = [[WIDTH, HEIGHT, FPS], items, titles] + ([kind] if kind != "final" else []) + \
        ([{"crossfade": fade, "seams": seams}] if fade else [])
    if tracks:
        basis.append(sorted(t["spec"] for t in tracks) + [loudness])
    digest = stable_hash(basis)
    key, cutlist_key = f"timeline/{project_id}/{digest}.mp4", f"timeline/{project_id}/{digest}.json"
    if not (storage.exists(key) and storage.exists(cutlist_key)):          # yarım kalmış yüklemeyi de tamamlar
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as work:
            normalized, durations = [], []
            sources = [storage.download(video_key, os.path.join(work, f"src{i}.mp4"))
                       for i, (code, video_key, description, emotion, seq) in enumerate(items)]
            from app.engine.finishing import film_size
            size = film_size(sources)          # bütün klipler upscale edildiyse film 1080p, değilse 720p
            for i, src in enumerate(sources):
                normalized.append(normalize(src, os.path.join(work, f"n{i:04d}.mp4"), size))
                durations.append(probe_duration(normalized[-1]))
            if fade:                                                    # continuous dikişlerde kısa geçiş
                starts = crossfade_join(normalized, durations, seams, fade, os.path.join(work, "cut.mp4"))
                film = os.path.join(work, "cut.mp4")
            else:
                starts = [sum(durations[:i]) for i in range(len(durations))]
                film = concat(normalized, os.path.join(work, "cut.mp4"))  # yalnızca sahnelerin kendi sesleri
            clips, spans = [], {}
            for (code, video_key, description, emotion, seq), start, duration, seam in zip(items, starts, durations, seams):
                clips.append({"scene_code": code, "sequence": seq, "in_s": round(start, 3),
                              "out_s": round(start + duration, 3), "description": description, "emotion": emotion,
                              "source": video_key, "tier": tiers[code], **({"crossfade": fade} if fade and seam else {})})
                if seq:
                    spans.setdefault(seq, [start, start + duration])[1] = start + duration
            t = starts[-1] + durations[-1]
            placed = place_tracks(tracks, {k: tuple(v) for k, v in spans.items()}, t)
            if placed:                                                  # sekans müziği: diyalog altında kısılır
                beds = [AudioInput(track_file(p, work), p["at"], p["gain_db"], p["length"], p["fade_in_s"],
                                   p["fade_out_s"]) for p in placed]
                film = mux(film, [AudioInput(film)], beds, os.path.join(work, "film.mp4"), loudness)
            cutlist = os.path.join(work, "cut.json")
            with open(cutlist, "w") as f:
                json.dump({"kind": kind, "duration_s": round(t, 3), "clips": clips,
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
        project = s.get(Project, project_id)
        if kind == "draft":
            project.draft_timeline_key = key
        else:
            project.timeline_key = key
    return {"key": key, "cutlist_key": cutlist_key, "scenes": len(items)}
