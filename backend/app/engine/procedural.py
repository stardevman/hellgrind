"""Ücretsiz, yerel prosedürel render motoru.

Gerçek bir yapay zekâ modeli değildir; ama sahne parametrelerini görünür biçimde yansıtır:
  duygu  -> renk paleti ve atmosfer (köz, sis)
  kamera -> çekim ölçeği (geniş/orta/yakın) ve hareket (pan, dolly, tracking, el kamerası...)
  karakterler -> adlı siluetler, aksiyon skoru yüksekse koşarlar
  seed   -> her varyasyonda farklı dekor, ışık ve kamera sapması
Böylece Quality Gate, Rafine Havuz, montaj ve panel, para harcamadan gerçek görüntülerle denenir.
"""
import math
import os
import random
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PALETTES = {  # (gökyüzü üst, ufuk, zemin, vurgu ışığı)
    "neutral":  ((52, 64, 80), (148, 156, 162), (38, 42, 48), (210, 214, 220)),
    "fear":     ((12, 16, 32), (64, 42, 70), (12, 12, 18), (120, 160, 230)),
    "anger":    ((58, 10, 6), (214, 82, 30), (28, 10, 8), (255, 170, 60)),
    "joy":      ((70, 140, 214), (250, 214, 150), (64, 104, 58), (255, 240, 200)),
    "sadness":  ((36, 46, 62), (118, 128, 142), (30, 34, 42), (170, 190, 215)),
    "surprise": ((84, 56, 134), (240, 170, 120), (46, 36, 66), (255, 220, 160)),
    "disgust":  ((46, 56, 18), (150, 160, 70), (30, 36, 16), (200, 220, 90)),
    "tension":  ((26, 26, 36), (168, 96, 40), (18, 18, 22), (240, 150, 70)),
}
SHOT_ZOOM = {"extreme_wide": 1.0, "wide": 1.15, "pov": 1.4, "medium": 1.55, "over_shoulder": 1.7,
             "close_up": 2.3, "extreme_close_up": 3.0}
EMBER_EMOTIONS = {"anger", "tension", "fear"}
# Ortam kayması: "durağan" çekim bile gerçek görüntü gibi hafifçe nefes alır. Aksi halde hiçbir şeyin
# hareket etmediği klip Quality Gate'in freezedetect filtresine takılır; donmuş klip yalnızca render_defect ile üretilir.
AMBIENT_ZOOM = 0.025    # zoom salınımı (oran)
AMBIENT_PAN = 0.012     # yatay kayma (dünya genişliğine oranla)
AMBIENT_HZ = 0.45       # salınım hızı (devir/sn)
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]


def _font(size: int):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _jitter(rgb, rng, amt=14):
    return tuple(int(min(255, max(0, c + rng.randint(-amt, amt)))) for c in rgb)


def _lerp(a, b, t):
    return a + (b - a) * t


