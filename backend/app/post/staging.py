"""Hareketli animatik: çekimin sahnelemesi (yerleşim planı) ve onu kareye çeviren çizici. Ağ yok, ücretsiz, deterministik.

Plan (çekim başına JSON, `scenes.staging`; boşsa kurallardan çıkarılır):
  {"version": 1, "source": "rules" | "director",
   "background": {"kind": "paper"} | {"kind": "image", "image": "frame" | "location:<ad>"},
   "layers": [{"id", "image": "frame" | "character:<ad>" | "prop:<ad>", "cutout": bool, "x", "y", "h",
               "keys": [{"t", "x"?, "y"?, "h"?}], "actions": [{"type", "t0", "t1", ...}]}],
   "camera": [{"t", "zoom", "cx", "cy"}], "captions": bool}
x: katmanın yatay ortası, y: alt kenarı (ayaklar), h: boyu; üçü de kare oranıdır (0-1). Zamanlar çekim içi saniyedir.
Hareketler (squash & stretch, DIRECTOR.md §9'dan): breathe, sigh, laugh, droop, lean, hop, shake, tremble.
Kamera: anahtar kareler arasında yumuşak geçişle yakınlaşma (zoom) ve odak (cx, cy).

Görseller kesik kâğıt gibi kullanılır: beyaz zeminli referansın (kitap sayfası, stüdyo görseli) zemini kenarlardan
doldurularak atılır. Zemini beyaz olmayan görsel olduğu gibi (dikdörtgen) yerleşir.

Kukla sürümü (`dummy=True`): görseller düz renkli, adı yazılı siluetlere, arka plan açık griye döner. Video modeline
rehber (hareket referansı) olarak verilmek içindir: konum, hareket, kamera ve zamanlama taşır, görünüş taşımaz.
"""
import math
import subprocess
from itertools import pairwise

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

W, H, FPS = 1280, 720, 24
RENDERER_VERSION = 2              # çizim değişince artar: önbellekteki klipler yeniden çizilir
PAPER = (252, 250, 246)
DUMMY_BG = (226, 228, 232)
CUTOUT_MAX = 1600                 # kesik kâğıt maskesinin en uzun kenarı (büyük sayfalar küçültülerek ayrılır)

# çerçeve -> karakterin kare boyuna oranı ve ayak hizası (kadraj dışına taşan yakın çekimler)
FRAMING = {"extreme_wide": (0.22, 0.80), "wide": (0.4, 0.86), "pov": (0.6, 0.98), "medium": (0.66, 1.0),
           "over_shoulder": (0.72, 1.04), "close_up": (0.95, 1.18), "extreme_close_up": (1.25, 1.35)}
POSITION = {"left": 0.3, "sol": 0.3, "center": 0.5, "orta": 0.5, "middle": 0.5, "right": 0.7, "sağ": 0.7}
EMOTION_ACTION = {"joy": ("laugh", {"freq": 3.6, "amp": 0.010}), "sadness": ("droop", {"sink": 0.015, "tilt": -2.5}),
                  "anger": ("shake", {"amp": 0.006}), "fear": ("tremble", {"amp": 0.004}),
                  "surprise": ("hop", {"amp": 0.04}), "tension": ("breathe", {"period": 1.4, "amp": 0.012}),
                  "disgust": ("lean", {"tilt": -4}), "neutral": ("breathe", {"period": 2.6, "amp": 0.01})}


# ---------- yardımcılar ----------

def ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def keyed(keys: list, t: float, name: str, default: float) -> float:
    ks = [k for k in keys or [] if name in k and "t" in k]
    if not ks:
        return default
    ks.sort(key=lambda k: k["t"])
    if t <= ks[0]["t"]:
        return float(ks[0][name])
    for a, b in pairwise(ks):
        if a["t"] <= t <= b["t"]:
            return float(a[name]) + (float(b[name]) - float(a[name])) * ease((t - a["t"]) / max(1e-6, b["t"] - a["t"]))
    return float(ks[-1][name])


