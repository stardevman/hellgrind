"""Ücretsiz, gerçek anomali tespiti (FFmpeg filtreleri).

Kademe 1: siyah kare ve donmuş görüntü. Kademe 2'de MediaPipe ile el ve kadraj kontrolü eklenecek.
"""
import re
import subprocess

BLACK_RE = re.compile(r"black_start:(?P<s>[\d.]+) black_end:(?P<e>[\d.]+)")
FREEZE_RE = re.compile(r"freeze_start: (?P<s>[\d.]+)")


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out) if out else 0.0


def detect_anomalies(path: str, black_min_s: float = 0.5, freeze_min_s: float = 2.0) -> list[dict]:
    duration = probe_duration(path)
    if duration <= 0:
        return [{"type": "empty", "detail": "süre 0"}]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an",
         "-vf", f"blackdetect=d={black_min_s}:pix_th=0.10,freezedetect=n=0.003:d={freeze_min_s}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = proc.stderr
    anomalies = []
    for m in BLACK_RE.finditer(log):
        anomalies.append({"type": "black", "start": float(m["s"]), "end": float(m["e"])})
    for m in FREEZE_RE.finditer(log):
        anomalies.append({"type": "freeze", "start": float(m["s"])})
    return anomalies
