"""Gerçek anomali tespiti (ücretsiz): FFmpeg'in blackdetect ve freezedetect filtreleri, sonuç stderr'den okunur.

Kusuru üreticinin söylemesine güvenmez: bozuk klibi yalnızca videonun kendisine bakarak bulur.
"""
import re
import subprocess

BLACK_MIN_S = 0.5          # en az bu kadar süren siyah bölüm kusurdur
FREEZE_MIN_S = 2.0         # en az bu kadar süren donuk bölüm kusurdur
BLACK_PIXEL_THRESHOLD = 0.10
FREEZE_NOISE = 0.003

BLACK_RE = re.compile(r"black_start:(?P<start>[\d.]+) black_end:(?P<end>[\d.]+)")
FREEZE_START_RE = re.compile(r"freeze_start:\s*(?P<start>[\d.]+)")
FREEZE_END_RE = re.compile(r"freeze_end:\s*(?P<end>[\d.]+)")


def probe_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out) if out and out != "N/A" else 0.0


def detect_anomalies(path: str, black_min_s: float = BLACK_MIN_S, freeze_min_s: float = FREEZE_MIN_S) -> list[dict]:
    """[{"type": "black", "start", "end"} | {"type": "freeze", "start", "end"?} | {"type": "empty"|"unreadable"}]."""
    try:
        duration = probe_duration(path)
    except (subprocess.CalledProcessError, ValueError) as e:
        return [{"type": "unreadable", "detail": str(e)[:200]}]      # bozuk dosya dalgayı çökertmesin, elensin
    if duration <= 0:
        return [{"type": "empty", "detail": "süre 0"}]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
         f"blackdetect=d={black_min_s}:pix_th={BLACK_PIXEL_THRESHOLD},freezedetect=n={FREEZE_NOISE}:d={freeze_min_s}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return [{"type": "unreadable", "detail": proc.stderr.strip()[-200:]}]
    anomalies = [{"type": "black", "start": float(m["start"]), "end": float(m["end"])}
                 for m in BLACK_RE.finditer(proc.stderr)]
    ends = [float(m["end"]) for m in FREEZE_END_RE.finditer(proc.stderr)]
    for i, m in enumerate(FREEZE_START_RE.finditer(proc.stderr)):
        item = {"type": "freeze", "start": float(m["start"])}
        if i < len(ends):
            item["end"] = ends[i]
        anomalies.append(item)
    return anomalies
