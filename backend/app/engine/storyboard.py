"""Storyboard (Faz 14): her çekim için bir kare ve yönetmenin onayı; storyboard PDF'i.

Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır: kare onaylanmadan (proje ayarı açıksa) görüntü
üretilmez. Ücretsiz modda kare, prosedürel motorun çekim ortasındaki karesidir; ücretli modda bestecinin prompt'uyla
çizilen tek bir Flux karesidir (Faz 18); fal.ai anahtarı yoksa (video MiniMax H3 ile kendi sunucumuzda üretilirken
fal.ai isteğe bağlıdır) ücretli modda da yerel kare kullanılır ve ücret doğmaz. İkisinde de üzerine çekim bilgisi yazılır.
Ücretsiz modda kare, ücretli karşılığının fiyatıyla sanal olarak bütçeden düşer.

Kare içerik-adreslidir: storyboard/{sahne}/{görsel özet}.jpg. Görsel girdiler değişince yeni kare çizilir ve onay
yeniden istenir (eski onay başka bir çekimi onaylamıştı).

MiniMax H3 (FL2VA): karenin yazısız hâli (`..._clean.jpg`) videonun ilk karesidir; kompozisyon seed'le değil bu kareyle
kontrol edilir. Ücretli modda kare H3 makinesindeki ComfyUI görsel iş akışıyla (keyframe.json) ya da fal.ai Flux ile
çizilir ve videoyla aynı kalite kapısından geçer; geçemezse yeni seed'le en çok BOARD_ATTEMPTS kez denenir, yine olmazsa
"değişiklik istendi" olur ve video üretilmez (yönetmen yine de onaylayabilir ya da kendi ilk karesini yükleyebilir).
Yönetmenin yüklediği ilk kare storyboard karesinin yerine geçer (çizim yok, ücret yok).
"""
import os
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass

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
BOARD_ATTEMPTS = 1                     # ücretli kare: bir çekim × bir çizim için en çok 1 kare (gerekirse yönetmen yeniden ister)
PAID_SOURCES = ("flux", "comfyui")
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

def clean_key(board_key: str) -> str:
    """Karenin yazısız hâli: MiniMax H3'e ilk kare olarak giden görsel."""
    return board_key[:-4] + "_clean.jpg"


def render_frame(out_path: str, seed: int, render: dict) -> str:
    """Prosedürel motordan çekimin orta karesi (yazısız)."""
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        clip = render_clip(os.path.join(tmp, "b.mp4"), seed, BOARD_W, BOARD_H, 1.0, 3, emotion=render["emotion"],
                           camera=render["camera"], characters=render["characters"], action=render["action"],
                           label="", lighting=render["lighting"])
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", clip, "-vf", r"select=eq(n\,1)", "-frames:v", "1",
                        "-q:v", "2", out_path], check=True)
    return out_path


def render_board(out_path: str, seed: int, render: dict, lines: list[str]) -> str:
    """Prosedürel motordan çekimin orta karesi; alt bantta çekim bilgisi."""
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        return annotate(render_frame(os.path.join(tmp, "mid.jpg"), seed, render), lines, out_path)


def board_estimate(reference) -> float:
    return pricing.estimate_frame(bool(reference), BOARD_MEGAPIXELS)


def board_label(reference) -> str:
    return "flux_kontext:storyboard" if reference else "flux_lora:storyboard"


def board_source(h3_mode: str = "fl2va", explicit: bool = False) -> str:
    """Ücretli modda karenin kaynağı: H3 platformundaki ComfyUI görsel iş akışı (keyframe.json), fal.ai Flux ya da yerel
    prosedürel kare (yerel kare H3'e ilk kare olarak verilmez).
    Ref2VA çekimi ilk kareyi kullanmaz: storyboard için yerel (ücretsiz) kare çizilir; ücretli kare yalnızca yönetmen açıkça
    isterse (explicit). H3_KEYFRAME_REFERENCE=flux_kontext (kanca; varsayılan kapalı) ve fal.ai anahtarı varsa ilk kare
    karakterin referans görselinden Flux Kontext ile çizilir."""
    if settings.dry_run:
        return "local"
    if h3_mode == "ref2va" and not explicit:
        return "local"
    if settings.h3_keyframe_reference == "flux_kontext" and paid.available("fal_key"):
        return "flux"
    if settings.h3_platform == "vast":
        if paid.available("vast_api_key"):
            from app.live import vast_h3            # ücretli modül: yalnızca fonksiyon içinde
            if vast_h3.comfy_keyframes_ready():
                return "comfyui"
    elif paid.available("runpod_api_key"):
        from app.live import runpod_h3              # ücretli modül: yalnızca fonksiyon içinde
        if runpod_h3.keyframes_ready():
            return "comfyui"
    return "flux" if paid.available("fal_key") else "local"