def motion(actions: list, t: float) -> tuple[float, float, float, float, float]:
    """Hareketler -> (dx, dy, sx, sy, derece). dx/dy kare oranı; sx/sy ölçek."""
    dx = dy = rot = 0.0
    sx = sy = 1.0
    for a in actions or []:
        k = a.get("type")
        t0, t1 = float(a.get("t0", 0)), float(a.get("t1", 1e9))
        tt = t
        if not t0 <= t <= t1:
            if k in ("droop", "lean") and t > t1:       # bitiş pozunda kalır
                tt = t1
            else:
                continue
        u, span = tt - t0, max(1e-6, t1 - t0) if t1 < 1e8 else 1.0
        amp = float(a.get("amp", 1.0))
        if k == "breathe":
            s = math.sin(2 * math.pi * u / float(a.get("period", 2.6))) * float(a.get("amp", 0.01))
            sy += s
            sx -= s * 0.5
        elif k == "sigh":                               # hazırlık (yükselir), çöküş, toparlanma
            p = min(1.0, u / span)
            s = 0.04 * math.sin(p * math.pi * 2) * (1 - p) * amp
            sy += s
            sx -= s * 0.6
        elif k == "laugh":
            ph, f = float(a.get("phase", 0)), float(a.get("freq", 4.0))
            env = ease(u / 0.3) * (ease((t1 - t) / 0.3) if t1 < 1e8 else 1.0)
            b = abs(math.sin(2 * math.pi * f * u + ph)) * float(a.get("amp", 0.01)) * env
            dy -= b
            sy += b * 0.5
            rot += math.sin(math.pi * f * u + ph) * float(a.get("tilt", 1.2)) * env
        elif k == "droop":
            p = ease(u / span)
            dy += p * float(a.get("sink", 0.015))
            rot += p * float(a.get("tilt", -2.5))
            sy -= p * 0.02
        elif k == "lean":
            p = ease(u / span)
            rot += p * float(a.get("tilt", 3))
            dx += p * float(a.get("dx", 0.0))
        elif k == "hop":                                # hazırlık (çömelir), sıçrar, iner
            p = min(1.0, u / min(span, 0.8))
            if p < 0.25:
                sy -= 0.05 * math.sin(p / 0.25 * math.pi)
            else:
                dy -= math.sin((p - 0.25) / 0.75 * math.pi) * float(a.get("amp", 0.04))
        elif k == "shake":
            dx += math.sin(2 * math.pi * 7 * u) * float(a.get("amp", 0.006))
        elif k == "tremble":
            dx += math.sin(2 * math.pi * 11 * u) * float(a.get("amp", 0.004))
            dy += math.sin(2 * math.pi * 13 * u + 1) * float(a.get("amp", 0.004)) * 0.5
    return dx, dy, sx, sy, rot


def is_page(im: Image.Image) -> bool:
    """Görselin kenarları büyük ölçüde açık renkli mi (kitap sayfası, beyaz zeminli referans)."""
    a = np.asarray(im.convert("L").resize((160, 90)))
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return float((edge > 225).mean()) > 0.8


