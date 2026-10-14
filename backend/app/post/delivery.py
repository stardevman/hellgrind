"""Teslim: şartname, QC raporu, altyazı, film stem'leri, hak zinciri ve teslim paketi (ücretsiz, yerel FFmpeg).

Teslim şartnamesi (projects.delivery) filmin biçimini belirler: çözünürlük (1280x720 / 1920x1080 / 3840x2160), kare hızı
(23,976 / 24 / 25), master kodeği (H.264 / ProRes 422 HQ), loudness hedefi (LUFS), altyazı (SRT), M&E mix (diyalogsuz
müzik + efekt) ve yazısız sürüm. Şartname yoksa film eski sabitlerle birleştirilir (post/timeline.py).

QC raporu birleştirilmiş final film üzerinde çalışır; her kontrol geçti/kaldı ve zaman koduyla listelenir:
- siyah kare (FFmpeg blackdetect), donma (freezedetect), loudness (ebur128: entegre LUFS ± 1 LU, gerçek tepe ≤ -1 dBTP),
- fotosensitif: kare başına ortalama parlaklık; en az %10'luk zıt iki değişim bir parlama sayılır, herhangi bir saniyede 3'ten
  fazla parlama kalır (Harding / ITU-R BT.1702 eşiğinin yaklaşık karşılığı; tam ekran ortalaması alan koşulunu sağlar),
- çizgi titremesi: kenar haritasında art arda üç karede açık-kapalı-açık salınan kenar piksellerinin payı; yarım saniye
  boyunca %25'i aşarsa şüphe (çizgilerin kaynamasının otomatik ön elemesi; son karar gözle).
"""
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import zipfile

import numpy as np
from sqlalchemy import select, text

from app import storage
from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import AudioTrack, Project, RightsItem, Scene, Sequence

RESOLUTIONS = {"1280x720": (1280, 720), "1920x1080": (1920, 1080), "3840x2160": (3840, 2160)}
# kare hızı -> (FFmpeg yazımı, sayı, EDL zaman tabanı)
FPS = {"23.976": ("24000/1001", 24000 / 1001, 24), "24": ("24", 24.0, 24), "25": ("25", 25.0, 25)}
CODECS = {"h264": "H.264", "prores_hq": "ProRes 422 HQ"}
DEFAULT_SPEC = {"resolution": "1280x720", "fps": "24", "codec": "h264", "loudness_lufs": -23.0, "srt": False,
                "me_mix": False, "textless": False, "preset": None}
PRESETS = {
    "tv_tr": {"label": "TV (Türkiye, 25 fps)", "resolution": "1920x1080", "fps": "25", "codec": "prores_hq",
              "loudness_lufs": -23.0, "srt": True, "me_mix": True, "textless": True},
    "web": {"label": "Web", "resolution": "1920x1080", "fps": "24", "codec": "h264", "loudness_lufs": -14.0,
            "srt": True, "me_mix": False, "textless": False},
    "platform": {"label": "Platform (yüksek)", "resolution": "3840x2160", "fps": "23.976", "codec": "prores_hq",
                 "loudness_lufs": -27.0, "srt": True, "me_mix": True, "textless": True},
}
RIGHTS = {"script_rights": "Senaryo hak devri", "voice_consent": "Ses oyuncusu sözleşmesi / rızası",
          "music_license": "Müzik lisansı", "model_terms": "Model kullanım koşulları"}
AI_DECLARATION = "Yapay zekâ kullanım beyanı"
QC_NAMES = {"black": "Siyah kare", "freeze": "Donma", "loudness": "Loudness", "photosensitive": "Fotosensitif (parlama)",
            "line_flicker": "Çizgi titremesi"}
FLASH_DELTA = 25.5           # 0-255 ölçeğinde %10'luk ortalama parlaklık değişimi
FLASH_DARK_MAX = 204         # zıt değişimlerin karanlık ucu bunun altında olmalı (parlak alanda titreşim parlama sayılmaz)
FLASHES_PER_S = 3
FLICKER_SHARE = 0.25
FLICKER_MIN_S = 0.5
QC_W, QC_H = 160, 90