def _ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _build_world(rng, ww, wh, pal):
    sky_top, horizon, ground_c, light = pal
    horizon_y = int(wh * rng.uniform(0.58, 0.68))

    # Arka katman: gökyüzü + uzak fabrika silueti (parallax yavaş)
    t = np.linspace(0, 1, wh)[:, None, None]
    grad = (np.array(sky_top) * (1 - t) + np.array(horizon) * t).astype(np.uint8)
    back = Image.fromarray(np.repeat(grad, ww, axis=1), "RGB")
    d = ImageDraw.Draw(back)
    far = tuple(int(_lerp(c, h, 0.55)) for c, h in zip(sky_top, horizon))
    x = 0
    while x < ww:
        w = rng.randint(ww // 40, ww // 14)
        h = rng.randint(wh // 10, wh // 3)
        d.rectangle([x, horizon_y - h, x + w, horizon_y], fill=far)
        if rng.random() < 0.35:   # baca
            cw = max(4, w // 5)
            cx = x + rng.randint(0, max(1, w - cw))
            ch = rng.randint(wh // 8, wh // 4)
            d.rectangle([cx, horizon_y - h - ch, cx + cw, horizon_y - h], fill=far)
        x += w + rng.randint(0, ww // 60)
    glow = Image.new("RGB", (ww, wh), light)
    mask = Image.new("L", (ww, wh), 0)
    gx = int(ww * rng.uniform(0.2, 0.8))
    ImageDraw.Draw(mask).ellipse([gx - wh // 2, horizon_y - wh // 3, gx + wh // 2, horizon_y + wh // 4], fill=70)
    back = Image.composite(glow, back, mask.filter(ImageFilter.GaussianBlur(wh // 8)))

    # Ön katman: yakın yapılar + zemin (parallax hızlı)
    front = Image.new("RGBA", (ww, wh), (0, 0, 0, 0))
    d = ImageDraw.Draw(front)
    near = tuple(int(c * 0.6) for c in ground_c) + (255,)
    for _ in range(rng.randint(3, 6)):
        px = rng.randint(0, ww)
        pw = rng.randint(wh // 30, wh // 14)
        d.rectangle([px, int(horizon_y - wh * rng.uniform(0.25, 0.55)), px + pw, horizon_y + 10], fill=near)
    beam_y = int(horizon_y - wh * rng.uniform(0.3, 0.45))
    d.rectangle([0, beam_y, ww, beam_y + max(4, wh // 60)], fill=near)       # kiriş / köprü
    d.rectangle([0, horizon_y, ww, wh], fill=tuple(ground_c) + (255,))
    for i in range(0, ww, max(8, ww // 24)):                                 # zemin perspektif çizgileri
        d.line([(i, horizon_y), (ww // 2 + (i - ww // 2) * 3, wh)], fill=tuple(int(c * 1.25) for c in ground_c) + (255,), width=2)
    return back, front, horizon_y


def _draw_figure(d, x, y, h, color, rim, name, font, phase):
    head = h * 0.16
    bw = h * 0.28
    bob = math.sin(phase) * h * 0.015
    top = y - h + bob
    d.rounded_rectangle([x - bw / 2, top + head * 1.05, x + bw / 2, y], radius=int(bw / 2.2), fill=color)
    d.ellipse([x - head / 2, top, x + head / 2, top + head], fill=color)
    d.arc([x - head / 2, top, x + head / 2, top + head], 200, 340, fill=rim, width=max(2, int(h / 90)))
    leg = math.sin(phase * 2) * bw * 0.25
    d.line([(x - bw / 4, y), (x - bw / 4 + leg, y + h * 0.04)], fill=color, width=max(3, int(bw / 4)))
    d.line([(x + bw / 4, y), (x + bw / 4 - leg, y + h * 0.04)], fill=color, width=max(3, int(bw / 4)))
    if name:
        tw = d.textlength(name, font=font)
        d.text((x - tw / 2, top - font.size * 1.6), name, font=font, fill=rim)


def render_clip(out_path: str, *, seed: int, width: int, height: int, duration: float = 5.0, fps: int = 24,
                emotion: str = "neutral", camera: dict | None = None, characters: list[str] | None = None,
                action: float = 0.0, label: str = "") -> str:
    rng = random.Random(seed)
    camera = camera or {}
    pal = tuple(_jitter(c, rng) for c in PALETTES.get(emotion, PALETTES["neutral"]))
    S = 1.6                                    # dünya çözünürlüğü / çıktı
    wh = int(height * S)
    ww = int(wh * 16 / 9 * 1.7)
    back, front, horizon_y = _build_world(rng, ww, wh, pal)

    names = list(characters or [])[:4]
    fig_h = wh * 0.42
    slots = [ww * (0.5 + (i - (len(names) - 1) / 2) * 0.13 + rng.uniform(-0.02, 0.02)) for i in range(len(names))]
    focus_x = slots[0] if slots else ww / 2
    focus_y = horizon_y - fig_h * 0.55 if names else horizon_y - wh * 0.15

    shot = camera.get("shot", "medium")
    move = camera.get("movement", "static")
    # Görüş penceresi dünyadan büyük olamaz (z < 1 ise PIL "box offset can't be negative" ile çöker);
    # extreme_wide (1.0) seed sapması ve ortam kaymasıyla 1'in altına düşmesin diye alt sınır konur.
    z0 = max(SHOT_ZOOM.get(shot, 1.5) * rng.uniform(0.95, 1.05), 1.0 + AMBIENT_ZOOM)
    running = action > 0.6                     # "aksiyon 0.6 üstündeyse koşarlar"
    run_speed = ww * 0.12 * action if running else 0.0
    pan_span = ww * rng.uniform(0.06, 0.12)
    shake = (0.02 if move == "handheld" else 0.0) + (0.015 if running else 0.0)

    font = _font(max(12, int(height / 38)))
    name_font = _font(max(14, int(wh / 34)))
    vignette = Image.new("L", (width, height), 0)
    ImageDraw.Draw(vignette).ellipse([-width * 0.15, -height * 0.25, width * 1.15, height * 1.25], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(height // 6))
    black = Image.new("RGB", (width, height), (0, 0, 0))
    embers = [(rng.random(), rng.random(), rng.uniform(0.3, 1.0)) for _ in range(40)] if emotion in EMBER_EMOTIONS else []
    drift_phase = rng.uniform(0, 2 * math.pi)

    n = max(1, int(round(duration * fps)))
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    jx = jy = 0.0
    try:
        for f in range(n):
            t = f / max(1, n - 1)
            e = _ease(t)
            z, cx, cy = z0, focus_x, focus_y
            if move == "dolly_in":
                z = z0 * (1 + 0.35 * e)
            elif move == "dolly_out":
                z = z0 * (1.35 - 0.35 * e)
            elif move == "pan":
                cx = focus_x + _lerp(-pan_span, pan_span, e)
            elif move == "tilt":
                cy = focus_y + _lerp(-wh * 0.12, wh * 0.05, e)
            elif move == "crane":
                cy = focus_y + _lerp(wh * 0.1, -wh * 0.12, e); z = z0 * (1.15 - 0.15 * e)
            elif move == "orbit":
                cx = focus_x + _lerp(pan_span, -pan_span, e)
            run = run_speed * t
            if move == "tracking" or run:
                cx += run
            sec = f / fps
            z = max(z * (1 + AMBIENT_ZOOM * math.sin(2 * math.pi * AMBIENT_HZ * sec + drift_phase)), 1.0)
            cx += ww * AMBIENT_PAN * math.sin(2 * math.pi * AMBIENT_HZ * 0.7 * sec + drift_phase * 1.3)
            cy += wh * AMBIENT_PAN * 0.6 * math.sin(2 * math.pi * AMBIENT_HZ * 0.5 * sec + drift_phase * 0.7)
            if shake:
                jx = jx * 0.85 + rng.uniform(-1, 1) * shake * wh * 0.15
                jy = jy * 0.85 + rng.uniform(-1, 1) * shake * wh * 0.1
            vh = wh / z
            vw = vh * width / height

            def window(par):
                x0 = min(max(cx * par + (ww / 2) * (1 - par) - vw / 2 + jx, 0), ww - vw)
                y0 = min(max(cy - vh / 2 + jy, 0), wh - vh)
                return x0, y0

            bx, by = window(0.35 if move != "orbit" else -0.2)
            frame = back.resize((width, height), Image.BILINEAR, box=(bx, by, bx + vw, by + vh))
            fx, fy = window(1.0)
            fl = front.resize((width, height), Image.BILINEAR, box=(fx, fy, fx + vw, fy + vh))
            frame.paste(fl, (0, 0), fl)
            d = ImageDraw.Draw(frame)
            sc = height / vh
            for i, (sx, name) in enumerate(zip(slots, names)):
                wx = sx + run + math.sin(t * 6 + i) * ww * 0.004
                px, py = (wx - fx) * sc, (horizon_y + fig_h * 0.05 - fy) * sc
                col = tuple(int(c * 0.35) for c in pal[2])
                _draw_figure(d, px, py, fig_h * sc, col, pal[3], name,
                             _font(max(12, int(name_font.size * sc / S))), t * 18 * (1 + 2 * action) + i)
            for ex, ey, sp in embers:
                yy = ((ey - t * sp * 0.6) % 1.0) * height
                xx = (ex * width + math.sin(t * 10 + ex * 30) * 8) % width
                r = max(1, height // 260)
                d.ellipse([xx - r, yy - r, xx + r, yy + r], fill=pal[3])
            frame = Image.composite(frame, black, vignette)
            if label:
                ImageDraw.Draw(frame).text((height // 40, height - font.size * 1.8), label, font=font, fill=(235, 235, 235))
            proc.stdin.write(frame.tobytes())
    except BrokenPipeError:
        pass                                   # ffmpeg erken çıktı; asıl neden aşağıda stderr'den okunur
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode(errors="replace").strip()
        code = proc.wait()                     # süreç her durumda toplanır (zombi bırakma)
    if code != 0:
        raise RuntimeError(f"ffmpeg kodlama hatası: {err[-300:]}")
    return out_path


def render_defect(out_path: str, *, kind: str, seed: int, width: int, height: int, duration: float = 5.0, fps: int = 24) -> str:
    """Quality Gate'i sınamak için bilerek bozuk klip üretir: 'black' veya 'freeze'."""
    if kind == "black":
        src = f"color=c=black:size={width}x{height}:rate={fps}"
    else:
        rng = random.Random(seed)
        src = f"color=c=0x{rng.randint(0x303030, 0x707070):06x}:size={width}x{height}:rate={fps}"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
                    "-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out_path],
                   check=True)
    return out_path


RESOLUTIONS = {"480p": (854, 480), "580p": (1032, 580), "720p": (1280, 720), "1080p": (1920, 1080)}