def _runs_min(lab: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Her satırdaki kesintisiz maske dizisine, dizideki en küçük etiketi yayar (C sırası, satır satır)."""
    prev = np.zeros_like(mask)
    prev[:, 1:] = mask[:, :-1]
    flat_mask = mask.ravel()
    starts = (mask & ~prev).ravel()[flat_mask]
    idx = np.flatnonzero(starts)
    vals = lab.ravel()[flat_mask]
    mins = np.minimum.reduceat(vals, idx)
    out = lab.copy()
    out.ravel()[flat_mask] = np.repeat(mins, np.diff(np.append(idx, len(vals))))
    return out


def _label(mask: np.ndarray) -> np.ndarray:
    """4-komşulu bağlı bileşen etiketi (yalnızca numpy): 0 = maske dışı, aynı bileşen = aynı etiket.
    Satır ve sütun dizileri boyunca en küçük etiket, değişmeyene dek sırayla yayılır."""
    big = mask.size + 1
    lab = np.where(mask, np.arange(1, mask.size + 1, dtype=np.int32).reshape(mask.shape), np.int32(big))
    if not mask.any():
        return np.zeros(mask.shape, np.int32)
    mask_t = np.ascontiguousarray(mask.T)
    while True:
        new = _runs_min(lab, mask)
        new = np.ascontiguousarray(_runs_min(np.ascontiguousarray(new.T), mask_t).T)
        if np.array_equal(new, lab):
            break
        lab = new
    return np.where(mask, lab, 0)


def _border_labels(lab: np.ndarray) -> np.ndarray:
    return np.setdiff1d(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])), [0])


def cutout(im: Image.Image, threshold: float = 249) -> Image.Image:
    """Beyaz zemini kenarlardan doldurarak atar; kalan şekil(ler) RGBA, sıkı kırpılmış. info["box"]: kaynaktaki yeri."""
    rgb = im.convert("RGB")
    k = min(1.0, CUTOUT_MAX / max(rgb.size))              # maske en çok CUTOUT_MAX pikselde hesaplanır (hız)
    small = rgb if k == 1 else rgb.resize((max(1, round(rgb.width * k)), max(1, round(rgb.height * k))), Image.BOX)
    a = np.asarray(small).astype(np.int16)
    light = (a.min(2) > threshold) & ((a.max(2) - a.min(2)) < 40)
    lab = _label(light)
    fg = ~np.isin(lab, _border_labels(lab))
    m = Image.fromarray((fg * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    fg = np.asarray(m) > 127                                    # açma: kırıntılar ve ince çizgiler gider
    lab2 = _label(fg)
    ids, sizes = np.unique(lab2[fg], return_counts=True)
    if len(ids):
        fg = np.isin(lab2, ids[sizes > sizes.max() * 0.04])
    holes = _label(~fg)                                         # içteki beyaz delikler doldurulur
    fg = np.isin(holes, _border_labels(holes), invert=True) & (holes > 0) | fg
    alpha = Image.fromarray((fg * 255).astype(np.uint8))
    if k != 1:
        alpha = alpha.resize(rgb.size, Image.BILINEAR)
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.2 / k))
    out = rgb.copy()
    out.putalpha(alpha)
    box = out.getbbox() or (0, 0, out.width, out.height)
    res = out.crop(box)
    res.info["box"] = box
    return res


def silhouette(im: Image.Image, label: str) -> Image.Image:
    """Kukla: aynı biçimde düz renk (görselin ortalama rengi, açılmış), koyu kontur ve ad etiketi."""
    al = im.getchannel("A")
    px = np.asarray(im.convert("RGB")).reshape(-1, 3)[np.asarray(al).reshape(-1) > 128]
    base = px.mean(0) if len(px) else np.array([150, 150, 160])
    col = tuple(int(c * 0.55 + 110) for c in base)
    flat = Image.new("RGBA", im.size, col + (255,))
    flat.putalpha(al)
    edge = al.filter(ImageFilter.MaxFilter(5))
    ring = Image.new("RGBA", im.size, (60, 62, 70, 255))
    ring.putalpha(ImageChops.subtract(edge, al))
    flat.alpha_composite(ring)
    d = ImageDraw.Draw(flat)
    from app.engine.procedural import _font
    f = _font(max(14, im.height // 12))
    tw = d.textlength(label, font=f)
    d.text(((im.width - tw) / 2, im.height * 0.35), label, font=f, fill=(30, 32, 38, 255))
    return flat


# ---------- kurallar planı ----------

def camera_keys(movement: str | None, duration: float, focus: tuple[float, float] = (0.5, 0.5)) -> list[dict]:
    fx, fy = focus
    d = max(0.5, duration)
    m = movement or "static"
    if m == "dolly_in":
        return [{"t": 0, "zoom": 1.0, "cx": 0.5, "cy": 0.5}, {"t": d, "zoom": 1.3, "cx": fx, "cy": fy}]
    if m == "dolly_out":
        return [{"t": 0, "zoom": 1.3, "cx": fx, "cy": fy}, {"t": d, "zoom": 1.0, "cx": 0.5, "cy": 0.5}]
    if m in ("pan", "tracking"):
        return [{"t": 0, "zoom": 1.2, "cx": 0.38, "cy": 0.5}, {"t": d, "zoom": 1.2, "cx": 0.62, "cy": 0.5}]
    if m == "tilt":
        return [{"t": 0, "zoom": 1.2, "cx": 0.5, "cy": 0.38}, {"t": d, "zoom": 1.2, "cx": 0.5, "cy": 0.6}]
    if m == "crane":
        return [{"t": 0, "zoom": 1.25, "cx": 0.5, "cy": 0.62}, {"t": d, "zoom": 1.05, "cx": 0.5, "cy": 0.45}]
    if m == "orbit":
        return [{"t": 0, "zoom": 1.15, "cx": 0.44, "cy": 0.5}, {"t": d, "zoom": 1.15, "cx": 0.56, "cy": 0.5}]
    if m == "handheld":
        return [{"t": round(i * d / 6, 2), "zoom": 1.08, "cx": 0.5 + (0.008 if i % 2 else -0.008),
                 "cy": 0.5 + (0.006 if i % 3 else -0.005)} for i in range(7)]
    return [{"t": 0, "zoom": 1.0, "cx": 0.5, "cy": 0.5}]


def act(emotion: str | None, duration: float, i: int = 0) -> list[dict]:
    kind, params = EMOTION_ACTION.get(emotion or "neutral", EMOTION_ACTION["neutral"])
    out = [{"type": kind, "t0": round(0.2 + 0.25 * i, 2), "t1": round(max(0.6, duration - 0.2), 2), **params}]
    if kind == "laugh":
        out[0]["phase"] = round(0.7 * i, 2)
        out[0]["freq"] = round(params["freq"] + 0.6 * (i % 3), 2)
    if kind != "breathe":
        out.append({"type": "breathe", "period": 2.6 + 0.3 * i, "amp": 0.008})
    return out


def rules_plan(info: dict) -> dict:
    """info: {"duration_s", "camera": {shot, movement}, "emotion", "frame": (w, h, sayfa mı, kutu) | None,
    "cast": [{"name", "position", "has_image"}], "location": ad | None}. Ağ ve veritabanı yok."""
    d = float(info.get("duration_s") or 5.0)
    cam = info.get("camera") or {}
    emotion = info.get("emotion")
    frame = info.get("frame")
    layers: list[dict] = []
    if frame:
        fw, fh, page, box = frame
        if page and box:
            x0, y0, x1, y1 = box
            names = " · ".join(c["name"] for c in info.get("cast") or []) or "Kare"
            layers.append({"id": names, "image": "frame", "cutout": True, "x": round((x0 + x1) / 2 / fw, 4),
                           "y": round(y1 / fh, 4), "h": round((y1 - y0) / fh, 4), "actions": act(emotion, d)})
            bg = {"kind": "paper"}
        else:
            bg = {"kind": "image", "image": "frame"}
        focus = (layers[0]["x"], max(0.3, layers[0]["y"] - layers[0]["h"] * 0.6)) if layers else (0.5, 0.45)
        return {"version": 1, "source": "rules", "background": bg, "layers": layers,
                "camera": camera_keys(cam.get("movement"), d, focus), "captions": True}
    h, foot = FRAMING.get(cam.get("shot") or "medium", FRAMING["medium"])
    cast = [c for c in info.get("cast") or []]
    n = len(cast)
    for i, c in enumerate(cast):
        x = POSITION.get(str(c.get("position") or "").lower(), (i + 1) / (n + 1))
        layers.append({"id": c["name"], "image": f"character:{c['name']}" if c.get("has_image") else "dummy",
                       "cutout": True, "x": round(x, 4), "y": foot, "h": h, "actions": act(emotion, d, i)})
    bg = {"kind": "image", "image": f"location:{info['location']}"} if info.get("location") else {"kind": "paper"}
    focus = (layers[0]["x"], max(0.3, foot - h * 0.75)) if layers else (0.5, 0.45)
    return {"version": 1, "source": "rules", "background": bg, "layers": layers,
            "camera": camera_keys(cam.get("movement"), d, focus), "captions": True}


# ---------- çizici ----------

class Stage:
    """Planı ve yerel görsel dosyalarını alır; frame(t) bir kare (RGB) döner."""

    def __init__(self, plan: dict, images: dict, duration_s: float, dummy: bool = False,
                 captions: list | None = None, label: str = ""):
        self.plan, self.duration, self.dummy, self.label = plan, float(duration_s), dummy, label
        self.captions = captions if plan.get("captions", True) else []
        bg = plan.get("background") or {}
        path = images.get(bg.get("image")) if bg.get("kind") == "image" else None
        if path:
            im = Image.open(path).convert("RGB")
            r = max(W / im.width, H / im.height)
            im = im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)
            self.bg = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
            if dummy:                                   # kukla: yalnızca çizgiler (kompozisyon), renk ve doku yok
                from PIL import ImageOps
                edges = ImageOps.autocontrast(self.bg.convert("L").filter(ImageFilter.FIND_EDGES), cutoff=2)
                self.bg = Image.composite(Image.new("RGB", (W, H), (120, 124, 134)), Image.new("RGB", (W, H), DUMMY_BG),
                                          edges.point(lambda v: 255 if v > 90 else 0))
        else:
            self.bg = Image.new("RGB", (W, H), DUMMY_BG if dummy else PAPER)
            if dummy:
                g = ImageDraw.Draw(self.bg)
                for x in range(0, W, 80):
                    g.line([(x, 0), (x, H)], fill=(212, 214, 219))
                for y in range(0, H, 80):
                    g.line([(0, y), (W, y)], fill=(212, 214, 219))
        self.sprites = {}
        for L in plan.get("layers", []):
            self.sprites[L["id"]] = self._sprite(L, images)

    def _sprite(self, L: dict, images: dict) -> Image.Image:
        name = L.get("id", "")
        path = images.get(L.get("image"))
        if path:
            im = Image.open(path)
            if L.get("cutout", True) and is_page(im):
                im = cutout(im)
            else:
                im = im.convert("RGBA")
        else:                                           # görseli yok: oval kukla
            im = Image.new("RGBA", (300, 600), (0, 0, 0, 0))
            dd = ImageDraw.Draw(im)
            dd.ellipse((70, 0, 230, 160), fill=(150, 150, 165, 255))
            dd.rounded_rectangle((40, 150, 260, 600), 90, fill=(150, 150, 165, 255))
        if self.dummy or not path:
            im = silhouette(im, name)
        return im

    def frame(self, t: float) -> Image.Image:
        world = self.bg.copy().convert("RGBA")
        for L in self.plan.get("layers", []):
            if not float(L.get("t0", 0)) <= t <= float(L.get("t1", 1e9)):
                continue
            im = self.sprites[L["id"]]
            keys = L.get("keys", [])
            x, y, h = keyed(keys, t, "x", L.get("x", 0.5)), keyed(keys, t, "y", L.get("y", 1.0)), keyed(keys, t, "h", L.get("h", 0.6))
            dx, dy, sx, sy, rot = motion(L.get("actions", []), t)
            th = max(2, round(h * H * sy))
            tw = max(2, round(im.width * (h * H / im.height) * sx))
            sp = im.resize((tw, th), Image.BILINEAR)
            if abs(rot) > 0.05:
                sp = sp.rotate(rot, resample=Image.BICUBIC, expand=True, center=(tw / 2, th))
            world.paste(sp, (round((x + dx) * W - sp.width / 2), round((y + dy) * H - sp.height)), sp)
        cam = self.plan.get("camera", [])
        z = max(1.0, keyed(cam, t, "zoom", 1.0))
        cx, cy = keyed(cam, t, "cx", 0.5) * W, keyed(cam, t, "cy", 0.5) * H
        vw, vh = W / z, H / z
        x0, y0 = min(max(0.0, cx - vw / 2), W - vw), min(max(0.0, cy - vh / 2), H - vh)
        out = world.convert("RGB")
        if z > 1.0:
            out = out.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + vw, y0 + vh))
        self._overlay(out, t)
        return out

    def _overlay(self, im: Image.Image, t: float) -> None:
        from app.engine.procedural import _font
        d = ImageDraw.Draw(im, "RGBA")
        for c in self.captions or []:
            if not c["t0"] <= t <= c["t1"]:
                continue
            a = max(0.0, min(1.0, (t - c["t0"]) / 0.2, (c["t1"] - t) / 0.2))
            f = _font(28)
            import textwrap
            lines = textwrap.wrap(c["text"], 60)[:3]
            wmax = max(d.textlength(ln, font=f) for ln in lines)
            top = H - 36 - 38 * len(lines) - 16
            d.rounded_rectangle(((W - wmax) / 2 - 20, top, (W + wmax) / 2 + 20, H - 36), 12, fill=(20, 20, 24, int(165 * a)))
            for i, ln in enumerate(lines):
                d.text(((W - d.textlength(ln, font=f)) / 2, top + 10 + i * 38), ln, font=f, fill=(255, 255, 255, int(255 * a)))
        if self.label:
            f = _font(18)
            tag = f"{self.label}  ·  {t:4.1f}/{self.duration:.1f} sn"
            d.rounded_rectangle((14, 14, 26 + d.textlength(tag, font=f), 44), 8, fill=(20, 20, 24, 170))
            d.text((20, 18), tag, font=f, fill=(232, 120, 58, 255))


def render(plan: dict, images: dict, duration_s: float, out: str, dummy: bool = False,
           captions: list | None = None, label: str = "") -> str:
    """Planı sessiz MP4'e çizer (1280×720, 24 fps)."""
    st = Stage(plan, images, duration_s, dummy, captions, label)
    n = max(1, round(float(duration_s) * FPS))
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                           "-pix_fmt", "yuv420p", "-an", out], stdin=subprocess.PIPE)
    try:
        for i in range(n):
            ff.stdin.write(st.frame(i / FPS).tobytes())
    finally:
        ff.stdin.close()
        code = ff.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg çıkış kodu {code}")
    return out