# ---------- şartname ----------

def spec_of(project) -> dict | None:
    """Projenin teslim şartnamesi (varsayılanlarla tamamlanmış) ya da None (eski sabitler)."""
    raw = getattr(project, "delivery", None)
    return {**DEFAULT_SPEC, **raw} if raw else None


def film_format(spec: dict | None) -> dict | None:
    """Film birleştirmenin biçimi: {"size", "fps", "fps_value", "timebase", "loudness"}; şartname yoksa None."""
    if not spec:
        return None
    rate, value, base = FPS[spec["fps"]]
    return {"size": RESOLUTIONS[spec["resolution"]], "fps": rate, "fps_value": value, "timebase": base,
            "loudness": float(spec["loudness_lufs"])}


def loudnorm(src: str, dst: str, lufs: float) -> str:
    """Filmin sesini hedef loudness'a getirir (tek geçiş loudnorm); görüntü kopyalanır."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                    "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11", "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", dst], check=True, capture_output=True)
    return dst


# ---------- zaman kodu ----------

def tc(seconds: float, fps: float = 24.0) -> str:
    frames = int(round(max(0.0, seconds) * fps))
    base = int(round(fps))
    s, ff = divmod(frames, base)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"


def scene_at(cut: dict | None, at: float) -> str | None:
    for c in (cut or {}).get("clips") or []:
        if c["in_s"] <= at < c["out_s"]:
            return c["scene_code"]
    return None


# ---------- QC ----------

def _detect(path: str) -> tuple[list[dict], list[dict]]:
    """Siyah ve donmuş aralıklar (FFmpeg)."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf",
                          "blackdetect=d=0.1:pix_th=0.10,freezedetect=n=0.003:d=1", "-an", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    black = [{"start": float(a), "end": float(b)} for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    freeze, start = [], None
    for kind, value in re.findall(r"lavfi\.freezedetect\.freeze_(start|end): ([\d.]+)", err):
        if kind == "start":
            start = float(value)
        elif start is not None:
            freeze.append({"start": start, "end": float(value)})
            start = None
    if start is not None:
        freeze.append({"start": start, "end": None})
    return black, freeze


def measure_loudness(path: str) -> dict:
    """Entegre loudness (LUFS), ses aralığı (LU) ve gerçek tepe (dBTP); ses yoksa {"silent": True}."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "ebur128=peak=true",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    i = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    lra = re.search(r"LRA:\s+([\d.]+) LU", summary)
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not i or i.group(1) == "-inf":
        return {"silent": True}
    return {"integrated": float(i.group(1)), "lra": float(lra.group(1)) if lra else None,
            "true_peak": float(tp.group(1)) if tp and tp.group(1) != "-inf" else None}


def frames_of(path: str, chunk: int = 240):
    """Gri, küçültülmüş kareler (QC_H, QC_W) parça parça (bellek film süresinden bağımsız)."""
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={QC_W}:{QC_H}", "-f", "rawvideo",
                             "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
    size = QC_W * QC_H
    try:
        while True:
            raw = proc.stdout.read(size * chunk)
            if not raw:
                break
            n = len(raw) // size
            yield np.frombuffer(raw[:n * size], np.uint8).reshape(n, QC_H, QC_W)
    finally:
        proc.stdout.close()
        proc.wait()


def flashes(luma: list[float], fps: float) -> list[dict]:
    """Zıt iki parlaklık değişimi bir parlama; herhangi bir 1 sn penceresinde FLASHES_PER_S'ten fazlası sorun."""
    edges = []                                             # (kare, yön)
    for i in range(1, len(luma)):
        d = luma[i] - luma[i - 1]
        if abs(d) >= FLASH_DELTA and min(luma[i], luma[i - 1]) < FLASH_DARK_MAX:
            edges.append((i, 1 if d > 0 else -1))
    flash_at = [edges[k][0] for k in range(1, len(edges)) if edges[k][1] != edges[k - 1][1]]
    issues, window = [], int(round(fps))
    k = 0
    while k < len(flash_at):
        j = k
        while j < len(flash_at) and flash_at[j] - flash_at[k] < window:
            j += 1
        if j - k > FLASHES_PER_S:
            start, end = flash_at[k] / fps, flash_at[j - 1] / fps
            if issues and start <= issues[-1]["end"] + 1:
                issues[-1]["end"], issues[-1]["count"] = end, max(issues[-1]["count"], j - k)
            else:
                issues.append({"start": start, "end": end, "count": j - k})
            k = j
        else:
            k += 1
    return issues


def edge_map(f: np.ndarray) -> np.ndarray:
    g = f.astype(np.int16)
    mag = np.abs(np.diff(g, axis=1, prepend=g[:, :1])) + np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
    return mag > 40


def flicker(edges: list[np.ndarray], fps: float) -> list[dict]:
    """Açık-kapalı-açık (ya da tersi) salınan kenar piksellerinin payı; FLICKER_MIN_S boyunca FLICKER_SHARE'i aşan aralık."""
    share = []
    for i in range(2, len(edges)):
        a, b, c = edges[i - 2], edges[i - 1], edges[i]
        osc = (a & ~b & c) | (~a & b & ~c)
        total = max(1, int((a | b | c).sum()))
        share.append(osc.sum() / total)
    issues, run = [], None
    need = max(1, int(round(FLICKER_MIN_S * fps)))
    for i, x in enumerate(share + [0.0]):
        if x > FLICKER_SHARE:
            run = i if run is None else run
        elif run is not None:
            if i - run >= need:
                issues.append({"start": (run + 1) / fps, "end": (i + 1) / fps,
                               "share": round(float(max(share[run:i])), 2)})
            run = None
    return issues


def run_qc(path: str, spec: dict | None, cut: dict | None) -> dict:
    """Bütün kontroller: [{"id", "name", "passed", "summary", "issues": [{"start", "end", "tc", "scene_code", "detail"}]}]."""
    from app.quality.anomaly import probe_duration
    duration = probe_duration(path)
    fps = (film_format(spec) or {}).get("fps_value") or 24.0
    target = float((spec or DEFAULT_SPEC)["loudness_lufs"])

    def issue(start, end, detail):
        end = duration if end is None else end
        return {"start": round(start, 3), "end": round(end, 3), "tc": f"{tc(start, fps)} – {tc(end, fps)}",
                "scene_code": scene_at(cut, start), "detail": detail}

    black, freeze = _detect(path)
    checks = [
        {"id": "black", "issues": [issue(b["start"], b["end"], f"{b['end'] - b['start']:.2f} sn siyah") for b in black]},
        {"id": "freeze", "issues": [issue(f["start"], f["end"], "donmuş görüntü") for f in freeze]},
    ]
    loud = measure_loudness(path)
    if loud.get("silent"):
        checks.append({"id": "loudness", "issues": [issue(0, duration, "ses yok")], "value": loud})
    else:
        problems = []
        if abs(loud["integrated"] - target) > 1.0:
            problems.append(f"entegre {loud['integrated']:.1f} LUFS, hedef {target:.1f} ± 1")
        if loud["true_peak"] is not None and loud["true_peak"] > -1.0:
            problems.append(f"gerçek tepe {loud['true_peak']:.1f} dBTP > -1")
        checks.append({"id": "loudness", "issues": [issue(0, duration, "; ".join(problems))] if problems else [],
                       "value": loud})
    luma, edges = [], []
    for chunk in frames_of(path):
        luma += [float(x) for x in chunk.reshape(len(chunk), -1).mean(axis=1)]
        edges += [edge_map(f) for f in chunk]
    measured_fps = len(luma) / duration if duration else fps
    checks.append({"id": "photosensitive", "issues": [issue(x["start"], x["end"], f"1 sn'de {x['count']} parlama")
                                                       for x in flashes(luma, measured_fps)]})
    checks.append({"id": "line_flicker", "issues": [issue(x["start"], x["end"], f"kenarların %{int(x['share'] * 100)}'i salınıyor")
                                                     for x in flicker(edges, measured_fps)]})
    for c in checks:
        c["name"], c["passed"] = QC_NAMES[c["id"]], not c["issues"]
        c["summary"] = "geçti" if c["passed"] else f"{len(c['issues'])} sorun"
        if c["id"] == "loudness" and "value" in c and not c["value"].get("silent"):
            v = c["value"]
            c["summary"] = f"{v['integrated']:.1f} LUFS, tepe {v['true_peak']:.1f} dBTP" if v.get("true_peak") is not None \
                else f"{v['integrated']:.1f} LUFS"
    return {"duration_s": round(duration, 3), "fps": round(fps, 3), "target_lufs": target,
            "passed": all(c["passed"] for c in checks), "checks": checks}


def qc_pdf(report: dict, title: str) -> bytes:
    """QC raporu, A4 PDF (Pillow; ek bağımlılık yok)."""
    from PIL import Image, ImageDraw
    from app.engine.procedural import _font
    w, h, m = 1240, 1754, 90
    pages, page, y = [], None, 0

    def new_page():
        nonlocal page, y
        page = Image.new("RGB", (w, h), "white")
        pages.append(page)
        y = m

    def line(textv, size=26, color="#111", gap=12):
        nonlocal y
        if page is None or y > h - m - size:
            new_page()
        ImageDraw.Draw(page).text((m, y), textv, font=_font(size), fill=color)
        y += size + gap

    line(f"QC raporu · {title}", 40, gap=18)
    line(f"Film: {report.get('film_key', '')}", 20, "#555")
    line(f"Tarih: {report.get('checked_at', '')} · Süre {report['duration_s']:.2f} sn · {report['fps']} fps · "
         f"hedef {report['target_lufs']:.1f} LUFS", 20, "#555", 30)
    line(f"Sonuç: {'GEÇTİ' if report['passed'] else 'KALDI'}", 34, "#1a7f4b" if report["passed"] else "#c0392b", 28)
    for c in report["checks"]:
        line(f"{'✓' if c['passed'] else '✗'}  {c['name']}: {c['summary']}", 28, "#1a7f4b" if c["passed"] else "#c0392b")
        for x in c["issues"]:
            line(f"      {x['tc']}  {x.get('scene_code') or ''}  {x['detail']}", 22, "#333", 8)
        y += 10
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return buf.getvalue()


# ---------- altyazı ----------

def _srt_tc(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def subtitles(s, project_id: str, cut: dict) -> str:
    """SRT: filmdeki her replik, kesme listesindeki çekim başlangıcı + repliğin ofseti; süre ölçülen sesten (yoksa tahmin)."""
    from app.post.voice import dialogue
    codes = {c["scene_code"]: c for c in cut.get("clips") or []}
    entries = []
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id)):
        clip = codes.get(sc.scene_code)
        if not clip:
            continue
        for t in dialogue(s, sc.id):
            start = clip["in_s"] + float(t.start_offset_s)
            length = float(t.duration_s) if t.duration_s else max(0.8, len((t.text or "").split()) / 2.5 + 0.3)
            end = min(clip["out_s"], start + length)
            if end > start and (t.text or "").strip():
                entries.append((start, end, t.text.strip()))
    entries.sort()
    return "".join(f"{i}\n{_srt_tc(a)} --> {_srt_tc(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(entries, start=1))


# ---------- hak zinciri ----------

def ai_declaration(s, project_id: str) -> list[dict]:
    """Yapay zekâ kullanım beyanı: servis geçmişinden (çekim, motor, işlem, tarih, mod). Başarılı çağrılar."""
    rows = s.execute(text("""SELECT subject, service, model, operation, started_at, dry_run FROM service_calls
                             WHERE project_id = :p AND status = 'succeeded' ORDER BY started_at"""), {"p": project_id})
    return [{"shot": r.subject or "—", "engine": f"{r.service} · {r.model or 'model bilinmiyor'}", "operation": r.operation,
             "date": r.started_at.strftime("%Y-%m-%d %H:%M"), "mode": "sanal (ücretsiz mod)" if r.dry_run else
             "gerçek" if r.dry_run is False else "bilinmiyor"} for r in rows]


def ai_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["Çekim", "Motor", "İşlem", "Tarih", "Mod"])
    for r in rows:
        out.writerow([r["shot"], r["engine"], r["operation"], r["date"], r["mode"]])
    return buf.getvalue()


