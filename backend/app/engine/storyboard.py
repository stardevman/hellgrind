"""Storyboard (Faz 14): her çekim için bir kare ve yönetmenin onayı; storyboard PDF'i.

Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır: kare onaylanmadan (proje ayarı açıksa) görüntü
üretilmez. Ücretsiz modda kare, prosedürel motorun çekim ortasındaki karesidir; ücretli modda bestecinin prompt'uyla
çizilen tek bir Flux karesidir (Faz 18); fal.ai anahtarı yoksa (video MiniMax H3 ile kendi sunucumuzda üretilirken
fal.ai isteğe bağlıdır) ücretli modda da yerel kare kullanılır ve ücret doğmaz. İkisinde de üzerine çekim bilgisi yazılır.
Ücretsiz modda kare, ücretli karşılığının fiyatıyla sanal olarak bütçeden düşer.

Kare içerik-adreslidir: storyboard/{sahne}/{görsel özet}.jpg. Görsel girdiler değişince yeni kare çizilir ve onay
yeniden istenir (eski onay başka bir çekimi onaylamıştı).
"""
import os
import subprocess
import tempfile
import textwrap

from PIL import Image, ImageDraw
from sqlalchemy import select

from app import storage
from app.budget import killswitch, pricing
from app.config import project_settings, settings
from app.core.hashing import seed_for, visual_fingerprint
from app.core.state import advance_scene, load_assets, load_characters
from app.db import session_scope
from app.engine import paid
from app.engine.procedural import _font, render_clip
from app.models import AudioTrack, Character, Project, Scene
from app.style import bible

BOARD_W, BOARD_H = 960, 540
BOARD_MEGAPIXELS = 1.0                 # ücretli karşılığı: tek bir 1 MP Flux karesi
SHOT_TR = {"extreme_wide": "çok geniş", "wide": "geniş", "pov": "öznel", "medium": "orta",
           "over_shoulder": "omuz üstü", "close_up": "yakın", "extreme_close_up": "çok yakın"}
MOVE_TR = {"static": "sabit", "dolly_in": "yaklaşma", "dolly_out": "uzaklaşma", "pan": "pan", "tilt": "tilt",
           "crane": "vinç", "orbit": "yörünge", "tracking": "takip", "handheld": "el kamerası"}
BOARD_TR = {"pending": "onay bekliyor", "approved": "onaylı", "changes_requested": "değişiklik istendi"}


def camera_text(camera: dict | None) -> str:
    camera = camera or {}
    parts = [SHOT_TR.get(camera.get("shot"), camera.get("shot")), MOVE_TR.get(camera.get("movement"), camera.get("movement"))]
    if camera.get("lens_mm"):
        parts.append(f"{camera['lens_mm']} mm")
    return ", ".join(p for p in parts if p)


def seconds_text(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + " sn"


# ---------- kare ----------

def render_board(out_path: str, seed: int, render: dict, lines: list[str]) -> str:
    """Prosedürel motordan çekimin orta karesi; alt bantta çekim bilgisi."""
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        clip = render_clip(os.path.join(tmp, "b.mp4"), seed, BOARD_W, BOARD_H, 1.0, 3, emotion=render["emotion"],
                           camera=render["camera"], characters=render["characters"], action=render["action"],
                           label="", lighting=render["lighting"])
        frame = os.path.join(tmp, "mid.png")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", clip, "-vf", r"select=eq(n\,1)", "-frames:v", "1", frame],
                       check=True)
        return annotate(frame, lines, out_path)


def board_estimate(reference) -> float:
    return pricing.estimate_frame(bool(reference), BOARD_MEGAPIXELS)


def board_label(reference) -> str:
    return "flux_kontext:storyboard" if reference else "flux_lora:storyboard"


def paid_board(out_path: str, project_id: str, scene_id: str, seed: int, prompt: str, extra: dict, lines: list[str]) -> str:
    """Ücretli kare (Faz 18): bestecinin prompt'uyla tek bir kare, karakter LoRA'larıyla ya da karakterin referans
    görseliyle (Kontext); alt bantta çekim bilgisi. Kilit rezervasyondan önce çalışır; geçici hatalar yeniden denenir."""
    from app.live import fal_video                  # ücretli modül: yalnızca fonksiyon içinde
    reference = extra.get("reference")
    frame = paid.call(project_id, board_estimate(reference), board_label(reference), "fal_key",
                      lambda: fal_video.image(prompt, seed, extra.get("loras"), extra.get("lora_triggers"),
                                              reference=reference),
                      scene_id=scene_id)
    try:
        with Image.open(frame.local_path) as im:
            im.convert("RGB").resize((BOARD_W, BOARD_H)).save(frame.local_path + ".png")
        return annotate(frame.local_path + ".png", lines, out_path)
    finally:
        for path in (frame.local_path, frame.local_path + ".png"):
            if os.path.exists(path):
                os.remove(path)


