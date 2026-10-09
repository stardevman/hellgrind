"""Storyboard (Faz 14): her çekim için bir kare ve yönetmenin onayı; storyboard PDF'i.

Revizyonu en ucuz aşamada yapmak maliyeti en çok düşüren adımdır: kare onaylanmadan (proje ayarı açıksa) görüntü
üretilmez. Ücretsiz modda kare, prosedürel motorun çekim ortasındaki karesidir; ücretli modda bestecinin prompt'uyla
çizilen tek bir Flux karesidir (Faz 18); fal.ai anahtarı yoksa (video MiniMax H3 ile kendi sunucumuzda üretilirken
fal.ai isteğe bağlıdır) ücretli modda da yerel kare kullanılır ve ücret doğmaz. İkisinde de üzerine çekim bilgisi yazılır.
Ücretsiz modda kare, ücretli karşılığının fiyatıyla sanal olarak bütçeden düşer.

Kare içerik-adreslidir: storyboard/{sahne}/{görsel özet}.jpg. Görsel girdiler değişince yeni kare çizilir ve onay
yeniden istenir (eski onay başka bir çekimi onaylamıştı).

MiniMax H3 (FL2VA): karenin yazısız hâli (`..._clean.jpg`) videonun ilk karesidir; kompozisyon seed'le değil bu kareyle
kontrol edilir. Ücretli modda kare RunPod worker'ındaki ComfyUI görsel iş akışıyla çizilir: karakterli çekimde
Qwen-Image-Edit-2511 ile karakterin onaylı referansından (edit_keyframe.json; image 1 ana karakter), karaktersiz çekimde
Qwen-Image ile metinden (keyframe.json); fal.ai Flux isteğe bağlıdır. Kare videoyla aynı kalite kapısından geçer:
Qwen-Image-Edit karesinde ayrıca yüz kimliği (SFace, referansa karşı ≥ identity_threshold); geçemezse yeni seed'le en çok
EDIT_RETRIES kez yeniden çizilir (her deneme ayrı ücretlenir ve harcama geçmişine yazılır), yine olmazsa "kare inceleme
gerekiyor" olur ve video üretilmez (yönetmen kareyi onaylayabilir, yeniden çizdirebilir ya da kendi ilk karesini yükler).
Seedance çekimi de aynı kareyi image-to-video başlangıcı olarak kullanır.
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
from app.budget import history, killswitch, pricing
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
EDIT_RETRIES = 2                       # Qwen-Image-Edit karesi kimlikten kalırsa en çok 2 yeniden deneme (toplam 3 kare)
PAID_SOURCES = ("flux", "comfyui", "qwen_edit")    # comfyui: Qwen-Image (metinden); qwen_edit: karakter referansından
SHARED_SOURCES = ("comfyui", "qwen_edit")         # RunPod'da çizilen kare: Seedance de aynı kareden başlar
SOURCE_LABEL = {"comfyui": "h3:storyboard", "qwen_edit": "h3:storyboard:qwen_edit"}


def outdated(current: str | None, source: str | None) -> bool:
    """Güncel görsel özetin karesi daha iyi bir kaynaktan yeniden çizilmeli mi: ücretsiz moddan kalan yerel kare (ücretli
    kaynak varken) ya da karakterli çekimin metinden çizilmiş Qwen-Image karesi (Qwen-Image-Edit varken)."""
    return (current == "local" and source in PAID_SOURCES) or (current == "comfyui" and source == "qwen_edit")
REVIEW = "Kare inceleme gerekiyor"
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


def board_source(h3_mode: str = "fl2va", explicit: bool = False, model: str = "qwen") -> str:
    """Ücretli modda karenin kaynağı: RunPod worker'ındaki ComfyUI görsel iş akışı (qwen_edit: Qwen-Image-Edit-2511,
    karakter referansından; comfyui: Qwen-Image, metinden), fal.ai Flux ya da yerel prosedürel kare (yerel kare H3'e ilk kare
    olarak verilmez). model: çekimin ilk kare modeli (core/h3mode.keyframe_model).
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
        if model == "qwen_edit" and runpod_h3.keyframes_ready("edit_keyframe"):
            return "qwen_edit"
        if runpod_h3.keyframes_ready():
            return "comfyui"
    return "flux" if paid.available("fal_key") else "local"