def paid_frame(out_path: str, source: str, project_id: str, scene_id: str, seed: int, prompt: str, extra: dict) -> str:
    """Ücretli kare (yazısız): fal.ai Flux (karakter LoRA'larıyla ya da referans görseliyle Kontext) ya da H3
    makinesindeki ComfyUI görsel iş akışı. Kilit rezervasyondan önce çalışır; geçici hatalar yeniden denenir."""
    if source == "comfyui":                         # H3 platformundaki ComfyUI (RunPod worker'ı ya da vast.ai makinesi)
        if settings.h3_platform == "vast":
            from app.live import vast_h3 as platform    # ücretli modül: yalnızca fonksiyon içinde
            key_name = "vast_api_key"
        else:
            from app.live import runpod_h3 as platform
            key_name = "runpod_api_key"
        estimate = platform.estimate_keyframe() if hasattr(platform, "estimate_keyframe") else pricing.estimate_keyframe()
        made = paid.call(project_id, estimate, "h3:storyboard", key_name,
                         lambda: platform.keyframe_image(prompt, seed, project_id), scene_id=scene_id)
        local = made.local_path
    else:
        from app.live import fal_video              # ücretli modül: yalnızca fonksiyon içinde
        reference = extra.get("reference")
        local = paid.call(project_id, board_estimate(reference), board_label(reference), "fal_key",
                          lambda: fal_video.image(prompt, seed, extra.get("loras"), extra.get("lora_triggers"),
                                                  reference=reference), scene_id=scene_id).local_path
    try:
        with Image.open(local) as im:
            im.convert("RGB").save(out_path, "JPEG", quality=92)
    finally:
        os.remove(local)
    return out_path


def board_from(clean: str, lines: list[str], out_path: str) -> str:
    """Panel ve PDF için kare: yazısız kare 960x540'a getirilir, alt bandına çekim bilgisi yazılır."""
    resized = out_path + ".png"
    with Image.open(clean) as im:
        im.convert("RGB").resize((BOARD_W, BOARD_H)).save(resized)
    try:
        return annotate(resized, lines, out_path)
    finally:
        os.remove(resized)


def keyframe_problem(scene, fp: str) -> str | None:
    """MiniMax H3 FL2VA için ilk kare hazır mı (ücretli mod): yüklenen kare ya da bu görsel özetin, ücretli kaynaktan çizilmiş
    ve kalite kapısını geçmiş (ya da yönetmenin onayladığı) storyboard karesi. Değilse yönetmene Türkçe açıklama."""
    if scene.keyframe_key:
        return None
    if scene.board_key and scene.board_hash == fp and scene.board_source in PAID_SOURCES:
        if scene.board_status == "changes_requested":
            return ("İlk kare kullanılamaz: storyboard karesi " + ("kalite kapısını geçemedi" if scene.board_note and
                    scene.board_note.startswith("Kalite kapısı") else "için değişiklik istendi") +
                    ". Kareyi onaylayın, kendi ilk karenizi yükleyin ya da çekimi Ref2VA'ya alın.")
        return None
    if scene.board_key and scene.board_hash == fp:
        return ("FL2VA ilk kare ister: storyboard karesi yerel çizildi (H3 makinesinde keyframe.json iş akışı ya da fal.ai "
                "anahtarı yok). Denetçiden kendi ilk karenizi yükleyin ya da çekimi Ref2VA'ya alın.")
    return None


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


@dataclass
class Drawn:
    source: str
    score: float | None
    passed: bool
    attempts: int


