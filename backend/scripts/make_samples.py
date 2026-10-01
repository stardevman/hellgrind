"""Sahte üretim için örnek medya oluşturur (FFmpeg, ücretsiz).

8 video: 6 sağlam, 1 siyah (anomali), 1 donmuş (anomali) + 30 sn fon müziği.
Kullanım: docker compose run --rm api python -m scripts.make_samples
"""
import os
import subprocess
import sys

GOOD = [
    "testsrc2=size=854x480:rate=24",
    "testsrc2=size=854x480:rate=24,hue=h=90",
    "testsrc2=size=854x480:rate=24,hue=h=180",
    "testsrc2=size=854x480:rate=24,hue=h=270",
    "mandelbrot=size=854x480:rate=24",
    "life=size=854x480:rate=24:mold=10:ratio=0.1:death_color=#C83232:life_color=#00FF66",
]
BAD = [
    "color=c=black:size=854x480:rate=24",      # siyah kare -> blackdetect
    "color=c=#406080:size=854x480:rate=24",    # durağan görüntü -> freezedetect
]
MUSIC = "aevalsrc=0.20*sin(2*PI*220*t)+0.15*sin(2*PI*277.18*t)+0.10*sin(2*PI*329.63*t):s=44100:d=30"


def ff(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def make(out_dir: str, duration: float = 5.0) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for i, src in enumerate(GOOD + BAD):
        path = os.path.join(out_dir, f"sample_{i:02d}.mp4")
        ff(["-f", "lavfi", "-i", src, "-t", str(duration), "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", path])
        made.append(path)
    music = os.path.join(out_dir, "music.wav")
    ff(["-f", "lavfi", "-i", MUSIC, "-ac", "2", music])
    made.append(music)
    return made


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SAMPLES_DIR", "/samples")
    for p in make(out):
        print("oluşturuldu:", p)