def edit_prompt(prompt: str, refs: list[dict]) -> str:
    """Qwen-Image-Edit istemi: önce görsellere "image n" diye başvuru, ardından bestecinin çekim tarifi (kamera ve proje
    stili dahil). Şablonun örnek metni her zaman ezilir."""
    parts = []
    for i, r in enumerate(refs, start=1):
        parts.append(f"{r['name']} is the person in image {i}" if r["kind"] == "character"
                     else f"the setting is the location in image {i} ({r['name']})")
    keep = "Keep each character's face, hair, proportions and outfit exactly as in their image."
    return (f"{'; '.join(parts)}. {keep} {prompt} A single cinematic film frame, 16:9, no text, no captions.").strip()


def chain_prompt(prompt: str, refs: list[dict], transition: str) -> str:
    """Zincir karesi (Kn, n>1): image 1 önceki çekimin ilk karesi (K(n-1)); image 2/3 karakterlerin durağan referansları.
    continuous: aynı oda, yerleşim, nesneler, ışık ve karakter tasarımları korunur; yalnızca çekimin anlattığı değişir.
    cut: kamera açısı ve kadraj değişebilir, mekân, nesneler ve karakterlerin durumu korunur."""
    parts = ["Image 1 is the previous shot of this scene"]
    parts += [f"{r['name']} is the person in image {i}" if r["kind"] == "character"
              else f"the location is shown in image {i}" for i, r in enumerate(refs, start=1) if r["kind"] != "frame"]
    keep = ("Keep the same room, layout, props, lighting and characters' designs as image 1." if transition != "cut" else
            "The camera angle and framing may change for this shot, but keep the same location, props, lighting, the "
            "characters' designs and the characters' state as in image 1.")
    return (f"{'; '.join(parts)}. {keep} Change only what this shot describes: {prompt} "
            "A single cinematic film frame, 16:9, no text, no captions.").strip()


def paid_frame(out_path: str, source: str, project_id: str, scene_id: str, seed: int, prompt: str, extra: dict,
               details: dict | None = None) -> str:
    """Ücretli kare (yazısız): fal.ai Flux (karakter LoRA'larıyla ya da referans görseliyle Kontext) ya da H3
    makinesindeki ComfyUI görsel iş akışı. Kilit rezervasyondan önce çalışır; geçici hatalar yeniden denenir.
    details: harcama geçmişine yazılır (ör. kalite kapısı denemesi)."""
    if source == "qwen_edit":                       # RunPod worker'ında Qwen-Image-Edit-2511, karakter referansından
        from app.live import runpod_h3              # ücretli modül: yalnızca fonksiyon içinde
        refs, mode = extra.get("edit_references") or [], extra.get("edit_mode") or "full"
        chain = extra.get("chain")
        text = chain_prompt(prompt, refs, chain["transition"]) if chain else edit_prompt(prompt, refs)
        made = paid.call(project_id, runpod_h3.estimate_keyframe("qwen_edit", mode), SOURCE_LABEL[source],
                         "runpod_api_key",
                         lambda: runpod_h3.edit_keyframe_image(text, seed, refs, extra.get("negative", ""), project_id,
                                                               mode),
                         scene_id=scene_id, details={**(details or {}), "edit_mode": mode})
        local = made.local_path
    elif source == "comfyui":                       # H3 platformundaki ComfyUI (RunPod worker'ı ya da vast.ai makinesi)
        if settings.h3_platform == "vast":
            from app.live import vast_h3 as platform    # ücretli modül: yalnızca fonksiyon içinde
            key_name = "vast_api_key"
        else:
            from app.live import runpod_h3 as platform
            key_name = "runpod_api_key"
        estimate = platform.estimate_keyframe() if hasattr(platform, "estimate_keyframe") else pricing.estimate_keyframe()
        made = paid.call(project_id, estimate, SOURCE_LABEL[source], key_name,
                         lambda: platform.keyframe_image(prompt, seed, project_id), scene_id=scene_id, details=details)
        local = made.local_path
    else:
        from app.live import fal_video              # ücretli modül: yalnızca fonksiyon içinde
        reference = extra.get("reference")
        local = paid.call(project_id, board_estimate(reference), board_label(reference), "fal_key",
                          lambda: fal_video.image(prompt, seed, extra.get("loras"), extra.get("lora_triggers"),
                                                  reference=reference), scene_id=scene_id, details=details).local_path
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
            if scene.board_note and scene.board_note.startswith(REVIEW):
                return (f"{scene.board_note}. Kareyi onaylayın, yeniden çizdirin, kendi ilk karenizi yükleyin ya da "
                        "çekimi Ref2VA'ya alın.")
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
    anomalies: list | None = None
    detail: dict | None = None
    sha: str | None = None           # yazısız karenin SHA-256'sı (zincirde sonraki karenin `board_parent`'ı)


