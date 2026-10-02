"""Testler için ortak medya yardımcıları: gerçek ffprobe/ffmpeg ile ölçüm. Üretim koduna bağlı değildir."""
import json
import re
import subprocess

import numpy as np


def probe(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    return {"duration": float(info["format"]["duration"]), "video": video, "audio": audio, "format": info["format"]}


def anomalies(path) -> dict:
    """Bağımsız ölçüm (Quality Gate'in kendi modülünden değil): siyah ve donmuş bölüm var mı?"""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf",
                          "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2", "-an", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return {"black": "black_start" in err, "freeze": "freeze_start" in err}


def frame(path, at: float = 0.0) -> np.ndarray:
    """`at` saniyesindeki kare, (yükseklik, genişlik, 3) uint8."""
    info = probe(path)["video"][0]
    w, h = int(info["width"]), int(info["height"])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)


def volume_db(path) -> float:
    """Ses akışının ortalama ses düzeyi (dB). Sessizlik çok düşük (-91) çıkar."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(m.group(1)) if m else float("-inf")


def make_video(path, seconds=3.0, size="160x90", fps=12, color=None):
    """Hızlı sahte video (lavfi). color verilirse düz renk, aksi halde hareketli test deseni."""
    source = f"color=c={color}:size={size}:rate={fps}" if color else f"testsrc2=size={size}:rate={fps}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, "-t", str(seconds), "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


def make_mixed(path, parts, size="160x90", fps=12):
    """Parçalardan oluşan video: [("black", 0.3), ("moving", 2.7), ("flat", 1.5)] (lavfi + concat)."""
    sources = {"black": f"color=c=black:size={size}:rate={fps}", "flat": f"color=c=0x808080:size={size}:rate={fps}",
               "moving": f"testsrc2=size={size}:rate={fps}"}
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for kind, seconds in parts:
        cmd += ["-f", "lavfi", "-t", str(seconds), "-i", sources[kind]]
    inputs = "".join(f"[{i}:v]" for i in range(len(parts)))
    cmd += ["-filter_complex", f"{inputs}concat=n={len(parts)}:v=1:a=0[v]", "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return str(path)
