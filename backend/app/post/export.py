"""Profesyonel dışa aktarma (Faz 17): son dokunuşlar DaVinci / Premiere gibi araçlarda yapılabilsin.

- CMX3600 EDL ve OpenTimelineIO JSON: birleştirilmiş filmin kesme listesinden, 24 fps, kare doğruluğunda. Klip adları
  paketteki çekim klasörlerine işaret eder (yeniden bağlama için).
- Paket (zip): çekim başına klasör: final video, rafine adaylar (etkin elle düzeltmesiyle), ayrı stem'ler (diyalog,
  müzik, efekt+ambiyans; her biri çekimin süresinde, karıştırılmamış ve ses seviyesi dengelenmemiş) ve çekim bilgisi;
  üst düzeyde EDL, OTIO, kesme listesi ve kürasyon kaydı. Sekans ve film müziği, filmdeki yerleşimine göre çekimlere
  dilimlenir: stem'ler yan yana konunca filmdeki müzik geri gelir (diyalog altında kısma uygulanmamış haliyle).
"""
import json
import os
import subprocess
import zipfile

from sqlalchemy import select

from app import storage
from app.core.hashing import grade_settings
from app.db import session_scope
from app.models import AudioTrack, Character, Project, Revision, Scene, Sequence, Variation
from app.post.mux import AudioInput, mix_audio
from app.post.timeline import FPS, active_tracks, place_tracks, track_file

STEMS = {"dialogue": ("dialogue",), "music": ("music",), "sfx": ("sfx", "ambience")}


# ---------- zaman kodu ----------

def frames(seconds: float, fps: int = FPS) -> int:
    return int(round(seconds * fps))


def timecode(frame_count: int, fps: int = FPS) -> str:
    seconds, ff = divmod(frame_count, fps)
    minutes, ss = divmod(seconds, 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def reel(code: str) -> str:
    """CMX3600 makara adı en çok 8 karakterdir: S001_SH01 -> S001SH01."""
    return code.replace("_", "")[:8]


def folder(index: int, code: str) -> str:
    return f"{index:03d}_{code}"


# ---------- EDL ----------

def edl(cut: dict, title: str, fps: int = FPS) -> str:
    """CMX3600 EDL. Kayıt zamanları kesme listesinin in/out değerleridir; kaynak her zaman klibin başından başlar."""
    lines = [f"TITLE: {title[:70]}", "FCM: NON-DROP FRAME", ""]
    for n, clip in enumerate(cut["clips"], start=1):
        rec_in, rec_out = frames(clip["in_s"], fps), frames(clip["out_s"], fps)
        lines.append(f"{n:03d}  {reel(clip['scene_code']):<8} AA/V  C        "
                     f"{timecode(0, fps)} {timecode(rec_out - rec_in, fps)} {timecode(rec_in, fps)} {timecode(rec_out, fps)}")
        lines.append(f"* FROM CLIP NAME: {folder(n, clip['scene_code'])}/final.mp4")
        if clip.get("sequence"):
            lines.append(f"* COMMENT: {clip['sequence']} · {clip.get('description') or ''}"[:120])
        lines.append("")
    return "\n".join(lines)


# ---------- OpenTimelineIO ----------

def _rt(value: int, fps: int) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(fps), "value": float(value)}


def _range(start: int, duration: int, fps: int) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, fps), "duration": _rt(duration, fps)}


def _clip(n: int, clip: dict, fps: int) -> dict:
    duration = frames(clip["out_s"], fps) - frames(clip["in_s"], fps)
    target = f"{folder(n, clip['scene_code'])}/final.mp4"
    return {"OTIO_SCHEMA": "Clip.1", "name": clip["scene_code"], "source_range": _range(0, duration, fps),
            "media_reference": {"OTIO_SCHEMA": "ExternalReference.1", "name": target, "target_url": target,
                                "available_range": _range(0, duration, fps), "metadata": {}},
            "effects": [], "markers": [], "enabled": True,
            "metadata": {"hellgrind": {"sequence": clip.get("sequence"), "description": clip.get("description"),
                                       "emotion": clip.get("emotion"), "tier": clip.get("tier")}}}


