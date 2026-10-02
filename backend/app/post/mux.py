"""FFmpeg muksing: video + zamanlanmış diyaloglar + fon (diyalog altında müzik otomatik kısılır)."""
import subprocess
from dataclasses import dataclass

from app.quality.anomaly import probe_duration

SAMPLE_RATE = 48000
AUDIO_BITRATE = "192k"


@dataclass
class AudioInput:
    path: str
    offset_s: float = 0.0
    gain_db: float = 0.0
    duration_s: float | None = None     # verilirse kanal bu süreye kırpılır (film düzeyindeki sekans müziği)
    fade_in_s: float = 0.0              # yalnızca duration_s ile
    fade_out_s: float = 0.0
    skip_s: float = 0.0                 # yalnızca duration_s ile: kaynağın başından atlanan süre (stem dilimleme)


def _channel(index: int, a: AudioInput, label: str) -> str:
    """Her kanal: 48k stereo, (isteğe bağlı) kırpma ve fade, başlangıç ofseti (adelay), kazanç (volume)."""
    ms = int(round(a.offset_s * 1000))
    shape = ""
    if a.duration_s is not None:
        d = a.duration_s
        start = f"{a.skip_s:.3f}:{a.skip_s + d:.3f}" if a.skip_s else f"0:{d:.3f}"
        shape = f"atrim={start},asetpts=PTS-STARTPTS,"
        if a.fade_in_s > 0:
            shape += f"afade=t=in:st=0:d={min(a.fade_in_s, d):.3f},"
        if a.fade_out_s > 0:
            fade = min(a.fade_out_s, d)
            shape += f"afade=t=out:st={d - fade:.3f}:d={fade:.3f},"
    return (f"[{index}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,{shape}"
            f"adelay=delays={ms}:all=1,volume={a.gain_db}dB[{label}]")


def _mix(labels: list[str], out: str, pad_s: float) -> str:
    """Kanalları toplar ve video süresine kadar sessizlikle tamamlar. Tamamlama şart: sidechaincompress yan zincir
    (diyalog) bitince çıktıyı keser; müzik son replikle susardı, ses akışı da videodan kısa kalırdı."""
    pad = f"apad=whole_dur={pad_s:.3f}"
    if len(labels) == 1:
        return f"[{labels[0]}]{pad}[{out}]"
    return "".join(f"[{l}]" for l in labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest,{pad}[{out}]"


def build_command(video: str, dialogue: list[AudioInput], bed: list[AudioInput], out: str,
                  loudness_lufs: float = -23.0) -> list[str]:
    duration = probe_duration(video)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    for a in dialogue + bed:
        cmd += ["-i", a.path]
    if not dialogue and not bed:                                   # ses yok: video sessiz kopyalanır
        return cmd + ["-map", "0:v", "-c:v", "copy", "-an", "-movflags", "+faststart", out]

    parts, index = [], 1
    dialogue_labels, bed_labels = [], []
    for i, a in enumerate(dialogue):
        parts.append(_channel(index, a, f"d{i}"))
        dialogue_labels.append(f"d{i}")
        index += 1
    for i, a in enumerate(bed):
        parts.append(_channel(index, a, f"b{i}"))
        bed_labels.append(f"b{i}")
        index += 1
    if dialogue_labels:
        parts.append(_mix(dialogue_labels, "dlg", duration))
    if bed_labels:
        parts.append(_mix(bed_labels, "bed", duration))

    if dialogue_labels and bed_labels:                             # ducking: diyalog varken fon kısılır
        parts += ["[dlg]asplit=2[dlgA][dlgSC]",
                  "[bed][dlgSC]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[ducked]",
                  "[dlgA][ducked]amix=inputs=2:normalize=0:duration=longest[mix]"]
    else:
        parts.append(f"[{'dlg' if dialogue_labels else 'bed'}]anull[mix]")
    parts.append(f"[mix]loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11,aresample={SAMPLE_RATE}[aout]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[aout]",
                  "-c:v", "copy", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                  "-t", f"{duration:.3f}", "-movflags", "+faststart", out]       # süre videoya eşit


def mux(video: str, dialogue: list[AudioInput], bed: list[AudioInput], out: str, loudness_lufs: float = -23.0) -> str:
    subprocess.run(build_command(video, dialogue, bed, out, loudness_lufs), check=True)
    return out


def mix_audio(inputs: list[AudioInput], out: str, duration: float) -> str:
    """Zamanlanmış kanalları tek bir wav dosyasında toplar (ses akışı, `duration` süresine eşit). Dudak senkronu,
    videoyla birlikte tek bir ses dosyası ister."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for a in inputs:
        cmd += ["-i", a.path]
    parts = [_channel(i, a, f"a{i}") for i, a in enumerate(inputs)]
    parts.append(_mix([f"a{i}" for i in range(len(inputs))], "mix", duration))
    subprocess.run(cmd + ["-filter_complex", ";".join(parts), "-map", "[mix]", "-t", f"{duration:.3f}", out], check=True)
    return out