def rights_missing(s, project_id: str) -> list[str]:
    """Belgesi yüklenmemiş hak zinciri maddeleri (yapay zekâ beyanı otomatiktir, eksik sayılmaz)."""
    have = set(s.scalars(select(RightsItem.kind).where(RightsItem.project_id == project_id)))
    return [label for k, label in RIGHTS.items() if k not in have]


def overview(s, project) -> dict:
    """Overview için kısa özet: QC sonucu ve sorunları (Kurgu işaretçileri), eksik hak zinciri maddeleri."""
    qc = project.qc
    issues = [{"check": c["id"], "name": c["name"], **{k: x[k] for k in ("start", "end", "tc", "scene_code", "detail")}}
              for c in (qc or {}).get("checks", []) for x in c["issues"]] if qc else []
    return {"spec": project.delivery is not None, "package": bool(project.delivery_key),
            "qc": {"passed": qc["passed"], "stale": qc.get("film_key") != project.timeline_key, "issues": issues,
                   "failed": [c["name"] for c in qc["checks"] if not c["passed"]]} if qc else None,
            "rights_missing": rights_missing(s, str(project.id))}


def rights_state(s, project_id: str) -> dict:
    items = list(s.scalars(select(RightsItem).where(RightsItem.project_id == project_id).order_by(RightsItem.created_at)))
    ai = ai_declaration(s, project_id)
    kinds = [{"kind": k, "label": label, "files": [{"id": str(x.id), "filename": x.filename, "note": x.note,
                                                    "url": f"/api/media?key={x.file_key}",
                                                    "created_at": x.created_at.timestamp() if x.created_at else None}
                                                   for x in items if x.kind == k]} for k, label in RIGHTS.items()]
    kinds.append({"kind": "ai_declaration", "label": AI_DECLARATION, "auto": True, "rows": len(ai), "files": []})
    missing = [k["label"] for k in kinds if not k.get("auto") and not k["files"]]
    return {"items": kinds, "missing": missing, "ai_rows": ai}


