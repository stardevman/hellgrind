"""Ses/video testleri için ortak ölçüm yardımcıları (gerçek FFmpeg)."""
import io
import json
import re
import subprocess

import numpy as np
from PIL import Image

SR = 48000


def run(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def make_video(path, duration, size="160x90", fps=12):
    """Hareketli (donmuş/siyah olmayan) test videosu."""
    run("-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}", "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return str(path)


def make_sine(path, freq, duration, volume=0.5):
    """`volume`: sinüsün gerçek tepe genliği (FFmpeg'in sine kaynağı varsayılan olarak yalnızca 1/8 üretir)."""
    run("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100:duration={duration}",
        "-af", f"volume={volume * 8}", "-ac", "1", str(path))
    return str(path)


def pcm(path, t0=0.0, t1=None):
    """[t0, t1) aralığını 48 kHz mono float32 dizisi olarak okur."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(t0)]
    if t1 is not None:
        cmd += ["-t", str(t1 - t0)]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32)


def tone_amp(x, freq):
    """Pencereli (Hann) tek frekanslı genlik kestirimi."""
    t = np.arange(len(x)) / SR
    w = np.hanning(len(x))
    return float(2 * abs(np.sum(w * x * np.exp(-2j * np.pi * freq * t))) / np.sum(w))


def rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def db(ratio):
    return 20 * np.log10(max(ratio, 1e-9))


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,channels,"
                          "sample_rate,bit_rate,nb_frames,duration,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return data["streams"], float(data["format"]["duration"])


def loudness(path):
    """Bütünleşik gürültü seviyesi (LUFS), EBU R128."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])


def frame(path, t=0.5):
    """t anındaki kareyi (H, W, 3) RGB dizisi olarak döndürür."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.int16)


def band_spectrum(x, bands=48, lo=80, hi=8000):
    """Logaritmik bantlara bölünmüş, birim uzunlukta genlik spektrumu (sesin 'rengi'). İki sesin benzerliği
    iç çarpımlarıdır (1.0 = aynı). espeak-ng çıktısı bayt düzeyinde deterministik değildir ama bu parmak izi kararlıdır."""
    mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    edges = np.geomspace(lo, hi, bands + 1)
    out = np.array([mag[(freqs >= a) & (freqs < b)].sum() for a, b in zip(edges[:-1], edges[1:])])
    return out / np.linalg.norm(out)