def identity_text(drawn: Drawn) -> str:
    """Kimlik kapısının sonucu, yönetmen için: "Elif 0,47 < 0,60" ya da "yüz görünmedi"."""
    for a in drawn.anomalies or ():
        if a.get("type") == "no_face":
            return "yüz görünmedi"
        if a.get("type") in ("low_identity", "low_likeness"):
            threshold = f"{a['threshold']:.2f}".replace(".", ",")
            values = a.get("identity") or a.get("look") or a.get("look_dino") or {}
            what = "benzerlik" if a["type"] == "low_likeness" else "kimlik"
            return f"{what} " + ", ".join(f"{name} {(v or 0):.2f}".replace(".", ",") + f" < {threshold}"
                                          for name, v in values.items())
        if a.get("type") == "continuity_jump":
            return (f"süreklilik {a['score']:.2f}".replace(".", ",") + f" < {a['threshold']:.2f}".replace(".", ",")
                    + f" (önceki kareyle mekân farklı)")
    return f"skor {drawn.score:.2f}".replace(".", ",") if drawn.score is not None else "kusurlu kare"


def draw(project_id: str, scene_id: str, fp: str, key: str, source: str, prompt: str, extra: dict, info: dict,
         lines: list[str], cfg: dict, target, salt: int | None = None) -> Drawn:
    """Kareyi çizer ya da yüklenen ilk kareden alır; yazısız hâlini ve yazılı panel karesini depoya koyar. Ücretli kaynakta
    kare kalite kapısından geçer; Qwen-Image-Edit karesi ayrıca yüz kimliğinden (SFace) geçer ve kalırsa yeni seed'le en çok
    EDIT_RETRIES kez yeniden çizilir (her deneme ayrı ücretlenir). salt: yönetmenin açık "yeniden çiz" isteği (yeni seed'ler)."""
    from app.core.chain import sha_of
    from app.quality import gate
    from app.quality.anomaly import detect_frame_anomalies
    os.makedirs(settings.work_dir, exist_ok=True)
    seed0 = seed_for(scene_id, fp, "board", *([salt] if salt else []))
    chain = extra.get("chain")
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        clean = os.path.join(tmp, "clean.jpg")
        previous = storage.download(chain["key"], os.path.join(tmp, "previous.jpg")) if chain else None
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
            edit = source == "qwen_edit"
            attempts = 1 + EDIT_RETRIES if edit else BOARD_ATTEMPTS
            for attempt in range(attempts):
                seed = seed0 if attempt == 0 else seed_for(scene_id, fp, "board", *([salt] if salt else []), attempt)
                paid_frame(clean, source, project_id, scene_id, seed, prompt, extra,
                           details={"frame_attempt": f"{attempt + 1}/{attempts}", "seed": seed})
                score = gate.score_frame(clean, seed, cfg, target, check_identity=edit, previous=previous,
                                         transition=chain["transition"] if chain else None)
                drawn = Drawn(source, score.composite, score.passed, attempt + 1, list(score.anomalies), score.detail)
                killswitch.event(project_id, "board_attempt", scene_id=scene_id, code=info["code"], attempt=attempt + 1,
                                 of=attempts, score=score.composite, passed=score.passed,
                                 identity=(score.detail or {}).get("identity"), look=(score.detail or {}).get("look"),
                                 look_dino=(score.detail or {}).get("look_dino"),
                                 faces_seen=(score.detail or {}).get("faces_seen"),
                                 faces_measured=(score.detail or {}).get("faces_measured"),
                                 continuity=(score.detail or {}).get("continuity"),
                                 review=(score.detail or {}).get("review"),
                                 chain=chain["code"] if chain else None,
                                 reasons=[a.get("type") for a in score.anomalies])
                history.annotate_last(scene_id, "h3:storyboard", {"gate": {     # kalibrasyon için kalıcı kayıt
                    "attempt": attempt + 1, "passed": score.passed, "composite": score.composite,
                    "reasons": [a.get("type") for a in score.anomalies],
                    **{k: (score.detail or {}).get(k) for k in ("look", "look_dino", "identity", "continuity", "review")
                       if (score.detail or {}).get(k) is not None}}})
                if score.passed:
                    break
        storage.put_file(clean, clean_key(key), "image/jpeg")
        drawn.sha = sha_of(clean)
        board = board_from(clean, lines, os.path.join(tmp, "board.jpg"))
        storage.put_file(board, key, "image/jpeg")
    return drawn