def annotate(frame_path: str, lines: list[str], out_path: str) -> str:
    """Karenin alt bandına çekim bilgisini yazar."""
    with Image.open(frame_path) as im:
        img = im.convert("RGB")
    band_h = 26 + 24 * len(lines)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle([0, img.height - band_h, img.width, img.height], fill=(16, 17, 19, 205))
    font = _font(19)
    for i, line in enumerate(lines):
        d.text((16, img.height - band_h + 12 + 24 * i), line, font=font, fill=(236, 231, 222, 255))
    Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB").save(out_path, "JPEG", quality=85)
    return out_path


def dialogue_of(s, scene_id) -> list[tuple[str | None, str]]:
    """Çekimin aktif replikleri, sırayla: [(karakter adı, metin)]."""
    rows = s.execute(select(Character.name, AudioTrack.text).select_from(AudioTrack)
                     .outerjoin(Character, Character.id == AudioTrack.character_id)
                     .where(AudioTrack.scene_id == scene_id, AudioTrack.is_active, AudioTrack.kind == "dialogue")
                     .order_by(AudioTrack.start_offset_s)).all()
    return [(name, text or "") for name, text in rows]


def make_board(project_id: str, scene_id: str, standalone: bool = False) -> str:
    """STORYBOARD aşaması. Kare güncelse hiçbir şey yapmaz. standalone: zincirde başka aşama yok; sahne onaya kalır."""
    from app.engine.iteration import compose_for_scene, loras_of
    with session_scope() as s:
        scene, project = s.get(Scene, scene_id), s.get(Project, project_id)
        chars, assets, style = load_characters(s, scene_id), load_assets(s, scene_id), bible.active(s, project_id)
        fp = visual_fingerprint(scene, chars, assets, style.version if style else None)
        prompt, extra = compose_for_scene(scene, chars, assets, style).prompt, loras_of(chars)
        cfg = project_settings(project.settings)
        dialogue = dialogue_of(s, scene_id)
        info = dict(code=scene.scene_code, camera=dict(scene.camera or {}), duration=scene.duration_s,
                    board_key=scene.board_key, board_hash=scene.board_hash, board_status=scene.board_status,
                    render={"emotion": scene.emotion, "camera": dict(scene.camera or {}),
                            "characters": [c.name for c in chars], "action": scene.action_complexity,
                            "lighting": style.lighting_prefix if style else ""})
    key = f"storyboard/{scene_id}/{fp}.jpg"
    if not (info["board_hash"] == fp and info["board_key"] == key and storage.exists(key)):
        if not storage.exists(key):
            advance_scene(project_id, scene_id, status="storyboard")
            lines = [f"{info['code']} · {camera_text(info['camera'])} · {seconds_text(info['duration'])}"]
            lines += [textwrap.shorten(f"{(who or 'Anlatıcı').upper()}: {text}", 84, placeholder="…")
                      for who, text in dialogue[:2]]
            estimate = board_estimate(extra.get("reference"))      # ücretsiz modda da gerçek fiyatla (sanal)
            os.makedirs(settings.work_dir, exist_ok=True)
            seed = seed_for(scene_id, fp, "board")
            with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
                out = os.path.join(tmp, "board.jpg")
                if settings.dry_run:
                    with killswitch.spend(project_id, estimate, board_label(extra.get("reference")), scene_id=scene_id) as charge:
                        path = render_board(out, seed, info["render"], lines)
                        charge.actual = estimate
                elif not paid.available("fal_key"):         # fal.ai isteğe bağlı: kare yerel çizilir, ücret doğmaz
                    path = render_board(out, seed, info["render"], lines)
                else:
                    path = paid_board(out, project_id, scene_id, seed, prompt, extra, lines)
                storage.put_file(path, key, "image/jpeg")
        changed = info["board_hash"] is not None and info["board_hash"] != fp
        fields = {"board_key": key, "board_hash": fp}
        if changed or info["board_status"] == "changes_requested":
            fields["board_status"] = "pending"          # yeni kare yeni onay ister
        advance_scene(project_id, scene_id, **fields)
        killswitch.event(project_id, "board_ready", scene_id=scene_id, code=info["code"])
        info["board_status"] = fields.get("board_status", info["board_status"])
    if standalone or (cfg["require_board_approval"] and info["board_status"] != "approved"):
        advance_scene(project_id, scene_id, status="board_review")
    return key


def board_ready(scene, fp_visual: str) -> bool:
    """Onay zorunluyken görüntü üretimi için: kare bu görsel özet için çizilmiş ve onaylanmış."""
    return scene.board_status == "approved" and scene.board_hash == fp_visual and scene.board_key is not None


# ---------- PDF ----------