def draw(project_id: str, scene_id: str, fp: str, key: str, source: str, prompt: str, extra: dict, info: dict,
         lines: list[str], cfg: dict, target) -> Drawn:
    """Kareyi çizer ya da yüklenen ilk kareden alır; yazısız hâlini ve yazılı panel karesini depoya koyar. Ücretli kaynakta
    kare kalite kapısından geçer; geçemezse yeni seed'le yeniden çizilir (her deneme ayrı ücretlenir)."""
    from app.quality import gate
    from app.quality.anomaly import detect_frame_anomalies
    os.makedirs(settings.work_dir, exist_ok=True)
    seed0 = seed_for(scene_id, fp, "board")
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        clean = os.path.join(tmp, "clean.jpg")
        drawn = Drawn(source, None, True, 1)
        if source == "upload":                                  # yönetmenin ilk karesi: çizim yok, ücret yok
            storage.download(info["keyframe_key"], clean)
        elif settings.dry_run:
            estimate = board_estimate(extra.get("reference"))   # ücretsiz modda da gerçek fiyatla (sanal)
            with killswitch.spend(project_id, estimate, board_label(extra.get("reference")), scene_id=scene_id) as charge:
                render_frame(clean, seed0, info["render"])
                charge.actual = estimate
            drawn.passed = not detect_frame_anomalies(clean)
        elif source == "local":                                 # ücretli modda ama ücretli kaynak yok: ücret doğmaz
            render_frame(clean, seed0, info["render"])
        else:
            for attempt in range(BOARD_ATTEMPTS):
                seed = seed0 if attempt == 0 else seed_for(scene_id, fp, "board", attempt)
                paid_frame(clean, source, project_id, scene_id, seed, prompt, extra)
                score = gate.score_frame(clean, seed, cfg, target)
                drawn = Drawn(source, score.composite, score.passed, attempt + 1)
                if score.passed:
                    break
        storage.put_file(clean, clean_key(key), "image/jpeg")
        board = board_from(clean, lines, os.path.join(tmp, "board.jpg"))
        storage.put_file(board, key, "image/jpeg")
    return drawn


def make_board(project_id: str, scene_id: str, standalone: bool = False, explicit: bool = False) -> str:
    """STORYBOARD aşaması. Kare güncelse hiçbir şey yapmaz. standalone: zincirde başka aşama yok; sahne onaya kalır.
    explicit: Ref2VA çekiminde de ücretli kare çizilir (yönetmenin açık isteği; storyboard sayfası)."""
    from app.core.h3mode import effective_mode
    from app.engine.iteration import compose_for_scene, loras_of
    from app.quality import scorer
    with session_scope() as s:
        scene, project = s.get(Scene, scene_id), s.get(Project, project_id)
        chars, assets, style = load_characters(s, scene_id), load_assets(s, scene_id), bible.active(s, project_id)
        fp = visual_fingerprint(scene, chars, assets, style.version if style else None)
        prompt, extra = compose_for_scene(scene, chars, assets, style).prompt, loras_of(chars)
        cfg = project_settings(project.settings)
        dialogue = dialogue_of(s, scene_id)
        target = scorer.target_for(scene, chars, style)
        mode = effective_mode(scene, chars, assets) if (scene.engine or "h3") == "h3" else "fl2va"
        info = dict(code=scene.scene_code, camera=dict(scene.camera or {}), duration=scene.duration_s,
                    board_key=scene.board_key, board_hash=scene.board_hash, board_status=scene.board_status,
                    board_source=scene.board_source, keyframe_key=scene.keyframe_key,
                    render={"emotion": scene.emotion, "camera": dict(scene.camera or {}),
                            "characters": [c.name for c in chars], "action": scene.action_complexity,
                            "lighting": style.lighting_prefix if style else ""})
    key = f"storyboard/{scene_id}/{fp}.jpg"
    source = "upload" if info["keyframe_key"] else board_source(mode, explicit)
    stored = storage.exists(key) and storage.exists(clean_key(key))
    upgrade = info["board_source"] == "local" and source in PAID_SOURCES      # ücretsiz moddan kalan yerel kare
    if not (info["board_hash"] == fp and info["board_key"] == key and stored and not upgrade):
        drawn = None
        if not stored or upgrade:
            advance_scene(project_id, scene_id, status="storyboard")
            lines = [f"{info['code']} · {camera_text(info['camera'])} · {seconds_text(info['duration'])}"]
            lines += [textwrap.shorten(f"{(who or 'Anlatıcı').upper()}: {text}", 84, placeholder="…")
                      for who, text in dialogue[:2]]
            drawn = draw(project_id, scene_id, fp, key, source, prompt, extra, info, lines, cfg, target)
        changed = info["board_hash"] is not None and info["board_hash"] != fp
        fields = {"board_key": key, "board_hash": fp}
        if drawn is not None:
            fields.update(board_source=drawn.source, board_score=drawn.score)
        if drawn is not None and not drawn.passed:          # kalite kapısı: video bu kareden üretilmez
            shown = f"{drawn.score:.2f}" if drawn.score is not None else "kusurlu kare"
            fields.update(board_status="changes_requested",
                          board_note=f"Kalite kapısı: storyboard karesi geçemedi ({shown}, {drawn.attempts} deneme)")
            killswitch.event(project_id, "board_rejected", scene_id=scene_id, code=info["code"], score=drawn.score)
        elif changed or info["board_status"] == "changes_requested":
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
