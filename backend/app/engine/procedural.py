"""Ücretsiz, yerel prosedürel render motoru.

Gerçek bir yapay zekâ modeli değildir; ama sahne parametrelerini görünür biçimde yansıtır:
  duygu       -> renk paleti ve atmosfer (öfke, gerilim ve korkuda kor parçacıkları)
  kamera      -> çekim ölçeği (zoom) ve hareket (pan, dolly, tracking, el kamerası...)
  karakterler -> adlı siluetler; aksiyon yüksekse koşarlar
  seed        -> her varyasyonda farklı dekor, ışık ve kamera sapması
Böylece Quality Gate, Rafine Havuz, montaj ve panel para harcamadan gerçek görüntülerle denenir.
"""
import math
import os
import random
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WORLD_SCALE = 1.6       # dünya çözünürlüğü / çıktı çözünürlüğü
PARALLAX_BACK = 0.35    # arka katman kamera hareketinin bu oranında kayar
RUN_THRESHOLD = 0.6     # aksiyon bunun üstündeyse karakterler koşar

PALETTES = {  # (gökyüzü, ufuk, zemin, vurgu)
    "neutral":  ((52, 64, 80), (148, 156, 162), (38, 42, 48), (210, 214, 220)),
    "joy":      ((70, 140, 214), (250, 214, 150), (64, 104, 58), (255, 240, 200)),
    "anger":    ((58, 10, 6), (214, 82, 30), (28, 10, 8), (255, 170, 60)),
    "fear":     ((12, 16, 32), (64, 42, 70), (12, 12, 18), (120, 160, 230)),
    "sadness":  ((36, 46, 62), (118, 128, 142), (30, 34, 42), (170, 190, 215)),
    "surprise": ((84, 56, 134), (240, 170, 120), (46, 36, 66), (255, 220, 160)),
    "disgust":  ((46, 56, 18), (150, 160, 70), (30, 36, 16), (200, 220, 90)),
    "tension":  ((26, 26, 36), (168, 96, 40), (18, 18, 22), (240, 150, 70)),
}
EMBER_EMOTIONS = {"anger", "tension", "fear"}

SHOT_ZOOM = {"extreme_wide": 1.0, "wide": 1.15, "pov": 1.4, "medium": 1.55, "over_shoulder": 1.7,
             "close_up": 2.3, "extreme_close_up": 3.0}

# Ortam kayması: "durağan" çekim bile gerçek görüntü gibi hafifçe nefes alır. Aksi halde hiçbir şeyin hareket
# etmediği klip Quality Gate'in freezedetect filtresine takılırdı; donmuş klip yalnızca render_defect ile üretilir.
AMBIENT_ZOOM = 0.025    # zoom salınımı (oran)
AMBIENT_PAN = 0.012     # kayma (dünya boyutuna oranla)
AMBIENT_HZ = 0.45       # salınım hızı (devir/sn)

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


@lru_cache(maxsize=64)
def _font(size: int):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)             # Pillow >= 10.1: ölçeklenebilir yedek yazı tipi
    except TypeError:
        return ImageFont.load_default()


def _jitter(rgb, rng, amount=14):
    return tuple(int(min(255, max(0, c + rng.randint(-amount, amount)))) for c in rgb)


def _lerp(a, b, t):
    return a + (b - a) * t


def _ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * t)


# ---------- dünya ----------

@dataclass
class World:
    back: Image.Image       # gökyüzü, uzak fabrikalar, ufuk parıltısı
    front: Image.Image      # kirişler, köprü, zemin (RGBA)
    horizon_y: int
    width: int
    height: int


