"""Renk aşaması (Faz 16): film geneli LUT (.cube, FFmpeg lut3d) ve çekim bazında ayar.

Ayarlar: exposure (EV, FFmpeg `exposure`), contrast / saturation / gamma (FFmpeg `eq`), temperature (Kelvin, FFmpeg
`colortemperature`; 6500 nötrdür, düşük değer ısıtır, yüksek değer soğutur). Sıra: LUT -> exposure -> eq -> temperature.
Aynı ayar hem videoya hem tek bir kareye (önizleme, süreklilik önerisi) birebir aynı filtreyle uygulanır.
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


def grade_video(src: str, dst: str, raw: dict | None, lut_path: str | None) -> str:
    """Videoya renk ayarı; ses (varsa) kopyalanır."""
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-map", "0:v", "-map", "0:a?",
                    "-vf", f"{filter_chain(raw, lut_path)},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "18", "-c:a", "copy", "-movflags", "+faststart", dst], check=True, capture_output=True)
    return dst


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