# ---------- doğrulama ----------

ACTIONS = ("breathe", "sigh", "laugh", "droop", "lean", "hop", "shake", "tremble")


def validate(plan) -> list[str]:
    """Yönetmenin ya da bir modelin yazdığı planın Türkçe hataları (boş liste: geçerli)."""
    errs = []
    if not isinstance(plan, dict):
        return ["Plan bir JSON nesnesi olmalı."]
    bg = plan.get("background") or {"kind": "paper"}
    if bg.get("kind") not in ("paper", "image"):
        errs.append("background.kind 'paper' ya da 'image' olmalı.")
    layers = plan.get("layers") or []
    if not isinstance(layers, list) or len(layers) > 12:
        errs.append("layers en çok 12 öğelik bir liste olmalı.")
        layers = []
    ids = set()
    for i, L in enumerate(layers):
        if not isinstance(L, dict) or not L.get("id"):
            errs.append(f"layers[{i}]: 'id' gerekli.")
            continue
        if L["id"] in ids:
            errs.append(f"layers[{i}]: '{L['id']}' iki kez kullanılmış.")
        ids.add(L["id"])
        for k in ("x", "y", "h"):
            v = L.get(k)
            if v is not None and not (isinstance(v, (int, float)) and -1 <= v <= 3):
                errs.append(f"{L['id']}: {k} -1 ile 3 arasında bir sayı olmalı.")
        for a in L.get("actions") or []:
            if not isinstance(a, dict) or a.get("type") not in ACTIONS:
                errs.append(f"{L['id']}: bilinmeyen hareket ({a.get('type') if isinstance(a, dict) else a}). "
                            f"Kullanılabilenler: {', '.join(ACTIONS)}.")
    for k in plan.get("camera") or []:
        if not isinstance(k, dict) or "t" not in k:
            errs.append("camera: her anahtar karede 't' gerekli.")
            break
        if float(k.get("zoom", 1)) < 1 or float(k.get("zoom", 1)) > 4:
            errs.append("camera: zoom 1 ile 4 arasında olmalı.")
            break
    return errs
