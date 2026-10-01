"""FFmpeg muksing: video + zamanlanmış diyaloglar + fon müziği (diyalog altında otomatik kısılır)."""
import subprocess
from dataclasses import dataclass

from app.quality.anomaly import probe_duration


@dataclass
class AudioInput:
    path: str
    offset_s: float = 0.0
    gain_db: float = 0.0


def _chain(idx: int, a: AudioInput, label: str) -> str:
    ms = int(round(a.offset_s * 1000))
    return (f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"adelay=delays={ms}:all=1,volume={a.gain_db}dB[{label}]")


def _mix(labels: list[str], out: str) -> str:
    if len(labels) == 1:
        return f"[{labels[0]}]anull[{out}]"
    return "".join(f"[{l}]" for l in labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest[{out}]"


def build_command(video: str, dialogue: list[AudioInput], bed: list[AudioInput], out: str,
                  loudness_lufs: float = -23.0) -> list[str]:
    duration = probe_duration(video)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    for a in dialogue + bed:
        cmd += ["-i", a.path]

    if not dialogue and not bed:
        return cmd + ["-map", "0:v", "-c:v", "copy", "-an", "-movflags", "+faststart", out]

    parts, idx = [], 1
    d_labels = []
    for i, a in enumerate(dialogue):
        parts.append(_chain(idx, a, f"d{i}")); d_labels.append(f"d{i}"); idx += 1
    b_labels = []
    for j, a in enumerate(bed):
        parts.append(_chain(idx, a, f"b{j}")); b_labels.append(f"b{j}"); idx += 1

    if d_labels:
        parts.append(_mix(d_labels, "dlg"))
    if b_labels:
        parts.append(_mix(b_labels, "bed"))

    if d_labels and b_labels:
        parts.append("[dlg]asplit=2[dlgA][dlgSC]")
        parts.append("[bed][dlgSC]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[ducked]")
        parts.append("[dlgA][ducked]amix=inputs=2:normalize=0:duration=longest[mix]")
    else:
        parts.append(f"[{'dlg' if d_labels else 'bed'}]anull[mix]")

    parts.append(f"[mix]loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11,aresample=48000[aout]")
    return cmd + [
        "-filter_complex", ";".join(parts),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}", "-movflags", "+faststart", out,
    ]


def mux(video: str, dialogue: list[AudioInput], bed: list[AudioInput], out: str,
        loudness_lufs: float = -23.0) -> str:
    subprocess.run(build_command(video, dialogue, bed, out, loudness_lufs), check=True)
    return out