CHAIN_LOCK_S = 2 * 3600          # zincirin en uzun süresi (her kare 3 denemeye kadar, kare başına ~3 dk)


def chain_enabled() -> bool:
    """Anahtar kare zinciri çizilebilir mi: ücretsiz modda (yerel kareler; sıra ve kayıt aynı) ya da ücretli modda
    Qwen-Image-Edit (edit_keyframe.json) RunPod'da varsa. Değilse kareler eskisi gibi bağımsız çizilir; zincir kaydı
    tutulmaz (karelere "eski" denmez)."""
    if settings.dry_run:
        return True
    return board_source("fl2va", False, "qwen_edit") == "qwen_edit"


def make_board(project_id: str, scene_id: str, standalone: bool = False, explicit: bool = False) -> str:
    """STORYBOARD aşaması. Kare güncelse hiçbir şey yapmaz. standalone: zincirde başka aşama yok; sahne onaya kalır.
    explicit: yönetmenin açık isteği (storyboard sayfası): Ref2VA çekiminde de ücretli kare çizilir; ücretli kare yeni
    seed'lerle yeniden çizilir.
    Anahtar kare zinciri (core/chain.py): bir sekansın kareleri tek kilitle sırayla çizilir; çekim zincirdeyse (geçişi
    continuous ya da cut) önceki kareler eksik ya da eskiyse önce onlar çizilir (K(n-1) çizilmeden Kn çizilmez)."""
    from app.core.chain import ancestors, parent_of
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        chain = [sc.id for sc in ancestors(s, scene)] if chain_enabled() else []
        lock_key = f"hg:chain:{scene.sequence_id or scene.project_id}"
    # Sekansın bütün kareleri aynı kilitle: zincirin başı da (kendi görevi, sonraki çekimin görevi onu "önceki kare" olarak
    # çizerken) aynı anda iki kez çizilmesin.
    with killswitch.r().lock(lock_key, timeout=CHAIN_LOCK_S, blocking_timeout=CHAIN_LOCK_S):
        for target in [*chain, scene_id]:             # önce zincirin eksik ya da eski kareleri, en eskisinden
            with session_scope() as s:
                shot = s.get(Scene, target)
                parent = parent_of(s, shot) if chain else None
                if parent is not None and parent.board_status == "changes_requested" and not shot.keyframe_key:
                    killswitch.event(project_id, "chain_waiting", scene_id=scene_id, code=shot.scene_code,
                                     parent=parent.scene_code)
                    if target != scene_id:
                        break                         # zincir incelemedeki karede durur: sonrakiler ondan çizilecek
                    return shot.board_key or ""
            if target != scene_id:
                _make_board(project_id, str(target), keep_status=True)
        with session_scope() as s:
            current = s.get(Scene, scene_id)
            parent = parent_of(s, current) if chain else None
            if parent is not None and parent.board_status == "changes_requested" and not current.keyframe_key:
                return current.board_key or ""
        return _make_board(project_id, scene_id, standalone, explicit)


FINISHED = ("done", "refined", "audio", "lipsync", "grading", "muxing", "needs_review")
RUNNING = ("pending", "probing", "generating", "correcting")


def dependents(s, scene) -> list:
    """Bir karenin değişmesinden etkilenen çekimler: önceki çekim (videosu bu karede bitiyorsa: FL2VA last_frame), çekimin
    kendisi ve zincirde ondan türeyen sonraki çekimler."""
    from app.core.chain import neighbours, successors, transition_of
    previous, _ = neighbours(s, scene)
    out = [previous] if previous is not None and transition_of(scene) == "continuous" else []
    return [*out, scene, *(successors(s, scene) if chain_enabled() else [])]