def otio(cut: dict, title: str, fps: int = FPS, jumps: set[str] | None = None) -> dict:
    """OpenTimelineIO zaman çizelgesi (Timeline.1): V1 ve A1 izleri aynı klipleri taşır. Renk sıçraması olan kesmeler
    işaretlenir (marker)."""
    video = [_clip(n, c, fps) for n, c in enumerate(cut["clips"], start=1)]
    for clip in video:
        if jumps and clip["name"] in jumps:
            clip["markers"].append({"OTIO_SCHEMA": "Marker.2", "name": "Renk sıçraması", "color": "RED",
                                    "marked_range": _range(0, 0, fps), "metadata": {}})
    audio = [_clip(n, c, fps) for n, c in enumerate(cut["clips"], start=1)]

    def track(name, kind, children):
        return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "children": children, "source_range": None,
                "effects": [], "markers": [], "enabled": True, "metadata": {}}
    return {"OTIO_SCHEMA": "Timeline.1", "name": title, "global_start_time": _rt(0, fps),
            "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "children": [track("V1", "Video", video),
                                                                               track("A1", "Audio", audio)],
                       "source_range": None, "effects": [], "markers": [], "enabled": True, "metadata": {}},
            "metadata": {"hellgrind": {"sequences": cut.get("sequences", []), "fps": fps}}}


# ---------- paket ----------

def _silence(out: str, duration: float) -> str:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", f"{duration:.3f}", out], check=True, capture_output=True)
    return out


def _stem(inputs: list[AudioInput], out: str, duration: float) -> str:
    return mix_audio(inputs, out, duration) if inputs else _silence(out, duration)


def _timeline_slices(placed: list[dict], kinds: tuple, clip: dict, work: str) -> list[AudioInput]:
    """Filme yerleşmiş kanalların bu çekime düşen dilimleri (çekimin başına göre)."""
    out = []
    cin, cout = clip["in_s"], clip["out_s"]
    for p in placed:
        if p["kind"] not in kinds:
            continue
        start, end = max(cin, p["at"]), min(cout, p["at"] + p["length"])
        if end - start < 0.05:
            continue
        out.append(AudioInput(track_file(p, work), offset_s=start - cin, gain_db=p["gain_db"], duration_s=end - start,
                              skip_s=start - p["at"],
                              fade_in_s=p["fade_in_s"] if start == p["at"] else 0.0,
                              fade_out_s=p["fade_out_s"] if end == p["at"] + p["length"] else 0.0))
    return out