# ---------- stem'ler ----------

def _silence(out: str, seconds: float) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", f"{seconds:.3f}", "-i",
                    "anullsrc=r=48000:cl=stereo", out], check=True, capture_output=True)
    return out


def stems(project_id: str, cut: dict, work: str) -> dict:
    """Film boyunca üç stem (wav, 48 kHz stereo): diyalog, müzik, efekt (sfx + ortam). Çekim kanalları kesme listesindeki
    çekim başlangıcına, sekans kanalları sekans aralıklarına yerleşir (filmin birleşmesiyle aynı kural)."""
    from app.post.mux import AudioInput, mix_audio
    from app.post.pipeline import ensure_bed_audio
    from app.post.timeline import active_tracks, place_tracks, track_file
    total = float(cut["duration_s"])
    with session_scope() as s:
        scenes = {sc.scene_code: sc for sc in s.scalars(select(Scene).where(Scene.project_id == project_id))}
        tracks = {code: list(s.scalars(select(AudioTrack).where(AudioTrack.scene_id == sc.id, AudioTrack.is_active)
                                       .order_by(AudioTrack.start_offset_s))) for code, sc in scenes.items()}
        seqs = {q.id: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
        film_tracks = active_tracks(s, project_id, seqs)
        s.expunge_all()
    groups = {"dialogue": [], "music": [], "effects": []}
    for clip in cut.get("clips") or []:
        sc = scenes.get(clip["scene_code"])
        for i, t in enumerate(tracks.get(clip["scene_code"], [])):
            at, room = clip["in_s"] + float(t.start_offset_s), clip["out_s"] - clip["in_s"] - float(t.start_offset_s)
            if room <= 0:
                continue
            if t.kind == "dialogue":
                if t.file_key:
                    path = storage.download(t.file_key, os.path.join(work, f"d_{t.id}{os.path.splitext(t.file_key)[1]}"))
                    groups["dialogue"].append(AudioInput(path, at, float(t.gain_db)))
                continue
            path = ensure_bed_audio(t, sc, work)
            groups["music" if t.kind == "music" else "effects"].append(AudioInput(path, at, float(t.gain_db), room))
    spans = {q["code"]: (q["in_s"], q["out_s"]) for q in cut.get("sequences") or []}
    for p in place_tracks(film_tracks, spans, total):
        groups["music" if p["kind"] == "music" else "effects"].append(
            AudioInput(track_file(p, work), p["at"], p["gain_db"], p["length"], p["fade_in_s"], p["fade_out_s"]))
    return {name: (mix_audio(inputs, os.path.join(work, f"{name}.wav"), total) if inputs
                   else _silence(os.path.join(work, f"{name}.wav"), total)) for name, inputs in groups.items()}


def me_mix(music: str, effects: str, out: str, lufs: float) -> str:
    """M&E: diyalogsuz müzik + efekt, şartnamenin loudness hedefinde."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", music, "-i", effects, "-filter_complex",
                    f"amix=inputs=2:normalize=0,loudnorm=I={lufs}:TP=-1.5:LRA=11", "-ar", "48000", out],
                   check=True, capture_output=True)
    return out


# ---------- paket ----------

MASTER = {"h264": "master.mp4", "prores_hq": "master.mov"}


def plan(s, project_id: str) -> list[dict]:
    """Teslim paketinin dosya listesi (oluşturmadan): yol, açıklama, hazır mı."""
    p = s.get(Project, project_id)
    spec = spec_of(p) or DEFAULT_SPEC
    film = bool(p.timeline_key)
    rights = rights_state(s, project_id)
    from app.core import licensing
    rows = [
        {"path": MASTER[spec["codec"]], "label": f"Master ({CODECS[spec['codec']]}, {spec['resolution']}, {spec['fps']} fps)", "ready": film},
        {"path": "izleme_kopyasi.mp4", "label": "İzleme kopyası (H.264, 720p)", "ready": film},
        {"path": "stems/diyalog.wav", "label": "Stem: diyalog", "ready": film},
        {"path": "stems/muzik.wav", "label": "Stem: müzik", "ready": film},
        {"path": "stems/efekt.wav", "label": "Stem: efekt ve ortam", "ready": film},
    ]
    if spec["me_mix"]:
        rows.append({"path": "me_mix.wav", "label": "M&E mix (diyalogsuz müzik + efekt)", "ready": film})
    if spec["srt"]:
        rows.append({"path": "altyazi.srt", "label": "Altyazı (SRT)", "ready": film})
    if spec["textless"]:
        rows.append({"path": "yazisiz_surum.mp4", "label": "Yazısız sürüm", "ready": film})
    rows += [{"path": "film.edl", "label": "EDL (CMX3600)", "ready": film},
             {"path": "film.otio", "label": "OpenTimelineIO", "ready": film},
             {"path": "qc_raporu.pdf", "label": "QC raporu", "ready": film}]
    for item in rights["items"]:
        if item.get("auto"):
            rows.append({"path": "hak_zinciri/yapay_zeka_beyani.csv", "label": f"{item['label']} ({item['rows']} kayıt)", "ready": True})
        for f in item["files"]:
            rows.append({"path": f"hak_zinciri/{item['kind']}/{f['filename']}", "label": item["label"], "ready": True})
        if not item.get("auto") and not item["files"]:
            rows.append({"path": f"hak_zinciri/{item['kind']}/", "label": f"{item['label']} (eksik)", "ready": False})
    if licensing.restricted(s, project_id):
        rows.append({"path": "MINIMAX_H3.txt", "label": "MiniMax H3 lisans bildirimi", "ready": True})
    rows.append({"path": "BENIOKU.txt", "label": "Paket açıklaması", "ready": True})
    return rows


def build(project_id: str) -> dict:
    """Teslim paketini (zip) oluşturur: delivery/{proje}/{özet}.zip. QC raporu güncel değilse önce QC çalışır."""
    import tempfile
    from app.core import licensing
    from app.post import export
    with session_scope() as s:
        p = s.get(Project, project_id)
        if not p.timeline_key:
            return {"error": "Önce film birleştirilmeli."}
        spec = spec_of(p) or DEFAULT_SPEC
        fmt = film_format(spec) or {"timebase": 24, "fps_value": 24.0}
        film_key, title = p.timeline_key, p.title
        rights = rights_state(s, project_id)
        files = [(item["kind"], f) for item in rights["items"] for f in item["files"]]
        keys = {f["id"]: s.get(RightsItem, f["id"]).file_key for _, f in files}
        listing = plan(s, project_id)
        notice = licensing.notice(licensing.restricted(s, project_id))
        report = p.qc if p.qc and p.qc.get("film_key") == film_key else None
        srt = None
    cut = json.loads(storage.read_bytes(film_key[:-4] + ".json"))
    with session_scope() as s:
        srt = subtitles(s, project_id, cut) if spec["srt"] else None
    digest = stable_hash([film_key, spec, sorted(keys.values()), len(rights["ai_rows"]), bool(notice)])
    key = f"delivery/{project_id}/{digest}.zip"
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as work:
        film = storage.download(film_key, os.path.join(work, "film.mp4"))
        if report is None:
            report = {**run_qc(film, spec, cut), "film_key": film_key,
                      "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
            with session_scope() as s:
                s.get(Project, project_id).qc = report
        if not storage.exists(key):
            out = os.path.join(work, "paket.zip")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
                if spec["codec"] == "prores_hq":
                    master = os.path.join(work, "master.mov")
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", film, "-c:v", "prores_ks", "-profile:v", "3",
                                    "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le", master], check=True, capture_output=True)
                    z.write(master, "master.mov")
                else:
                    z.write(film, "master.mp4")
                proxy = os.path.join(work, "izleme.mp4")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", film, "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                                proxy], check=True, capture_output=True)
                z.write(proxy, "izleme_kopyasi.mp4")
                parts = stems(project_id, cut, work)
                for name, path in (("diyalog", parts["dialogue"]), ("muzik", parts["music"]), ("efekt", parts["effects"])):
                    z.write(path, f"stems/{name}.wav")
                if spec["me_mix"]:
                    z.write(me_mix(parts["music"], parts["effects"], os.path.join(work, "me.wav"), float(spec["loudness_lufs"])), "me_mix.wav")
                if srt is not None:
                    z.writestr("altyazi.srt", srt)
                if spec["textless"]:
                    z.write(film, "yazisiz_surum.mp4")
                z.writestr("film.edl", export.edl(cut, title, fmt["timebase"]))
                z.writestr("film.otio", json.dumps(export.otio(cut, title, fmt["timebase"]), ensure_ascii=False, indent=2))
                z.writestr("qc_raporu.pdf", qc_pdf(report, title))
                z.writestr("hak_zinciri/yapay_zeka_beyani.csv", ai_csv(rights["ai_rows"]))
                for kind, f in files:
                    z.write(storage.download(keys[f["id"]], os.path.join(work, f"r_{f['id']}")), f"hak_zinciri/{kind}/{f['filename']}")
                if notice:
                    z.writestr("MINIMAX_H3.txt", notice)
                z.writestr("BENIOKU.txt", readme(title, spec, listing, report))
            storage.put_file(out, key, "application/zip")
    with session_scope() as s:
        s.get(Project, project_id).delivery_key = key
    return {"key": key, "files": [r["path"] for r in listing], "qc_passed": report["passed"]}


def readme(title: str, spec: dict, listing: list[dict], report: dict) -> str:
    lines = [f"{title} — teslim paketi", "",
             f"Şartname: {spec['resolution']}, {spec['fps']} fps, {CODECS[spec['codec']]}, {spec['loudness_lufs']} LUFS",
             f"QC: {'geçti' if report['passed'] else 'kaldı'} (ayrıntı qc_raporu.pdf)", "",
             "Filme yazı ya da altyazı basılmaz: yazısız sürüm, altyazısız film görüntüsüdür; altyazı ayrı SRT dosyasındadır.", "",
             "İçerik:"]
    lines += [f"  {r['path']:<44} {r['label']}{'' if r['ready'] else '  [eksik]'}" for r in listing]
    return "\n".join(lines) + "\n"
