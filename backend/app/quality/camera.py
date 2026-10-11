"""Kamera ölçüleri (OpenCV; ağ yok, büyük model yok): iki kare aynı kamerayla mı çekilmiş (jump cut) ve klipte kamera
hareketi (pan, tilt, dolly) var mı.

Aynı kamera: karakter dışı bölgede (üst, sol, sağ şeritler; post/color.background_mask) ORB anahtar noktaları eşlenir,
RANSAC homografisi bulunur. Eşleşmelerin çoğu tek homografiye oturuyor, ölçek ~1 ve kayma küçükse kamera değişmemiştir.
Kıvılcım kalibrasyonu (önceki çekimin son karesi -> kesmenin ilk karesi; hepsi jump cut): iç oran 0,82-0,90, ölçek
0,97-1,00, kayma <= %1,1; ikinci tur SH02 son kare -> SH03 (aynı geniş plandan aynı yeni açıya gitmişti) iç oran 0,92,
ölçek 0,98. Kamerası gerçekten değişen SH01 -> SH02 iç oran 0,03. Sentetik kamera değişimi: %80 yakınlaşma ölçek
1,24; %60 yakınlaşma iç oran 0,03; ayna (ters açı) iç oran 0,04. DINOv2 arka plan benzerliği aynayı ayıramadı (0,88), karar homografide.
"""
import numpy as np

SAME_RATIO = 0.5               # iç oranı bunun üstü ve
SAME_SCALE = 0.10              # |ölçek - 1| bunun altı ve
SAME_SHIFT = 0.05              # kayma (kare genişliğine göre) bunun altıysa aynı kamera
CUT_IN_SCALE = 1.5             # aynı kamera, >= 1,5 kat yakın: geçerli cut-in (jump cut değil)
MIN_MATCHES = 12
MOTION = {"pan": ("dx", 0.04), "tracking": ("dx", 0.04), "tilt": ("dy", 0.04), "crane": ("dy", 0.04),
          "dolly_in": ("zoom_in", 0.04), "dolly_out": ("zoom_out", 0.04), "orbit": ("dx", 0.04)}


def _gray(image) -> np.ndarray:
    import cv2
    return cv2.cvtColor(np.asarray(image.convert("RGB").resize((1280, 704))), cv2.COLOR_RGB2GRAY)


def homography(a, b) -> dict:
    """{"matches", "ratio", "scale", "shift", "dx", "dy"}: a'dan b'ye arka plan homografisi (PIL görselleri)."""
    import cv2
    from app.post.color import background_mask
    ga, gb = _gray(a), _gray(b)
    mask = background_mask(*ga.shape).astype(np.uint8) * 255
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(ga, mask)
    kb, db = orb.detectAndCompute(gb, mask)
    if da is None or db is None:
        return {"matches": 0, "ratio": 0.0}
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < MIN_MATCHES:
        return {"matches": len(matches), "ratio": 0.0}
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    h, inliers = cv2.findHomography(pa, pb, cv2.RANSAC, 4.0)
    if h is None:
        return {"matches": len(matches), "ratio": 0.0}
    w = ga.shape[1]
    return {"matches": len(matches), "ratio": round(float(inliers.mean()), 3),
            "scale": round(float(np.sqrt(abs(np.linalg.det(h[:2, :2])))), 3),
            "shift": round(float(np.hypot(h[0, 2], h[1, 2]) / w), 3),
            "dx": round(float(h[0, 2] / w), 3), "dy": round(float(h[1, 2] / ga.shape[0]), 3)}


def same_camera(a, b) -> dict:
    """homography + "same": kamera konumu, açısı ve kadrajı değişmemiş mi (jump cut riski). "cut_in": aynı kameranın
    >= CUT_IN_SCALE kat yakınlaşması (kırp + iyileştir); geçerli bir kesmedir."""
    h = homography(a, b)
    same = bool(h.get("ratio", 0) >= SAME_RATIO and abs(h.get("scale", 0) - 1) < SAME_SCALE
                and h.get("shift", 1) < SAME_SHIFT)
    cut_in = bool(h.get("ratio", 0) >= SAME_RATIO and h.get("scale", 0) >= CUT_IN_SCALE)
    return {**h, "same": same, "cut_in": cut_in}


def camera_motion(first, last, movement: str | None) -> dict:
    """Klibin ilk ve son karesi arasındaki kamera hareketi ve istenen hareket görüldü mü: {"dx", "dy", "zoom",
    "expected", "seen"}. Arka plan homografisinin kayması pan/tilt'i, ölçeği dolly'yi verir."""
    h = homography(first, last)
    zoom = h.get("scale")
    out = {"dx": h.get("dx"), "dy": h.get("dy"), "zoom": zoom, "ratio": h.get("ratio"), "expected": movement}
    rule = MOTION.get(movement or "")
    if rule is None or zoom is None:
        return {**out, "seen": None}
    kind, threshold = rule
    value = {"dx": abs(h.get("dx") or 0), "dy": abs(h.get("dy") or 0), "zoom_in": (zoom or 1) - 1,
             "zoom_out": 1 - (zoom or 1)}[kind]
    return {**out, "seen": bool(value >= threshold)}


KEPT_RATIO = 0.25              # kırp + iyileştir: kompozisyon korundu (ölçek ~1, kayma küçük ve iç oran bunun üstü)


def cut_in_view(previous, frame, rect: tuple = (0.0, 0.0, 1.0, 1.0)) -> dict:
    """Kırp + iyileştir karesi kırpımın kompozisyonunu korudu mu. previous: kırpım (rect tam kare) ya da önceki kare
    (rect: kırpım, kare boyutuna oranla). Bölge büyütülür ve yeni kareyle homografiyle karşılaştırılır. Kıvılcım SH02
    kamerasındaki Bolt'lu karenin 2,03 kat kırpımı, SH02'nin aynı bölgesiyle: ölçek 0,993, kayma 0,007, iç oran 0,32
    (Bolt arka plan şeritlerine giriyor); başka kameralardan kareler iç oran <= 0,03."""
    w, h = previous.size
    x0, y0, x1, y1 = rect
    region = previous.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))).resize(frame.size)
    held = homography(region, frame)
    kept = bool(held.get("ratio", 0) >= KEPT_RATIO and abs(held.get("scale", 0) - 1) < SAME_SCALE
                and held.get("shift", 1) < SAME_SHIFT)
    return {"ratio": held.get("ratio"), "scale": held.get("scale"), "shift": held.get("shift"), "kept": kept}
