"""Renk aşaması (Faz 16): film geneli LUT (.cube, FFmpeg lut3d) ve çekim bazında ayar.

Ayarlar: exposure (EV, FFmpeg `exposure`), contrast / saturation / gamma (FFmpeg `eq`), temperature (Kelvin, FFmpeg
`colortemperature`; 6500 nötrdür, düşük değer ısıtır, yüksek değer soğutur). Sıra: LUT -> exposure -> eq -> temperature.
Aynı ayar hem videoya hem tek bir kareye (önizleme, süreklilik önerisi) birebir aynı filtreyle uygulanır.

Kare tutma (on twos, proje ayarı): 2D animasyonun "12 çizim/sn" görünümü. Renkten sonra her ikinci kare atılır
(framestep=2) ve kaynak kare hızına geri doldurulur (fps): kare sayısı ve süre aynı kalır, her çizim iki kare görünür.
"""
import subprocess

import numpy as np

from app.core.hashing import GRADE_IDENTITY, grade_settings

LIMITS = {"exposure": (-3.0, 3.0), "contrast": (0.5, 2.0), "saturation": (0.0, 3.0), "gamma": (0.3, 3.0),
          "temperature": (2000, 12000)}
FRAME_W, FRAME_H = 160, 90


class InvalidLut(ValueError):
    """Yüklenen dosya geçerli bir 3B .cube LUT değil (mesaj Türkçe)."""


def validate_cube(text: str) -> int:
    """3B .cube dosyasını denetler; LUT boyutunu döndürür. FFmpeg'e bozuk dosya verilmesin."""
    size, rows = None, 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("TITLE", "DOMAIN_MIN", "DOMAIN_MAX")):
            continue
        if line.startswith("LUT_1D_SIZE"):
            raise InvalidLut("1B LUT desteklenmiyor; 3B .cube dosyası yükle.")
        if line.startswith("LUT_3D_SIZE"):
            try:
                size = int(line.split()[1])
            except (IndexError, ValueError):
                raise InvalidLut("LUT_3D_SIZE okunamadı.")
            if not 2 <= size <= 65:
                raise InvalidLut("LUT_3D_SIZE 2 ile 65 arasında olmalı.")
            continue
        parts = line.split()
        if len(parts) != 3:
            raise InvalidLut(f"Beklenmeyen satır: {line[:40]}")
        try:
            [float(p) for p in parts]
        except ValueError:
            raise InvalidLut(f"Sayı olmayan değer: {line[:40]}")
        rows += 1
    if size is None:
        raise InvalidLut("LUT_3D_SIZE satırı yok.")
    if rows != size ** 3:
        raise InvalidLut(f"{size}³ = {size ** 3} renk satırı bekleniyordu, {rows} bulundu.")
    return size


def clamp(raw: dict) -> dict:
    """Ayarı sınırlar içine alır ve normalleştirir."""
    g = grade_settings(raw)
    return grade_settings({k: min(max(g[k], LIMITS[k][0]), LIMITS[k][1]) for k in g})


def filter_chain(raw: dict | None, lut_path: str | None) -> str:
    """FFmpeg -vf zinciri; etkisiz parçalar atlanır. Hiçbir şey yoksa "null"."""
    g = grade_settings(raw)
    parts = []
    if lut_path:
        parts.append(f"lut3d=file='{lut_path}'")
    if g["exposure"] != GRADE_IDENTITY["exposure"]:
        parts.append(f"exposure=exposure={g['exposure']}")
    if (g["contrast"], g["saturation"], g["gamma"]) != (1.0, 1.0, 1.0):
        parts.append(f"eq=contrast={g['contrast']}:saturation={g['saturation']}:gamma={g['gamma']}")
    if g["temperature"] != GRADE_IDENTITY["temperature"]:
        parts.append(f"colortemperature=temperature={g['temperature']}")
    return ",".join(parts) or "null"


def probe_fps(video: str) -> str:
    """Videonun kare hızı, FFmpeg'in kesirli yazımıyla ("24/1", "24000/1001"); okunamazsa "24"."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip()
    return out if out and out not in ("0/0", "N/A") else "24"


def hold_filter(fps: str) -> str:
    """Kare tutma (on twos): her ikinci kare atılır, kaynak hızına geri doldurulur (her çizim iki kare)."""
    return f"framestep=2,fps=fps={fps}"


def grade_video(src: str, dst: str, raw: dict | None, lut_path: str | None, on_twos: bool = False) -> str:
    """Videoya renk ayarı (ve istenirse kare tutma); ses (varsa) kopyalanır."""
    vf = filter_chain(raw, lut_path)
    if on_twos:
        vf = f"{vf},{hold_filter(probe_fps(src))}"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-map", "0:v", "-map", "0:a?",
                    "-vf", f"{vf},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-c:a", "copy", "-movflags", "+faststart", dst], check=True, capture_output=True)
    return dst


STRIP_FRAMES = 6
STRIP_W = 200


def strip_jpeg(video: str, at: float, out: str, raw: dict | None = None, lut_path: str | None = None,
               on_twos: bool = False, frames: int = STRIP_FRAMES) -> str:
    """`at` saniyesinden başlayan art arda `frames` kare yan yana (renk ayarıyla ve istenirse kare tutmayla): kare
    tutmanın etkisi durağan bir görüntüde görünür (aynı çizim iki karede tekrar eder)."""
    fps = probe_fps(video)
    vf = filter_chain(raw, lut_path)
    if on_twos:
        vf = f"{vf},{hold_filter(fps)}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", video, "-frames:v", "1",
                    "-vf", f"{vf},scale={STRIP_W}:-2,tile={frames}x1:padding=2:color=0x111214", "-q:v", "4", out],
                   check=True, capture_output=True)
    return out


def mid_frame(video: str, at: float, raw: dict | None = None, lut_path: str | None = None,
              width: int = FRAME_W, height: int = FRAME_H) -> np.ndarray:
    """`at` saniyesindeki kare, (isteğe bağlı) renk ayarıyla; RGB uint8 (yükseklik, genişlik, 3)."""
    vf = f"{filter_chain(raw, lut_path)},scale={width}:{height}"
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", video, "-frames:v", "1", "-vf", vf,
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(height, width, 3)


def jpeg_frame(video: str, at: float, out: str, raw: dict | None = None, lut_path: str | None = None) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", video, "-frames:v", "1",
                    "-vf", f"{filter_chain(raw, lut_path)},scale=480:-2", "-q:v", "4", out], check=True,
                   capture_output=True)
    return out