def package(project_id: str, cut: dict, out_zip: str, work: str, curation_jsonl: str, continuity_jumps: set[str],
            notice: str | None = None) -> str:
    """Çekim klasörleri ve üst düzey dosyalarla zip. Medya sıkıştırılmadan (ZIP_STORED) yazılır. notice: MiniMax H3
    çıktısı içeren pakette lisans bildirimi (MINIMAX_H3.txt)."""
    from app.post.pipeline import ensure_bed_audio            # pipeline -> export döngüsünü önlemek için geç import
    with session_scope() as s:
        project = s.get(Project, project_id)
        title = project.title
        scenes = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == project_id))}
        sequences = {q.id: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
        tracks = active_tracks(s, project_id, sequences)
        audio = {code: list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.is_active)
                                      .order_by(AudioTrack.start_offset_s))) for code, sc in scenes.items()}
        names = dict(s.execute(select(Character.id, Character.name).where(Character.project_id == project_id)).all())
        pools = {}                                   # kod -> [(sıra, varyasyon, video anahtarı, skor, revizyon sürümü)]
        for code, sc in scenes.items():
            pools[code] = []
            for v in s.scalars(select(Variation).where(Variation.scene_id == sc.id, Variation.refined_rank.is_not(None))
                               .order_by(Variation.refined_rank)):
                rev = s.get(Revision, v.revision_id) if v.revision_id else None
                pools[code].append((v.refined_rank, str(v.id), rev.video_key if rev else v.video_key, v.composite_score,
                                    rev.version if rev else None))
        s.expunge_all()
    spans = {q["code"]: (q["in_s"], q["out_s"]) for q in cut.get("sequences", [])}
    placed = place_tracks(tracks, spans, cut["duration_s"])

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED) as z:
        z.writestr("film.edl", edl(cut, title))
        z.writestr("film.otio", json.dumps(otio(cut, title, jumps=continuity_jumps), ensure_ascii=False, indent=2))
        z.writestr("kesme_listesi.json", json.dumps(cut, ensure_ascii=False, indent=2))
        z.writestr("kurasyon.jsonl", curation_jsonl)
        z.writestr("BENIOKU.txt", README.format(title=title))
        if notice:
            z.writestr("MINIMAX_H3.txt", notice)
        for n, clip in enumerate(cut["clips"], start=1):
            sc, base = scenes[clip["scene_code"]], folder(n, clip["scene_code"])
            duration = clip["out_s"] - clip["in_s"]
            z.write(storage.download(clip["source"], os.path.join(work, f"{base}_final.mp4")), f"{base}/final.mp4")
            for rank, vid, key, score, revision in pools[clip["scene_code"]]:
                if key and storage.exists(key):
                    name = f"rank{rank}_{vid[:8]}" + (f"_r{revision}" if revision else "") + ".mp4"
                    z.write(storage.download(key, os.path.join(work, f"{base}_{name}")), f"{base}/rafine/{name}")
            for stem, kinds in STEMS.items():
                inputs = []
                for t in audio[clip["scene_code"]]:
                    if t.kind not in kinds:
                        continue
                    if t.kind == "dialogue":
                        if not t.file_key:
                            continue
                        path = storage.download(t.file_key, os.path.join(work, f"{t.id}{os.path.splitext(t.file_key)[1]}"))
                    else:
                        path = ensure_bed_audio(t, sc, work)
                    inputs.append(AudioInput(path, t.start_offset_s, t.gain_db))
                inputs += _timeline_slices(placed, kinds, clip, work)
                z.write(_stem(inputs, os.path.join(work, f"{base}_{stem}.wav"), duration), f"{base}/stems/{stem}.wav")
            info = {"scene_code": clip["scene_code"], "sequence": clip.get("sequence"), "in_s": clip["in_s"],
                    "out_s": clip["out_s"], "description": sc.description, "camera": sc.camera, "emotion": sc.emotion,
                    "duration_s": round(duration, 3), "method": sc.method, "cut_tier": sc.cut_tier,
                    "grade": grade_settings(sc.grade), "prompt": (sc.prompt_spec or {}).get("prompt"),
                    "dialogue": [{"character": names.get(t.character_id), "text": t.text, "start_s": t.start_offset_s}
                                 for t in audio[clip["scene_code"]] if t.kind == "dialogue"],
                    "refined": [{"rank": r, "variation_id": v, "score": sc_, "revision": rev}
                                for r, v, _, sc_, rev in pools[clip["scene_code"]]]}
            z.writestr(f"{base}/cekim.json", json.dumps(info, ensure_ascii=False, indent=2, default=str))
    return out_zip


README = """{title} — Hell Grind dışa aktarma paketi

film.edl            CMX3600 EDL (24 fps, NON-DROP). Klip adları çekim klasörlerindeki final.mp4 dosyalarıdır.
film.otio           OpenTimelineIO zaman çizelgesi (V1 ve A1). Renk sıçraması olan kesmeler işaretlidir.
kesme_listesi.json  Filmin kesme listesi (in/out saniye).
kurasyon.jsonl      Yönetmenin kararları (her satır bir olay).

Her çekim klasörü (NNN_KOD):
  final.mp4         Çekimin montajlı (renk, ses, dudak senkronu) videosu.
  rafine/           Rafine havuzdaki adaylar, sırayla (elle düzeltilmişse etkin revizyon).
  stems/            dialogue.wav, music.wav, sfx.wav (efekt + ambiyans). Her biri çekimin süresinde; karıştırılmamış,
                    ses seviyesi dengelenmemiş ve diyalog altında kısma uygulanmamıştır. Sekans müziği filmdeki
                    yerine göre dilimlenmiştir: stem'ler yan yana konunca müzik kesintisiz akar.
  cekim.json        Çekim bilgisi: kamera, süre, renk ayarı, yöntem, diyalog, rafine adaylar.
"""