PAGE_W, PAGE_H = 1754, 1240                    # A4 yatay, 150 dpi
COLS, ROWS = 3, 2
MARGIN, GAP, HEADER_H, FOOTER_H = 70, 34, 96, 50


def layout(scenes: list[dict]) -> list[dict]:
    """Sayfalar: her sekans yeni sayfada başlar, sayfa başına en çok 6 kare. scenes seq sırasındadır ve her biri
    {"sequence": (kod, başlık) | None, ...} taşır. [{"sequence", "continued", "cells": [sahne...]}]."""
    pages: list[dict] = []
    for sc in scenes:
        last = pages[-1] if pages else None
        if last is None or last["sequence"] != sc["sequence"] or len(last["cells"]) == COLS * ROWS:
            pages.append({"sequence": sc["sequence"], "continued": bool(last and last["sequence"] == sc["sequence"]),
                          "cells": []})
        pages[-1]["cells"].append(sc)
    return pages


def _cell(page: Image.Image, d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, sc: dict, frame: Image.Image | None):
    img_h = int(w * 9 / 16)
    if frame is not None:
        page.paste(frame.convert("RGB").resize((w, img_h)), (x, y))
    else:
        d.rectangle([x, y, x + w, y + img_h], fill=(225, 222, 216), outline=(170, 166, 160), width=2)
        d.text((x + w // 2 - 50, y + img_h // 2 - 12), "Kare yok", font=_font(24), fill=(120, 116, 110))
    d.rectangle([x, y, x + w, y + img_h], outline=(60, 60, 60), width=2)
    ty = y + img_h + 12
    d.text((x, ty), sc["code"], font=_font(26), fill=(20, 20, 20))
    status = BOARD_TR.get(sc["board_status"], sc["board_status"])
    d.text((x + w - d.textlength(status, font=_font(20)), ty + 4), status, font=_font(20),
           fill=(46, 125, 90) if sc["board_status"] == "approved" else (150, 90, 40))
    ty += 34
    d.text((x, ty), f"{camera_text(sc['camera'])} · {seconds_text(sc['duration'])}", font=_font(20), fill=(70, 70, 70))
    ty += 28
    room = (y + h - ty) // 24
    lines = []
    for who, text in sc["dialogue"]:
        lines += textwrap.wrap(f"{(who or 'Anlatıcı').upper()}: {text}", 58)
    if sc.get("note"):
        lines += textwrap.wrap(f"Not: {sc['note']}", 58)
    for i, line in enumerate(lines[:room]):
        last = i == room - 1 and len(lines) > room
        d.text((x, ty + 24 * i), line + (" …" if last else ""), font=_font(19), fill=(40, 40, 40))


def build_pdf(title: str, scenes: list[dict], frames: dict, out_path: str) -> str:
    """frames: {sahne kodu: PIL kare | None}. Çekim kodları sırayla PDF'in Subject alanına da yazılır."""
    pages = layout(scenes) or [{"sequence": None, "continued": False, "cells": []}]
    cell_w = (PAGE_W - 2 * MARGIN - (COLS - 1) * GAP) // COLS
    cell_h = (PAGE_H - HEADER_H - FOOTER_H - 2 * MARGIN // 2 - (ROWS - 1) * GAP) // ROWS
    images = []
    for n, pg in enumerate(pages, start=1):
        page = Image.new("RGB", (PAGE_W, PAGE_H), (250, 248, 244))
        d = ImageDraw.Draw(page)
        d.text((MARGIN, 34), title, font=_font(34), fill=(20, 20, 20))
        if pg["sequence"]:
            code, name = pg["sequence"]
            head = f"{code} · {name}" + (" (devam)" if pg["continued"] else "")
            d.text((PAGE_W - MARGIN - d.textlength(head, font=_font(30)), 38), head, font=_font(30), fill=(194, 87, 63))
        d.line([(MARGIN, HEADER_H), (PAGE_W - MARGIN, HEADER_H)], fill=(180, 176, 170), width=2)
        for i, sc in enumerate(pg["cells"]):
            col, row = i % COLS, i // COLS
            _cell(page, d, MARGIN + col * (cell_w + GAP), HEADER_H + MARGIN // 2 + row * (cell_h + GAP), cell_w, cell_h,
                  sc, frames.get(sc["code"]))
        foot = f"Sayfa {n} / {len(pages)}"
        d.text((PAGE_W - MARGIN - d.textlength(foot, font=_font(20)), PAGE_H - FOOTER_H), foot, font=_font(20),
               fill=(120, 116, 110))
        images.append(page)
    images[0].save(out_path, "PDF", save_all=True, append_images=images[1:], resolution=150, title=f"{title} — storyboard",
                   subject=",".join(sc["code"] for sc in scenes), author="Hell Grind", creator="Hell Grind")
    return out_path
