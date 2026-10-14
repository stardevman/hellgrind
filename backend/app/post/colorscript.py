"""Renk betiği (color script): her sekansın baskın paleti yan yana; hedef palet stil kılavuzundan.

Her çekimin filmdeki klibinin (renk ayarlı, yoksa dudak senkronlu video) orta karesi küçültülür; sekansın pikselleri
deterministik k-ortalamalarla (LAB uzayında, sabit başlangıç) en çok 5 renge indirilir. Renkler ağırlığıyla döner (renk
betiği şeridinde genişlik). Hedef palet etkin stil kılavuzunun paletidir; sekans paletinin hedefe yakınlığı, her baskın
rengin en yakın hedef renge LAB uzaklığının ağırlıklı ortalamasıdır (düşük = yakın). Sonuç içerik-adreslidir: aynı
klipler aynı anahtarı üretir (colorscript/{proje}/{özet}.json), yeniden hesaplanmaz. Hiçbir ücretli servis çağrılmaz.
"""
import json
import os
import tempfile

import numpy as np
from sqlalchemy import select

from app import storage
from app.config import settings
from app.core.hashing import stable_hash
from app.models import Scene, Sequence
from app.style import bible

K = 5
FRAME_W, FRAME_H = 96, 54


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0-255, N×3) -> CIELAB (D65)."""
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    xyz = c @ np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]]).T
    xyz /= np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], axis=1)


def hex_of(rgb) -> str:
    return "#" + "".join(f"{int(round(max(0, min(255, x)))):02X}" for x in rgb)


def rgb_of(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)


def palette(pixels: np.ndarray, k: int = K, rounds: int = 12) -> list[dict]:
    """Deterministik k-ortalamalar (LAB): [{"hex", "weight"}] ağırlığa göre azalan. Başlangıç: parlaklığa göre sıralı
    piksellerden eşit aralıklı seçim (rastgelelik yok)."""
    if not len(pixels):
        return []
    lab = _srgb_to_lab(pixels)
    order = np.argsort(lab[:, 0])
    k = min(k, len(np.unique(pixels, axis=0)))
    centers = lab[order[np.linspace(0, len(order) - 1, k).astype(int)]]
    for _ in range(rounds):
        labels = np.argmin(((lab[:, None, :] - centers[None, :, :]) ** 2).sum(-1), axis=1)
        fresh = np.array([lab[labels == i].mean(0) if (labels == i).any() else centers[i] for i in range(k)])
        if np.allclose(fresh, centers, atol=1e-3):
            break
        centers = fresh
    labels = np.argmin(((lab[:, None, :] - centers[None, :, :]) ** 2).sum(-1), axis=1)
    out = []
    for i in range(k):
        mask = labels == i
        if mask.any():
            out.append({"hex": hex_of(pixels[mask].mean(0)), "weight": round(float(mask.mean()), 3)})
    return sorted(out, key=lambda x: -x["weight"])


def distance(pal: list[dict], target: list[str]) -> float | None:
    """Sekans paletinin hedef palete ağırlıklı LAB uzaklığı (ΔE76); hedef yoksa None."""
    if not pal or not target:
        return None
    a = _srgb_to_lab(np.array([rgb_of(x["hex"]) for x in pal]))
    b = _srgb_to_lab(np.array([rgb_of(h) for h in target]))
    nearest = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)).min(1)
    w = np.array([x["weight"] for x in pal])
    return round(float((nearest * w).sum() / w.sum()), 1)


def source_of(sc: Scene) -> str | None:
    return sc.graded_video_key or sc.lipsync_video_key or sc.final_video_key


def compute(s, project_id: str) -> dict:
    """{"sequences": [{"id", "code", "title", "palette", "shots": [{"code", "palette"}], "distance"}], "target": [...]}."""
    from app.post.grade import mid_frame
    from app.quality.anomaly import probe_duration
    style = bible.active(s, project_id)
    target = list(getattr(style, "palette", None) or [])
    seqs = list(s.scalars(select(Sequence).where(Sequence.project_id == project_id).order_by(Sequence.seq_order)))
    scenes = list(s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)))
    inputs = [(str(sc.sequence_id), sc.scene_code, source_of(sc)) for sc in scenes if source_of(sc)]
    key = f"colorscript/{project_id}/{stable_hash([inputs, K, FRAME_W])}.json"
    if storage.exists(key):
        base = json.loads(storage.read_bytes(key))
    else:
        frames: dict[str, np.ndarray] = {}
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            for i, (_, code, video_key) in enumerate(inputs):
                path = storage.download(video_key, os.path.join(tmp, f"{i}.mp4"))
                frames[code] = mid_frame(path, probe_duration(path) / 2, width=FRAME_W, height=FRAME_H).reshape(-1, 3)
        base = {"sequences": []}
        for q in seqs:
            codes = [code for seq, code, _ in inputs if seq == str(q.id)]
            pixels = np.concatenate([frames[c] for c in codes]) if codes else np.zeros((0, 3))
            base["sequences"].append({"id": str(q.id), "code": q.code, "title": q.title, "palette": palette(pixels),
                                      "shots": [{"code": c, "palette": palette(frames[c], 3)} for c in codes]})
        storage.client().put_object(Bucket=settings.s3_bucket, Key=key, Body=json.dumps(base).encode(),
                                    ContentType="application/json")
    for q in base["sequences"]:
        q["distance"] = distance(q["palette"], target)
    return {**base, "target": target, "style_version": getattr(style, "version", None)}