def _build_world(rng: random.Random, width: int, height: int, palette) -> World:
    sky, horizon, ground, light = palette
    horizon_y = int(height * rng.uniform(0.58, 0.68))

    # Arka katman
    t = np.linspace(0, 1, height)[:, None, None]
    gradient = (np.array(sky) * (1 - t) + np.array(horizon) * t).astype(np.uint8)
    back = Image.fromarray(np.repeat(gradient, width, axis=1), "RGB")
    d = ImageDraw.Draw(back)
    far = tuple(int(_lerp(c, h, 0.55)) for c, h in zip(sky, horizon))
    x = 0
    while x < width:                                           # uzak fabrika siluetleri ve bacalar
        w = rng.randint(width // 40, width // 14)
        h = rng.randint(height // 10, height // 3)
        d.rectangle([x, horizon_y - h, x + w, horizon_y], fill=far)
        if rng.random() < 0.35:
            cw = max(4, w // 5)
            cx = x + rng.randint(0, max(1, w - cw))
            ch = rng.randint(height // 8, height // 4)
            d.rectangle([cx, horizon_y - h - ch, cx + cw, horizon_y - h], fill=far)
        x += w + rng.randint(0, width // 60)
    glow_mask = Image.new("L", (width, height), 0)             # ufuk parıltısı
    gx = int(width * rng.uniform(0.2, 0.8))
    ImageDraw.Draw(glow_mask).ellipse([gx - height // 2, horizon_y - height // 3, gx + height // 2,
                                       horizon_y + height // 4], fill=70)
    back = Image.composite(Image.new("RGB", (width, height), light), back,
                           glow_mask.filter(ImageFilter.GaussianBlur(height // 8)))

    # Ön katman
    front = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(front)
    steel = tuple(int(c * 0.6) for c in ground) + (255,)
    brace = max(2, height // 160)
    for _ in range(rng.randint(3, 6)):                         # kirişler: dikmeler ve çapraz bağlantılar
        px = rng.randint(0, width)
        pw = rng.randint(height // 30, height // 14)
        top = int(horizon_y - height * rng.uniform(0.25, 0.55))
        d.rectangle([px, top, px + pw, horizon_y + 10], fill=steel)
        for k in range(3):
            y0 = top + (horizon_y - top) * k // 3
            d.line([(px - pw * 2, y0 + (horizon_y - top) // 3), (px + pw * 3, y0)], fill=steel, width=brace)
    deck_y = int(horizon_y - height * rng.uniform(0.3, 0.45))  # köprü tabliyesi, korkuluk ve kafes
    d.rectangle([0, deck_y, width, deck_y + max(4, height // 60)], fill=steel)
    span = max(40, width // 18)
    for i in range(0, width, span):
        d.line([(i, deck_y), (i + span // 2, deck_y - height // 14), (i + span, deck_y)], fill=steel, width=brace)
    d.line([(0, deck_y - height // 14), (width, deck_y - height // 14)], fill=steel, width=brace)
    d.rectangle([0, horizon_y, width, height], fill=tuple(ground) + (255,))     # zemin
    lines = tuple(int(c * 1.25) for c in ground) + (255,)
    for i in range(0, width, max(8, width // 24)):                              # perspektif çizgileri
        d.line([(i, horizon_y), (width // 2 + (i - width // 2) * 3, height)], fill=lines, width=2)
    return World(back, front, horizon_y, width, height)


def _draw_figure(d, x, y, h, color, rim, name, font, phase):
    head, body_w = h * 0.16, h * 0.28
    top = y - h + math.sin(phase) * h * 0.015
    d.rounded_rectangle([x - body_w / 2, top + head * 1.05, x + body_w / 2, y], radius=int(body_w / 2.2), fill=color)
    d.ellipse([x - head / 2, top, x + head / 2, top + head], fill=color)
    d.arc([x - head / 2, top, x + head / 2, top + head], 200, 340, fill=rim, width=max(2, int(h / 90)))
    swing = math.sin(phase * 2) * body_w * 0.25
    for side in (-1, 1):
        d.line([(x + side * body_w / 4, y), (x + side * body_w / 4 - side * swing, y + h * 0.04)], fill=color,
               width=max(3, int(body_w / 4)))
    if name:
        d.text((x - d.textlength(name, font=font) / 2, top - font.size * 1.6), name, font=font, fill=rim)


# ---------- kamera ----------

def _camera_at(movement: str, e: float, t: float, sec: float, base: dict) -> tuple[float, float, float]:
    """Kameranın (zoom, merkez x, merkez y) değeri. e: yumuşatılmış ilerleme 0..1, t: doğrusal ilerleme."""
    z, cx, cy = base["z"], base["cx"], base["cy"]
    wh, pan = base["wh"], base["pan"]
    if movement == "dolly_in":
        z *= 1 + 0.35 * e
    elif movement == "dolly_out":
        z *= 1.35 - 0.35 * e
    elif movement == "pan":
        cx += _lerp(-pan, pan, e)
    elif movement == "tilt":
        cy += _lerp(-wh * 0.12, wh * 0.05, e)
    elif movement == "crane":
        cy += _lerp(wh * 0.1, -wh * 0.12, e)
        z *= 1.15 - 0.15 * e
    elif movement == "orbit":
        cx += _lerp(pan, -pan, e)
    cx += base["run"] * t                                      # koşan karakterleri izler
    phase = base["drift"]
    z = max(z * (1 + AMBIENT_ZOOM * math.sin(2 * math.pi * AMBIENT_HZ * sec + phase)), 1.0)
    cx += base["ww"] * AMBIENT_PAN * math.sin(2 * math.pi * AMBIENT_HZ * 0.7 * sec + phase * 1.3)
    cy += wh * AMBIENT_PAN * 0.6 * math.sin(2 * math.pi * AMBIENT_HZ * 0.5 * sec + phase * 0.7)
    return z, cx, cy


@contextmanager
def _encoder(out_path: str, width: int, height: int, fps: int):
    """Ham RGB kareleri alan FFmpeg süreci. Her durumda toplanır (zombi kalmaz); hata kodu RuntimeError olur."""
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        yield proc.stdin
    except BrokenPipeError:
        pass                                                   # ffmpeg erken çıktı; asıl neden aşağıda stderr'de
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode(errors="replace").strip()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg kodlama hatası: {err[-300:]}")


# ---------- klip ----------

def render_clip(out_path: str, seed: int, width: int, height: int, duration: float = 5.0, fps: int = 24,
                emotion: str = "neutral", camera: dict | None = None, characters: list[str] | None = None,
                action: float = 0.0, label: str = "") -> str:
    width, height = width + width % 2, height + height % 2      # yuv420p çift boyut ister
    rng = random.Random(seed)
    camera = camera or {}
    palette = tuple(_jitter(c, rng) for c in PALETTES.get(emotion, PALETTES["neutral"]))
    wh = int(height * WORLD_SCALE)
    ww = int(wh * width / height * 1.7)                        # pan ve takip için görüş alanından geniş
    world = _build_world(rng, ww, wh, palette)

    names = list(characters or [])[:4]
    fig_h = wh * 0.42
    slots = [ww * (0.5 + (i - (len(names) - 1) / 2) * 0.13 + rng.uniform(-0.02, 0.02)) for i in range(len(names))]
    running = action > RUN_THRESHOLD
    movement = camera.get("movement", "static")
    base = {
        # Görüş penceresi dünyadan büyük olamaz (z < 1 ise PIL negatif kutu hatası verir): alt sınır 1 + ortam zoomu.
        "z": max(SHOT_ZOOM.get(camera.get("shot", "medium"), 1.55) * rng.uniform(0.95, 1.05), 1.0 + AMBIENT_ZOOM),
        "cx": slots[0] if slots else ww / 2,
        "cy": world.horizon_y - fig_h * 0.55 if names else world.horizon_y - wh * 0.15,
        "wh": wh, "ww": ww, "pan": ww * rng.uniform(0.06, 0.12),
        "run": ww * 0.12 * action if running else 0.0, "drift": rng.uniform(0, 2 * math.pi),
    }
    shake = (0.02 if movement == "handheld" else 0.0) + (0.015 if running else 0.0)

    label_font = _font(max(12, int(height / 38)))
    name_size = max(14, int(wh / 34))
    vignette = Image.new("L", (width, height), 0)
    ImageDraw.Draw(vignette).ellipse([-width * 0.15, -height * 0.25, width * 1.15, height * 1.25], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(height // 6))
    black = Image.new("RGB", (width, height), (0, 0, 0))
    embers = ([(rng.random(), rng.random(), rng.uniform(0.3, 1.0)) for _ in range(40)]
              if emotion in EMBER_EMOTIONS else [])

    frames = max(1, int(round(duration * fps)))
    jx = jy = 0.0
    with _encoder(out_path, width, height, fps) as stdin:
        for f in range(frames):
            t = f / max(1, frames - 1)
            z, cx, cy = _camera_at(movement, _ease(t), t, f / fps, base)
            if shake:                                          # yumuşatılmış titreme
                jx = jx * 0.85 + rng.uniform(-1, 1) * shake * wh * 0.15
                jy = jy * 0.85 + rng.uniform(-1, 1) * shake * wh * 0.1
            view_h = wh / z
            view_w = view_h * width / height

            def window(parallax):
                x0 = min(max(cx * parallax + (ww / 2) * (1 - parallax) - view_w / 2 + jx, 0), max(0, ww - view_w))
                y0 = min(max(cy - view_h / 2 + jy, 0), wh - view_h)
                return x0, y0

            bx, by = window(-0.2 if movement == "orbit" else PARALLAX_BACK)
            frame = world.back.resize((width, height), Image.BILINEAR, box=(bx, by, bx + view_w, by + view_h))
            fx, fy = window(1.0)
            near = world.front.resize((width, height), Image.BILINEAR, box=(fx, fy, fx + view_w, fy + view_h))
            frame.paste(near, (0, 0), near)

            d = ImageDraw.Draw(frame)
            scale = height / view_h
            color = tuple(int(c * 0.35) for c in palette[2])
            for i, (slot, name) in enumerate(zip(slots, names)):
                wx = slot + base["run"] * t + math.sin(t * 6 + i) * ww * 0.004
                _draw_figure(d, (wx - fx) * scale, (world.horizon_y + fig_h * 0.05 - fy) * scale, fig_h * scale,
                             color, palette[3], name, _font(max(12, int(name_size * scale / WORLD_SCALE))),
                             t * 18 * (1 + 2 * action) + i)
            r = max(1, height // 260)
            for ex, ey, speed in embers:
                yy = ((ey - t * speed * 0.6) % 1.0) * height
                xx = (ex * width + math.sin(t * 10 + ex * 30) * 8) % width
                d.ellipse([xx - r, yy - r, xx + r, yy + r], fill=palette[3])
            frame = Image.composite(frame, black, vignette)
            if label:
                ImageDraw.Draw(frame).text((height // 40, height - label_font.size * 1.8), label,
                                           font=label_font, fill=(235, 235, 235))
            stdin.write(frame.tobytes())
    return out_path


def render_defect(out_path: str, kind: str, seed: int, width: int, height: int, duration: float = 5.0,
                  fps: int = 24) -> str:
    """Quality Gate'i sınamak için bilerek bozuk klip: "black" (siyah) ya da "freeze" (donmuş, tek düz renk)."""
    if kind == "black":
        source = f"color=c=black:size={width}x{height}:rate={fps}"
    elif kind == "freeze":
        grey = random.Random(seed).randint(0x60, 0xA0)         # orta gri: siyah eşiğinin güvenle üstünde
        source = f"color=c=0x{grey:02x}{grey:02x}{grey:02x}:size={width}x{height}:rate={fps}"
    else:
        raise ValueError(f"bilinmeyen kusur türü: {kind}")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source,
                    "-t", str(duration), "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", out_path], check=True)
    return out_path