def invalidate_dependents(project_id: str, scene_id: str, reason: str) -> dict:
    """Zincirin ortasında bir kare değişti: ona bağlı çekimlerin klipleri "eski kareden" işaretlenir (silinmez; yeniden
    kullanılmaz, kurguda kullanılmaz: çekim needs_review olur ve görüntüsü yeniden üretilir), süren üretimleri durur ve
    RunPod'daki bekleyen işleri iptal edilir. Karesi değişen çekimin kendi akışına (onu çizen görev) dokunulmaz."""
    from app.engine.iteration import CHAIN_STALE
    from app.models import Variation
    stopped, clips, codes = [], 0, []
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        for sc in dependents(s, scene):
            marked = 0
            for v in s.scalars(select(Variation).where(Variation.scene_id == sc.id,
                                                       Variation.status.in_(("passed", "refined", "generated")))):
                if not (v.params or {}).get("stale_keyframe"):
                    v.params = {**(v.params or {}), "stale_keyframe": reason}
                    marked += 1
            clips += marked
            if marked or sc.id != scene.id:
                sc.visual_hash = None                     # görüntü yeniden üretilecek (plan VISUAL der)
            if sc.id == scene.id:
                continue
            if sc.status in FINISHED and marked:
                sc.status = "needs_review"
                sc.last_error = f"Klip eski kareden üretildi ({reason}); yeniden üretin."
                codes.append(sc.scene_code)
            elif sc.status in RUNNING:
                killswitch.r().set(CHAIN_STALE.format(sc.id), reason, ex=7 * 3600)
                stopped.append(str(sc.id))
                codes.append(sc.scene_code)
    if stopped and not settings.dry_run and settings.h3_platform == "runpod":
        try:
            from app.live import runpod_h3            # ücretli modül: yalnızca fonksiyon içinde
            runpod_h3.cancel_scenes(project_id, stopped)
        except Exception as e:                        # noqa: BLE001 — iptal edilemese de üretim kendi kendine durur
            killswitch.event(project_id, "runpod_cancel_failed", error=str(e)[:200])
    if codes or clips:
        killswitch.event(project_id, "chain_invalidated", scene_id=scene_id, reason=reason, codes=codes, clips=clips)
    return {"codes": codes, "clips": clips, "stopped": len(stopped)}


def _chain_parent(s, scene, fp_of) -> tuple:
    """(önceki çekim, kullanılabilir mi): zincir varsa önceki çekim; karesi bu görsel özet için çizilmiş, yazısız hâli
    depoda ve inceleme beklemiyorsa kullanılabilir."""
    from app.core.chain import parent_of
    parent = parent_of(s, scene)
    if parent is None:
        return None, False
    usable = bool(parent.board_key and parent.board_hash == fp_of(parent) and parent.board_status != "changes_requested"
                  and storage.exists(clean_key(parent.board_key)))
    return parent, usable


