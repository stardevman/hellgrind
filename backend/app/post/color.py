"""Renk ölçüsü ve aktarımı (ağ yok, numpy): sRGB <-> CIELAB, karakter dışı bölge, istatistik ve Reinhard aktarımı.

Karakter dışı bölge: üst %22, sol %28 ve sağ %18 (karakterler çoğunlukla ortada ve aşağıda). Kıvılcım kalibrasyonu (arka
plan LAB, L 0-100): renklendirilmemiş SH01 -> SH02 ΔL -4,2 / ΔE 4,4 (gözle belirgin sıçrama); yönetmenin renklendirdiği
SH01 -> SH02 ΔE 1,3; diğer ardışık çekimler ΔE 0,4-1,3. Atlama: ΔL > 3 ya da ΔE > 4.
"""
import numpy as np

REGION = {"top": 0.22, "left": 0.28, "right": 0.18}
JUMP_DL, JUMP_DE = 3.0, 4.0
_WHITE = np.array([0.95047, 1.0, 1.08883])
_M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
_MI = np.linalg.inv(_M)


def background_mask(h: int, w: int) -> np.ndarray:
    m = np.zeros((h, w), bool)
    m[: int(h * REGION["top"]), :] = True
    m[:, : int(w * REGION["left"])] = True
    m[:, int(w * (1 - REGION["right"])):] = True
    return m


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """uint8 RGB (H, W, 3) -> LAB (L 0-100, a, b)."""
    c = rgb.astype(float) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ _M.T / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    fy = (lab[..., 0] + 16) / 116
    fx, fz = fy + lab[..., 1] / 500, fy - lab[..., 2] / 200
    f = np.stack([fx, fy, fz], axis=-1)
    xyz = np.where(f ** 3 > 0.008856, f ** 3, (f - 16 / 116) / 7.787) * _WHITE
    c = xyz @ _MI.T
    c = np.where(c > 0.0031308, 1.055 * np.clip(c, 0, None) ** (1 / 2.4) - 0.055, 12.92 * c)
    return np.clip(np.round(c * 255), 0, 255).astype(np.uint8)


def stats(rgb: np.ndarray) -> dict:
    """Karakter dışı bölgenin LAB ortalaması ve sapması."""
    lab = rgb_to_lab(rgb)[background_mask(*rgb.shape[:2])]
    mean, sd = lab.mean(axis=0), lab.std(axis=0)
    return {"L": round(float(mean[0]), 2), "a": round(float(mean[1]), 2), "b": round(float(mean[2]), 2),
            "sdL": round(float(sd[0]), 2), "sda": round(float(sd[1]), 2), "sdb": round(float(sd[2]), 2)}


def difference(a: dict, b: dict) -> dict:
    """b - a: ΔL, Δa, Δb (sıcaklık: Δb), ΔE76, sapma farkı."""
    d = {k: round(b[k] - a[k], 2) for k in ("L", "a", "b", "sdL")}
    d["E"] = round(float(np.sqrt(d["L"] ** 2 + d["a"] ** 2 + d["b"] ** 2)), 2)
    return d


def is_jump(d: dict) -> bool:
    return abs(d["L"]) > JUMP_DL or d["E"] > JUMP_DE


def transfer(rgb: np.ndarray, target: dict, max_shift: float = 8.0, max_ratio: float = 1.2) -> np.ndarray:
    """Reinhard renk aktarımı, sınırlı: karenin karakter dışı bölgesinin LAB ortalaması ve sapması hedefe yaklaşır (ortalama
    kayması en çok max_shift, sapma oranı 1/max_ratio..max_ratio); bütün kareye uygulanır."""
    lab = rgb_to_lab(rgb)
    own = stats(rgb)
    out = lab.copy()
    for i, k in enumerate(("L", "a", "b")):
        sd_own, sd_target = own["sd" + k], target["sd" + k]
        ratio = float(np.clip(sd_target / sd_own, 1 / max_ratio, max_ratio)) if sd_own > 1e-6 else 1.0
        shift = float(np.clip(target[k] - own[k], -max_shift, max_shift))
        out[..., i] = (lab[..., i] - own[k]) * ratio + own[k] + shift
    return lab_to_rgb(out)