def _make_board(project_id: str, scene_id: str, standalone: bool = False, explicit: bool = False,
                keep_status: bool = False) -> str:
    """Tek bir çekimin karesi. keep_status: zincirin önceki karesi olarak çizildi; çekimin durumuna dokunulmaz (kendi
    görevi ya da yönetmen yönetir)."""
    import time
    from app.core.chain import stale, transition_of
    from app.core.h3mode import edit_mode, edit_references, effective_mode, keyframe_model, strict_refs
    from app.engine.iteration import compose_for_scene, loras_of
    from app.quality import scorer
    with session_scope() as s:
        scene, project = s.get(Scene, scene_id), s.get(Project, project_id)
        chars, assets, style = load_characters(s, scene_id), load_assets(s, scene_id), bible.active(s, project_id)
        version = style.version if style else None
        fp = visual_fingerprint(scene, chars, assets, version)
        composed = compose_for_scene(scene, chars, assets, style)
        prompt, extra = composed.prompt, loras_of(chars)
        cfg = project_settings(project.settings)
        strict = strict_refs(cfg)
        model = keyframe_model(scene, chars, assets, strict)
        refs = edit_references(scene, chars, assets, strict)
        parent, usable = _chain_parent(s, scene, lambda sc: visual_fingerprint(
            sc, load_characters(s, sc.id), load_assets(s, sc.id), version)) if chain_enabled() else (None, False)
        chain_stale = stale(scene, parent if usable else None)
        extra["base_references"] = refs
        if parent is not None and usable:             # Kn: image 1 = K(n-1), sonra karakterlerin durağan görselleri
            extra["chain"] = {"key": clean_key(parent.board_key), "transition": transition_of(scene),
                              "code": parent.scene_code, "sha": parent.board_sha}
            refs = [{"name": parent.scene_code, "key": clean_key(parent.board_key), "kind": "frame"},
                    *[r for r in refs if r["kind"] == "character"][:2]]
            model = "qwen_edit"
        extra.update(edit_references=refs, negative=composed.negative, edit_mode=edit_mode(cfg))
        dialogue = dialogue_of(s, scene_id)
        target = scorer.target_for(scene, chars, style)
        mode = effective_mode(scene, chars, assets) if (scene.engine or "h3") == "h3" else "fl2va"
        info = dict(code=scene.scene_code, camera=dict(scene.camera or {}), duration=scene.duration_s,
                    board_key=scene.board_key, board_hash=scene.board_hash, board_status=scene.board_status,
                    board_source=scene.board_source, keyframe_key=scene.keyframe_key, board_sha=scene.board_sha,
                    render={"emotion": scene.emotion, "camera": dict(scene.camera or {}),
                            "characters": [c.name for c in chars], "action": scene.action_complexity,
                            "lighting": style.lighting_prefix if style else ""})
    key = f"storyboard/{scene_id}/{fp}.jpg"
    source = "upload" if info["keyframe_key"] else board_source(mode, explicit, model)
    if extra.get("chain") and source not in ("qwen_edit", "local", "upload"):
        extra.pop("chain")                            # zincir Qwen-Image-Edit ister (iş akışı yoksa referanslardan)
        extra["edit_references"] = extra["base_references"]
    stored = storage.exists(key) and storage.exists(clean_key(key))
    # ücretsiz moddan kalan yerel kare ya da karakterli çekimin metinden çizilmiş karesi: karakter referansından yeniden
    upgrade = outdated(info["board_source"], source)
    redraw = explicit and source in PAID_SOURCES and stored                 # yönetmenin açık isteği: yeni seed'lerle
    if not (info["board_hash"] == fp and info["board_key"] == key and stored and not upgrade and not redraw
            and not chain_stale):
        drawn = None
        if not stored or upgrade or redraw or chain_stale:
            if not keep_status:
                advance_scene(project_id, scene_id, status="storyboard")
            lines = [f"{info['code']} · {camera_text(info['camera'])} · {seconds_text(info['duration'])}"]
            lines += [textwrap.shorten(f"{(who or 'Anlatıcı').upper()}: {text}", 84, placeholder="…")
                      for who, text in dialogue[:2]]
            drawn = draw(project_id, scene_id, fp, key, source, prompt, extra, info, lines, cfg, target,
                         salt=time.time_ns() if redraw else None)
        changed = info["board_hash"] is not None and info["board_hash"] != fp
        fields = {"board_key": key, "board_hash": fp}
        if drawn is not None:
            fields.update(board_source=drawn.source, board_score=drawn.score, board_sha=drawn.sha,
                          board_parent=(extra.get("chain") or {}).get("sha"))
        if drawn is not None and not drawn.passed:          # kalite kapısı: video bu kareden üretilmez
            if drawn.source == "qwen_edit":
                note = f"{REVIEW}: {identity_text(drawn)} ({drawn.attempts} deneme)"
            else:
                shown = f"{drawn.score:.2f}" if drawn.score is not None else "kusurlu kare"
                note = f"Kalite kapısı: storyboard karesi geçemedi ({shown}, {drawn.attempts} deneme)"
            fields.update(board_status="changes_requested", board_note=note)
            killswitch.event(project_id, "board_rejected", scene_id=scene_id, code=info["code"], score=drawn.score,
                             note=note)
        elif drawn is not None and (drawn.detail or {}).get("review"):      # sınırda: geçti, yönetmen bakmalı
            fields.update(board_status="pending",
                          board_note="İnceleme önerilir: " + "; ".join(drawn.detail["review"]))
        elif drawn is not None and drawn.source in PAID_SOURCES and info["board_status"] == "changes_requested":
            fields.update(board_status="pending", board_note=None)    # yeniden çizilen kare geçti
        elif changed or info["board_status"] == "changes_requested":
            fields["board_status"] = "pending"          # yeni kare yeni onay ister
        advance_scene(project_id, scene_id, **fields)
        if drawn is not None and info.get("board_sha") and drawn.sha != info["board_sha"]:
            invalidate_dependents(project_id, scene_id, f"{info['code']} karesi değişti")   # eski karelerden klip yok
        killswitch.event(project_id, "board_ready", scene_id=scene_id, code=info["code"],
                         **({"chain": extra["chain"]["code"]} if extra.get("chain") else {}))
        info["board_status"] = fields.get("board_status", info["board_status"])
    if keep_status:
        return key
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
