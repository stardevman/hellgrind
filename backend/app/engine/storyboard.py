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
import re
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
from app.core.chain import camera_of
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


SHOT_EN = {"extreme_wide": "extreme wide shot", "wide": "wide shot", "pov": "point-of-view shot", "medium": "medium shot",
           "over_shoulder": "over-the-shoulder shot", "close_up": "close-up", "extreme_close_up": "extreme close-up"}


def state_clause(r: dict, i: int) -> str:
    """Durumdaki karakter için açık değişiklik: "replace Bolt's eyes with dark unlit glass as in image 3"."""
    if not r.get("state"):
        return ""
    name, as_in = r["name"], f" as in image {i}" if r.get("state_image") else ""
    parts = [p for p in r.get("state_parts") or ["eyes"]]
    out = []
    for part in parts:
        if r["state"] == "off":
            out.append(f"replace {name}'s eyes with dark unlit glass{as_in}" if part == "eyes"
                       else f"make {name}'s {part} dark and unlit{as_in}")
        else:
            out.append(f"make {name}'s {part} light up{as_in}")
    return ", ".join(out)


def looks_text(refs: list[dict], kept=()) -> list[str]:
    """Karakterin durağan görseli her edit isteminde referanstır, değişmezleriyle (tasarım zincir boyunca kaymasın; ör. K5'te
    beliren ikinci anten K6'ya geçmesin). kept: image 1'de zaten olan karakterler: onları "eklemez", aynı yer ve pozda
    tutar; tasarımı referansa göre düzeltir (image 1'deki fazla parça, ör. ikinci ampul, taşınmaz) ve durumdaysa durumu
    açıkça değiştirir. "Unchanged" demez: Kıvılcım'da image 1'deki yanık göz "unchanged" ile korunuyordu.
    Kıvılcım SH05: zincirden gelen Mila "must look exactly like image 3" ile istenince karede iki Mila oluşmuştu."""
    out = []
    for i, r in enumerate(refs, start=1):
        if r["kind"] != "character":
            continue
        inv = f", always with {'; '.join(x.rstrip('.') for x in r['invariants'])}" if r.get("invariants") else ""
        state = f" (image {i} shows {r['name']} in this shot's state)" if r.get("state_image") else ""
        change = state_clause(r, i)
        if r["name"] in kept:
            out.append(f"keep the single {r['name']} already in image 1 in the same place and pose — image {i} is "
                       f"{r['name']}'s identity reference{state}{inv}; {r['name']}'s design must match image {i} exactly, "
                       f"with no parts that image {i} does not have" + (f"; {change}" if change else "") +
                       f"; do not add another {r['name']}")
        else:
            out.append(f"{r['name']} must look exactly like image {i}{state}{inv}" + (f"; {change}" if change else ""))
    return out


ONCE = "Each character appears exactly once in the frame."


# ---------- karakter durumu (core/states.py) ----------

def state_plan(s, scene, composed, chars, assets, cfg: dict, style) -> tuple[dict, list]:
    """(karakter adı -> durum referansı anahtarı, durum sondaları). Elle yüklenen durum referansı (varlık references
    state_off / state_on) önce; yoksa sekansta aynı durumda doğrulanmış önceki karenin karakter kırpımı."""
    from app.core import states as st
    from app.quality.scorer import ref_order
    from app.style.composer import glow_colors
    key = st.state_key(composed.states)
    if key is None:
        return {}, []
    names = st.targets(composed.states, chars)
    nouns = [n.lower() for n in names]
    label = st.probe_label(composed.states)
    by_asset = {a.id: a for a in assets or ()}
    refs, probes = {}, []
    for ch in chars:
        if ch.name not in names:
            continue
        asset = by_asset.get(ch.asset_id)
        wanted, opposite = st.probe_texts(composed.states, key, st.subject_of(asset, ch),
                                          glow_colors(getattr(asset, "invariants", None) or []), nouns)
        colors = glow_colors(getattr(asset, "invariants", None) or [])
        probe = {"name": ch.name, "keys": list(ref_order(ch.reference_images)), "wanted": wanted, "opposite": opposite,
                 "label": label if len(names) == 1 else f"{ch.name} {label}", "key": key,
                 "color": colors[0] if colors else None}
        probes.append(probe)
        manual = ((getattr(asset, "references", None) or {}).get(f"state_{key}") or {}).get("key")
        if manual or (found := auto_state_reference(s, scene, ch, key, probe, cfg, style)):
            refs[ch.name] = manual or found
    return refs, probes


def with_state_refs(refs: list[dict], state_refs: dict, key: str | None, targets=(), parts=()) -> list[dict]:
    """Durumdaki karakter: kimlik referansı durum referansıyla değişir (gözleri yanık stil görseli durumu ezmesin; normal
    durağan görsel hiçbir görsel yuvasında kalmaz), değişmezleri durumla yazılır ("round glass eyes (dark and unlit)") ve
    istemde durumu açıkça değiştirilir."""
    from app.core.states import state_text
    out = []
    for r in refs:
        if r["kind"] == "character" and key and (r["name"] in targets or r["name"] in state_refs):
            r = {**r, "key": state_refs.get(r["name"], r["key"]), "state": key, "state_image": r["name"] in state_refs,
                 "state_parts": list(parts) or ["eyes"],
                 "invariants": [state_text(x, key, parts) for x in r.get("invariants") or []]}
        out.append(r)
    return out


def state_critical(s, scene, key: str | None, frame: str, style) -> bool:
    """Durum zincirde kritik mi: bu çekimin ya da durumu süren sonraki çekimlerin olayı bu duruma dayanıyor (SH05 "eyes
    flicker on" kapalı gözlere dayanır: SH03, SH04 ve SH05'in ilk karesi kritik). Kritik durum tutmazsa kare otomatik
    geçmez, video başlamaz."""
    from app.core import states as st
    from app.engine.iteration import compose_for_scene
    if key is None:
        return False
    flip = st.opposite(key)
    if frame == "start" and any(kind == flip for _, kind in compose_for_scene(
            scene, load_characters(s, scene.id), load_assets(s, scene.id), style).events):
        return True
    q = select(Scene).where(Scene.project_id == scene.project_id, Scene.seq_order > scene.seq_order)
    if scene.sequence_id is not None:
        q = q.where(Scene.sequence_id == scene.sequence_id)
    for nxt in s.scalars(q.order_by(Scene.seq_order)):
        composed = compose_for_scene(nxt, load_characters(s, nxt.id), load_assets(s, nxt.id), style)
        if any(kind == flip for _, kind in composed.events):
            return True
        if st.state_key(composed.states) != key:
            return False
    return False


COUNTED = re.compile(r"^\s*(?:exactly\s+one|only\s+one|a\s+single|single|one)\b", re.IGNORECASE)


def count_notes(chars, assets) -> list[str]:
    """Parça sayısı içeren değişmezler ("exactly one bent antenna with a small bulb"): örnek sayma kapısı nesne
    parçalarını sayamaz (Kıvılcım: antende iki ampul), bu yüzden kare "inceleme önerilir" notuyla gözle kontrole bırakılır."""
    by_id = {a.id: a for a in assets or ()}
    out = []
    for ch in chars:
        for inv in getattr(by_id.get(ch.asset_id), "invariants", None) or []:
            if COUNTED.match(str(inv)):
                out.append(f"{ch.name}: “{str(inv).strip()}” sayılamıyor, gözle kontrol edin")
    return out


STATE_MISS = "hg:state_miss:{}"           # sondadan geçemeyen aday kare (yeniden ölçülmesin)


def auto_state_reference(s, scene, ch, key: str, probe: dict, cfg: dict, style) -> str | None:
    """Sekansta bu çekimden önce, aynı durumda olan, karesi güncel ve incelemede olmayan en yakın çekimin karesinde
    karakter durum sondasıyla doğrulanırsa (ör. SH03 başlangıç karesi: gözler karanlık) karakterin kırpımı
    state_refs/{çekim}/{karakter}_{durum}_{kare}.jpg olarak saklanır ve durum referansı olur. Gerçek skorlayıcı gerekir."""
    import re
    from app.core import states as st
    from app.engine.iteration import compose_for_scene
    from app.quality import gate, scorer
    if gate.scorer_mode() != "real":
        return None
    q = select(Scene).where(Scene.project_id == scene.project_id, Scene.seq_order < scene.seq_order)
    if scene.sequence_id is not None:
        q = q.where(Scene.sequence_id == scene.sequence_id)
    version = style.version if style else None
    for cand in s.scalars(q.order_by(Scene.seq_order.desc())):
        cchars = load_characters(s, cand.id)
        if ch.name not in {c.name for c in cchars} or not cand.board_key or cand.board_status == "changes_requested":
            continue
        cassets = load_assets(s, cand.id)
        if cand.board_hash != visual_fingerprint(cand, cchars, cassets, version):
            continue
        if st.state_key(compose_for_scene(cand, cchars, cassets, style, frame="start").states) != key:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", ch.name.lower()).strip("-") or "c"
        out = f"state_refs/{cand.id}/{slug}_{key}_{(parent_token(cand) or 'x')[:12]}.jpg"
        if storage.exists(out):
            return out
        if killswitch.r().get(STATE_MISS.format(out)) or not storage.exists(clean_key(cand.board_key)):
            continue
        os.makedirs(settings.work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
            with Image.open(storage.download(clean_key(cand.board_key), os.path.join(tmp, "c.jpg"))) as im:
                image = im.convert("RGB")
            value = (scorer.state_probe(image, [probe]).get(probe["label"]) or {}).get("score", 0.0)
            if value < float(cfg.get("state_threshold", 0.5)):
                killswitch.r().set(STATE_MISS.format(out), value, ex=7 * 24 * 3600)
                continue
            path = os.path.join(tmp, "crop.jpg")
            scorer.character_crop(image, probe["keys"]).convert("RGB").save(path, "JPEG", quality=92)
            storage.put_file(path, out, "image/jpeg")
        killswitch.event(str(scene.project_id), "state_reference", scene_id=str(scene.id), code=scene.scene_code,
                         character=ch.name, source=cand.scene_code, state=key, probe=value)
        return out
    return None


def camera_move_text(anchor: dict | None, camera: dict | None, cast: list[str]) -> str:
    """Kesme karesinde kameranın image 1'e göre nereye gittiği, açıkça: "Move the camera closer and 35° to the left,
    slightly low angle: a close-up of Bolt, 50 mm lens." Qwen-Image-Edit image 1'in kompozisyonunu korur; kamera açıkça
    tarif edilmezse kesmede yalnızca karakter değişir (jump cut)."""
    from app.core.chain import SCALE_STEP
    anchor, camera = anchor or {}, camera or {}
    shot = SHOT_EN.get(camera.get("shot"))
    if not shot:
        return ""
    steps = SCALE_STEP.get(camera.get("shot"), 2) - SCALE_STEP.get(anchor.get("shot"), 1)
    turn = int(camera.get("side") or 0) - int(anchor.get("side") or 0)
    moves = [m for m in ("closer" if steps > 0 else "farther back" if steps < 0 else "",
                         f"{abs(turn)}° to the {'right' if turn > 0 else 'left'}" if abs(turn) >= 10 else "") if m]
    angle = {"low": "slightly low angle", "high": "slightly high angle"}.get(camera.get("angle"), "")
    who = f" of {' and '.join(cast)}" if cast else ""
    lens = f", {camera['lens_mm']} mm lens" if camera.get("lens_mm") else ""
    head = f"Move the camera {' and '.join(moves)}" if moves else "Change the camera angle"
    return (f"{head}{', ' + angle if angle else ''}: a {shot}{who}{lens}. The camera position, angle and framing must "
            "clearly differ from image 1; the same location is seen from this new viewpoint.")


def framing_text(camera: dict | None, cast: list[str]) -> str:
    """Kesme karesinde kadraj açıkça istenir (Qwen-Image-Edit image 1'in kompozisyonunu korur): "medium shot of Bolt,
    35 mm lens"."""
    camera = camera or {}
    shot = SHOT_EN.get(camera.get("shot"))
    if not shot:
        return ""
    who = f" of {' and '.join(cast)}" if cast else ""
    lens = f", {camera['lens_mm']} mm lens" if camera.get("lens_mm") else ""
    return f"Reframe the camera for this shot: a {shot}{who}{lens}."


def removal_text(removed: list[str]) -> str:
    if not removed:
        return ""
    names = " and ".join(removed)
    return (f"{names} {'is' if len(removed) == 1 else 'are'} not in this shot: remove {'them' if len(removed) > 1 else names}"
            " from the frame completely.")


def edit_prompt(prompt: str, refs: list[dict]) -> str:
    """Qwen-Image-Edit istemi: önce görsellere "image n" diye başvuru, ardından bestecinin çekim tarifi (kamera ve proje
    stili dahil). Şablonun örnek metni her zaman ezilir."""
    parts = looks_text(refs)
    parts += [f"the setting is the location in image {i} ({r['name']})" for i, r in enumerate(refs, start=1)
              if r["kind"] == "location"]
    keep = "Keep each character's face, hair, proportions and outfit exactly as in their image."
    return (f"{'; '.join(parts)}. {keep} {ONCE} {prompt} A single cinematic film frame, 16:9, no text, no captions."
            ).strip()


def chain_prompt(prompt: str, refs: list[dict], transition: str, plan: dict | None = None,
                 camera: dict | None = None) -> str:
    """Zincir karesi (Kn, n>1): image 1 önceki çekimin ilk karesi (K(n-1)); image 2/3 karakterlerin durağan referansları.
    continuous: aynı oda, yerleşim, nesneler, ışık ve karakter tasarımları korunur; yalnızca çekimin anlattığı değişir.
    cut: kadraj çekimin ölçeği ve merceğiyle açıkça istenir; mekân, nesneler ve karakterlerin durumu korunur.
    plan (core/chain.classify_shots): önceki karede olup bu çekimde olmayan karakterler açıkça çıkarılır."""
    plan = plan or {}
    parts = [plan.get("anchor_text") or "Image 1 is the previous shot of this scene",
             *looks_text(refs, plan.get("kept") or ())]
    parts += [f"the location is shown in image {i}" for i, r in enumerate(refs, start=1) if r["kind"] == "location"]
    focus = [n for n in plan.get("cast") or [] if n not in (plan.get("carried") or [])]   # kadraj yazılı kadroya göre
    changing = any(r.get("state") for r in refs)    # durum değişiyor: image 1'deki durum (yanık göz) korunmasın
    if transition != "cut":
        keep = ("Keep the same room, layout, props and lighting as image 1; the characters' designs and state follow "
                "their reference images and the state change below." if changing else
                "Keep the same room, layout, props, lighting and characters' designs as image 1.")
    else:
        keep = " ".join(x for x in (camera_move_text(plan["anchor_camera"], camera, focus) if plan.get("anchor_camera")
                                    else framing_text(camera, focus), plan.get("avoid", ""),
                                    "Keep the same location, props and lighting as in image 1; the characters' designs "
                                    "and state follow their reference images and the state change below." if changing else
                                    "Keep the same location, props, lighting, the characters' designs and the "
                                    "characters' state as in image 1.") if x)
    extra = removal_text(plan.get("removed") or [])
    return " ".join(x for x in (f"{'; '.join(parts)}.", keep, extra, ONCE,
                                f"Change only what this shot describes: {prompt}",
                                "A single cinematic film frame, 16:9, no text, no captions.") if x).strip()


def end_prompt(prompt: str, refs: list[dict], camera: dict | None = None) -> str:
    """Çekim sonu karesi (Kn_end): image 1 çekimin ilk karesi (Kn). Aynı çekimin son anı: eylem tamamlanmış (ör. SH05:
    Bolt'un gözleri yanmış), kamera, kadraj, mekân ve tasarımlar aynı."""
    camera = camera or {}
    shot = SHOT_EN.get(camera.get("shot"))
    framing = f" ({shot}{', ' + str(camera['lens_mm']) + ' mm lens' if camera.get('lens_mm') else ''})" if shot else ""
    kept = [r["name"] for r in refs if r["kind"] == "character"]          # Kn'de hepsi zaten var: korunur, eklenmez
    parts = ["Image 1 is the first frame of this shot", *looks_text(refs, kept)]
    designs = ("props, lighting" if any(r.get("state") for r in refs) else "props, lighting and character designs")
    return (f"{'; '.join(parts)}. Show the last frame of the same shot, when its own action is completed (if the shot "
            f"describes a still moment, the moment stays still): {prompt} Keep the same camera position and "
            f"framing{framing}, location, {designs} as image 1; change only what the action "
            f"changes. Keep this shot's own state; do not anticipate the next shot. {ONCE} A single cinematic film "
            "frame, 16:9, no text, no captions.").strip()


def paid_frame(out_path: str, source: str, project_id: str, scene_id: str, seed: int, prompt: str, extra: dict,
               details: dict | None = None) -> str:
    """Ücretli kare (yazısız): fal.ai Flux (karakter LoRA'larıyla ya da referans görseliyle Kontext) ya da H3
    makinesindeki ComfyUI görsel iş akışı. Kilit rezervasyondan önce çalışır; geçici hatalar yeniden denenir.
    details: harcama geçmişine yazılır (ör. kalite kapısı denemesi)."""
    if source == "qwen_edit":                       # RunPod worker'ında Qwen-Image-Edit-2511, karakter referansından
        from app.live import runpod_h3              # ücretli modül: yalnızca fonksiyon içinde
        refs, mode = extra.get("edit_references") or [], extra.get("edit_mode") or "full"
        chain = extra.get("chain")
        if extra.get("end"):                        # çekim sonu karesi (Kn_end): Kn'den, eylem tamamlanmış
            text = end_prompt(prompt, refs, extra.get("camera"))
        elif chain:
            text = chain_prompt(prompt, refs, chain["transition"], chain.get("plan"), extra.get("camera"))
        else:
            text = edit_prompt(prompt, refs)
        made = paid.call(project_id, runpod_h3.estimate_keyframe("qwen_edit", mode), extra.get("label") or SOURCE_LABEL[source],
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


class CutFailed(Exception):
    """Kesme yöntemi bu denemede uygulanamadı (ör. odak karakter önceki karede de ara karede de bulunamadı)."""


def cut_plan(method: str, anchor: dict, camera: dict, code: str, fp: str, focus: list[str]) -> dict:
    """Kesme karesinin yöntemi ve denemeleri (core/chain.cut_method, cut_attempts), taban kare (önceki çekimin son karesi
    ya da geniş plan), açı LoRA'sının tokenleri ve ayarları."""
    from app.core.chain import angle_tokens, cut_attempts
    angles_on = settings.qwen_edit_angles == "on" and bool(settings.qwen_edit_angles_lora)
    return {"method": method, "methods": cut_attempts(method, anchor["camera"], camera, angles_on, 1 + EDIT_RETRIES),
            "base_key": anchor["key"], "base_code": anchor["name"], "base_camera": anchor["camera"],
            "base_cast": anchor["cast"], "focus": focus, "fp": fp, "tokens": angle_tokens(anchor["camera"], camera),
            "angles": {"lora": settings.qwen_edit_angles_lora, "strength": float(settings.qwen_edit_angles_strength),
                       "mode": settings.qwen_edit_angles_mode} if angles_on else None}


def _edit_call(out_path: str, project_id: str, scene_id: str, seed: int, text: str, refs: list[dict], negative: str,
               mode: str, label: str, details: dict, angles: dict | None = None, noted: dict | None = None) -> str:
    from app.live import runpod_h3                  # ücretli modül: yalnızca fonksiyon içinde
    made = paid.call(project_id, runpod_h3.estimate_keyframe("qwen_edit", mode), label, "runpod_api_key",
                     lambda: runpod_h3.edit_keyframe_image(text, seed, refs, negative, project_id, mode, angles=angles,
                                                           noted=noted),
                     scene_id=scene_id, details={**details, "edit_mode": mode, **(noted or {})})
    try:
        with Image.open(made.local_path) as im:
            im.convert("RGB").save(out_path, "JPEG", quality=92)
    finally:
        os.remove(made.local_path)
    return out_path


def cut_base(project_id: str, scene_id: str, seed: int, prompt: str, extra: dict, tmp: str, details: dict) -> dict:
    """Odak karakterin bulunduğu taban kare: önceki çekimin son karesi; karakter orada yoksa (SH03'ün Bolt'u SH02'de yok)
    aynı kamerayla karakterin yerleştirildiği ara kare (cutin/… altında saklanır, sonraki denemeler yeniden kullanır).
    {"key", "path", "box", "placed"}."""
    from app.core.chain import sha_of
    from app.engine import cutin
    cut, target = extra["cut"], extra.get("target")
    path = storage.download(cut["base_key"], os.path.join(tmp, "cut_base.jpg"))
    if not cut["focus"]:                            # karaktersiz kesme: merkezden kırpılır
        return {"key": cut["base_key"], "path": path, "box": None, "placed": False}
    with Image.open(path) as im:
        found = cutin.focus_box(im, target, cut["focus"])
    if found is not None:
        return {"key": cut["base_key"], "path": path, "box": found, "placed": False}
    placed_key = f"cutin/{scene_id}/{cut['fp']}_{sha_of(path)[:12]}_placed.jpg"
    placed = os.path.join(tmp, "cut_placed.jpg")
    if storage.exists(placed_key):
        storage.download(placed_key, placed)
        with Image.open(placed) as im:
            found = cutin.focus_box(im, target, cut["focus"])
        if found is not None:
            return {"key": placed_key, "path": placed, "box": found, "placed": True}
    chars = [r for r in extra.get("edit_references") or [] if r["kind"] == "character"][:2]
    refs = [{"name": cut["base_code"], "key": cut["base_key"], "kind": "frame"}, *chars]
    text = cutin.place_prompt(prompt, refs, cut["focus"], cut["base_code"], looks_text(refs), ONCE)
    _edit_call(placed, project_id, scene_id, seed, text, refs, extra.get("negative", ""),
               extra.get("edit_mode") or "full", "h3:storyboard:cut_in:place", {**details, "cut_method": "cut_in"},
               noted={"cut_step": "place", "cut_base": cut["base_key"]})
    with Image.open(placed) as im:
        found = cutin.focus_box(im, target, cut["focus"])
    if found is None:
        raise CutFailed(f"{', '.join(cut['focus'])} ara karede bulunamadı")
    storage.put_file(placed, placed_key, "image/jpeg")
    return {"key": placed_key, "path": placed, "box": found, "placed": True}


def cut_frame(out_path: str, method: str, project_id: str, scene_id: str, seed: int, prompt: str, extra: dict,
              tmp: str, details: dict) -> dict:
    """Kesme karesi, yöntemiyle (core/chain.cut_method). Dönen değer kapı için: {"method", "previous"?: süreklilik
    karşılaştırmasının karesi, "crop"?}.
    - cut_in: taban karede odak kutusu -> 16:9 kırpım -> 1280x704 -> Qwen-Edit Lightning iyileştirme.
    - angles: taban kare + Multiple Angles LoRA ("<sks> …").
    - anchor / wide: metinle kamera tarifi (chain_prompt; wide'da image 1 geniş plan)."""
    from app.core.chain import sha_of
    from app.engine import cutin
    cut = extra["cut"]
    details = {**details, "cut_method": method}
    if method not in ("cut_in", "angles"):
        paid_frame(out_path, "qwen_edit", project_id, scene_id, seed, prompt, extra, details=details)
        return {"method": method}
    base = cut_base(project_id, scene_id, seed, prompt, extra, tmp, details)
    chars = [r for r in extra.get("edit_references") or [] if r["kind"] == "character"][:2]
    negative = extra.get("negative", "")
    if method == "angles":
        refs = [{"name": cut["base_code"], "key": base["key"], "kind": "frame"}, *chars]
        text = cutin.angles_prompt(cut["tokens"], cut["focus"], looks_text(refs, cut["focus"]), prompt, ONCE,
                                   extra.get("state_text", ""))
        angles = cut["angles"]
        _edit_call(out_path, project_id, scene_id, seed, text, refs, negative, angles["mode"], "h3:storyboard:angles",
                   details, angles={"lora": angles["lora"], "strength": angles["strength"]},
                   noted={"cut_base": base["key"], "placed": base["placed"], "angle_tokens": cut["tokens"]["text"],
                          "angles_lora": angles["lora"], "angles_strength": angles["strength"]})
        return {"method": method, "previous": base["path"]}
    from app.core.chain import SCALE_STEP
    shot = (extra.get("camera") or {}).get("shot")
    with Image.open(base["path"]) as im:
        width, height = im.size
    if base["box"] is None:
        steps = SCALE_STEP.get(shot, 2) - SCALE_STEP.get((cut.get("base_camera") or {}).get("shot"), 2)
        crop = cutin.centre_crop(width, height, steps)
    else:
        crop = cutin.crop_box(cutin.shot_box(base["box"], shot), base["box"]["fill"], width, height, shot)
    crop_path = cutin.crop_scale(base["path"], crop["rect"], os.path.join(tmp, "cut_crop.jpg"))
    crop_key = f"cutin/{scene_id}/{sha_of(crop_path)[:16]}.jpg"
    storage.put_file(crop_path, crop_key, "image/jpeg")
    refs = [{"name": f"{cut['base_code']} crop", "key": crop_key, "kind": "frame"}, *chars]
    text = cutin.refine_prompt(looks_text(refs, cut["focus"]), ONCE, extra.get("state_text", ""))
    _edit_call(out_path, project_id, scene_id, seed, text, refs, negative, cutin.REFINE_MODE, "h3:storyboard:cut_in",
               details, noted={"cut_step": "refine", "cut_base": base["key"], "placed": base["placed"],
                               "crop_box": list(crop["rect"]), "crop_zoom": crop["zoom"], "crop_area": crop["area"],
                               "crop_key": crop_key, "base_size": [width, height]})
    x0, y0, x1, y1 = crop["rect"]
    return {"method": method, "previous": crop_path, "crop": crop,
            "cut_in": {"rect": (x0 / width, y0 / height, x1 / width, y1 / height), "zoom": crop["zoom"],
                       "crop": crop_path}}


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


def match_colour(clean: str, ref_key: str, tmp: str) -> dict:
    """Karenin karakter dışı bölgesinin LAB ortalaması ve sapması referans kareye (zincirde önceki kare; son karede
    çekimin ilk karesi) taşınır (post/color.transfer, sınırlı). H3 ilk karenin rengini taşıdığı için çekimler arası ton
    farkı baştan küçülür (Kıvılcım: Qwen-Image ile çizilen SH01 ile Qwen-Image-Edit kareleri arasında ΔL ~4)."""
    import numpy as np
    from app.post import color
    with Image.open(storage.download(ref_key, os.path.join(tmp, "colour_ref.jpg"))) as im:
        target = color.stats(np.asarray(im.convert("RGB")))
    with Image.open(clean) as im:
        rgb = np.asarray(im.convert("RGB"))
    before = color.difference(target, color.stats(rgb))
    out = color.transfer(rgb, target)
    Image.fromarray(out).save(clean, "JPEG", quality=95)
    after = color.difference(target, color.stats(out))
    return {"before": {"L": before["L"], "E": before["E"]}, "after": {"L": after["L"], "E": after["E"]}}


NEW_VIEW_NOTE = ("yeni bakış açısı: mekânın görüntüsünde olmayan büyük nesne (ör. ikinci masa lambası) var mı, gözle "
                 "kontrol edin")


def jump_note(detail: dict) -> str:
    """Jump cut kapısı: kesmede kamera önceki çekimin son karesiyle aynı kaldı."""
    h = detail.get("camera_change") or {}
    return (f"jump cut riski: kamera önceki çekimle aynı (iç oran {h.get('ratio', 0):.2f}, ölçek {h.get('scale', 1):.2f})"
            .replace(".", ","))


def framing_note(detail: dict) -> str:
    """Kadraj kontrolü (kesme karesi): istenen ölçek ile karedeki karakter alanı oranı."""
    f = detail.get("framing") or {}
    want = SHOT_TR.get(f.get("scale"), f.get("scale"))
    return f"kadraj istenen ölçekte değil ({want} plan beklendi, karakter alanı %{round(100 * (f.get('area') or 0))})"


def identity_text(drawn: Drawn) -> str:
    """Kimlik kapısının sonucu, yönetmen için: "Elif 0,47 < 0,60" ya da "yüz görünmedi"."""
    for a in drawn.anomalies or ():
        if a.get("type") == "state_mismatch":
            return ", ".join(f"durum ({label}) tutmadı" for label in a.get("labels") or []) or "durum tutmadı"
        if a.get("type") == "duplicate_character":
            return ", ".join(f"{name} karede birden fazla" for name in a.get("characters") or [])
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
        jump_ref = storage.download(extra["jump_ref"], os.path.join(tmp, "jump_ref.jpg")) \
            if extra.get("jump_ref") and storage.exists(extra["jump_ref"]) else None
        drawn = Drawn(source, None, True, 1)
        if source == "upload":                                  # yönetmenin ilk karesi: çizim yok, ücret yok
            storage.download(info["keyframe_key"], clean)
        elif settings.dry_run:
            estimate = extra.get("estimate") or board_estimate(extra.get("reference"))   # ücretsiz modda da gerçek fiyatla
            label = extra.get("label") or board_label(extra.get("reference"))
            with killswitch.spend(project_id, estimate, label, scene_id=scene_id) as charge:
                render_frame(clean, seed0, info["render"])
                charge.actual = estimate
            drawn.passed = not detect_frame_anomalies(clean)
        elif source == "local":                                 # ücretli modda ama ücretli kaynak yok: ücret doğmaz
            render_frame(clean, seed0, info["render"])
        else:
            edit = source == "qwen_edit"
            cut = extra.get("cut") if edit and not extra.get("end") else None
            attempts = len(cut["methods"]) if cut else 1 + EDIT_RETRIES if edit else BOARD_ATTEMPTS
            cut_notes: list[str] = []
            for attempt in range(attempts):
                seed = seed0 if attempt == 0 else seed_for(scene_id, fp, "board", *([salt] if salt else []), attempt)
                trial = {"frame_attempt": f"{attempt + 1}/{attempts}", "seed": seed}
                compare, method, made = previous, None, {}
                if cut:                                 # kesme: kamerayı değiştiren yol (kırp + iyileştir, açı LoRA'sı)
                    method = cut["methods"][attempt]
                    try:
                        made = cut_frame(clean, method, project_id, scene_id, seed, prompt, extra, tmp, trial)
                    except CutFailed as e:          # ör. ara karede karakter yok: bu deneme metinle kamera tarifine düşer
                        killswitch.event(project_id, "cut_failed", scene_id=scene_id, code=info["code"],
                                         attempt=attempt + 1, cut_method=method, error=str(e))
                        cut_notes.append(f"kesme yöntemi ({method}) uygulanamadı: {e}")
                        method = "anchor"
                        made = cut_frame(clean, method, project_id, scene_id, seed, prompt, extra, tmp, trial)
                    compare = made.get("previous") or previous
                else:
                    paid_frame(clean, source, project_id, scene_id, seed, prompt, extra, details=trial)
                # kaynakta renk eşleştirme kapılardan ÖNCE: kapılar saklanacak kareyi ölçer (Kıvılcım SH03: aktarımdan
                # önceki karede cam gözdeki pencere yansıması "parlama" sayılmıştı)
                colour = match_colour(clean, extra["color_ref"], tmp) if extra.get("color_ref") else None
                score = gate.score_frame(clean, seed, cfg, target, check_identity=edit, previous=compare,
                                         transition=chain["transition"] if chain else None,
                                         scale=(extra.get("camera") or {}).get("shot") if chain and
                                         chain["transition"] == "cut" and not extra.get("end") else None,
                                         states=extra.get("state_probes") or None, jump_ref=jump_ref,
                                         cut_in=made.get("cut_in"))
                framing_off = bool((score.detail or {}).get("framing_mismatch"))
                state_off = bool((score.detail or {}).get("state_mismatch"))
                jump_off = bool((score.detail or {}).get("jump_cut"))
                soft_off = framing_off or state_off or jump_off   # yeniden deneme sebebi, eleme değil
                detail = dict(score.detail or {}, **({"color": colour} if colour else {}),
                              **({"cut_method": method} if method else {}))
                if cut_notes:
                    detail["review"] = [*detail.get("review", []), *dict.fromkeys(cut_notes)]
                anomalies, passed = list(score.anomalies), score.passed
                last = attempt + 1 == attempts
                if soft_off and last:                     # son deneme: bulgular nota (kare geçse de kalsa da)
                    state_notes = [] if extra.get("state_critical") else \
                        [f"durum ({label}) tutmadı" for label in detail.get("state_mismatch") or []]
                    detail["review"] = [*detail.get("review", []), *([framing_note(detail)] if framing_off else []),
                                        *([jump_note(detail)] if jump_off else []), *state_notes]
                if state_off and passed and last and extra.get("state_critical"):
                    # zincirde kritik durum (sonraki olay buna dayanıyor): kare geçmez, video başlamaz
                    anomalies.append({"type": "state_mismatch", "labels": list(detail.get("state_mismatch") or [])})
                    passed = False
                drawn = Drawn(source, score.composite, passed, attempt + 1, anomalies, detail)
                killswitch.event(project_id, "board_attempt", scene_id=scene_id, code=info["code"], attempt=attempt + 1,
                                 of=attempts, score=score.composite, passed=passed,
                                 identity=(score.detail or {}).get("identity"), look=(score.detail or {}).get("look"),
                                 look_dino=(score.detail or {}).get("look_dino"),
                                 faces_seen=(score.detail or {}).get("faces_seen"),
                                 faces_measured=(score.detail or {}).get("faces_measured"),
                                 continuity=(score.detail or {}).get("continuity"),
                                 framing=(score.detail or {}).get("framing"), state=(score.detail or {}).get("state"),
                                 camera_change=(score.detail or {}).get("camera_change"),
                                 review=detail.get("review"), end=bool(extra.get("end")),
                                 chain=chain["code"] if chain else None, cut_method=method,
                                 reasons=[a.get("type") for a in anomalies])
                history.annotate_last(scene_id, "h3:storyboard", {"gate": {     # kalibrasyon için kalıcı kayıt
                    "attempt": attempt + 1, "passed": score.passed, "composite": score.composite,
                    **({"cut_method": method} if method else {}),
                    "reasons": [a.get("type") for a in score.anomalies],
                    **{k: (score.detail or {}).get(k) for k in ("look", "look_dino", "identity", "continuity", "review",
                                                                 "framing", "state", "camera_change")
                       if (score.detail or {}).get(k) is not None}}})
                if score.passed and not (soft_off and attempt + 1 < attempts):   # kadraj ya da durum tutmadı: yeniden
                    break
        if drawn.passed and source in PAID_SOURCES and chain and chain.get("transition") == "cut" \
                and not extra.get("end") and (drawn.detail or {}).get("cut_method") != "cut_in":
            # yeni bakış açısı: mekânda olmayan büyük nesne eklenmiş olabilir (kırp + iyileştir yeni bakış açısı açmaz)
            drawn.detail = {**(drawn.detail or {}), "review": [*(drawn.detail or {}).get("review", []), NEW_VIEW_NOTE]}
        if drawn.passed and source in PAID_SOURCES and extra.get("count_notes"):   # sayılan parça ölçülemiyor: göz
            drawn.detail = {**(drawn.detail or {}), "review": [*(drawn.detail or {}).get("review", []),
                                                               *extra["count_notes"]]}
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
                    # zincir incelemedeki karede durur: ondan sonraki HİÇBİR halka (devam çekimi dahil) çizilmez
                    killswitch.event(project_id, "chain_waiting", scene_id=scene_id, code=shot.scene_code,
                                     parent=parent.scene_code)
                    current = s.get(Scene, scene_id)
                    return current.board_key or ""
            if target != scene_id:
                _make_board(project_id, str(target), keep_status=True)
        with session_scope() as s:
            current = s.get(Scene, scene_id)
            if chain and not current.keyframe_key:     # önceki kare bu görsel özet için güncel değilse çizilmez
                style = bible.active_version(s, project_id)
                parent, usable = _chain_parent(s, current, lambda sc: visual_fingerprint(
                    sc, load_characters(s, sc.id), load_assets(s, sc.id), style))
                if parent is not None and not usable:
                    killswitch.event(project_id, "chain_waiting", scene_id=scene_id, code=current.scene_code,
                                     parent=parent.scene_code, stale=True)
                    return current.board_key or ""
        return _make_board(project_id, scene_id, standalone, explicit)


FINISHED = ("done", "refined", "audio", "lipsync", "grading", "muxing", "needs_review")
RUNNING = ("pending", "probing", "generating", "correcting")


def dependents(s, scene, scope: str = "chain") -> list:
    """Bir karenin değişmesinden etkilenen çekimler: önceki çekim (videosu bu karede bitiyorsa: FL2VA last_frame), çekimin
    kendisi ve zincirde ondan türeyen sonraki çekimler. scope="self": yalnızca çekimin kendisi (son karesi değişti)."""
    from app.core.chain import neighbours, successors, transition_of
    if scope == "self":
        return [scene]
    previous, _ = neighbours(s, scene)
    out = [previous] if previous is not None and transition_of(scene) == "continuous" else []
    return [*out, scene, *(successors(s, scene) if chain_enabled() else [])]


def invalidate_dependents(project_id: str, scene_id: str, reason: str, scope: str = "chain") -> dict:
    """Zincirin ortasında bir kare değişti: ona bağlı çekimlerin klipleri "eski kareden" işaretlenir (silinmez; yeniden
    kullanılmaz, kurguda kullanılmaz: çekim needs_review olur ve görüntüsü yeniden üretilir), süren üretimleri durur ve
    RunPod'daki bekleyen işleri iptal edilir. Karesi değişen çekimin kendi akışına (onu çizen görev) dokunulmaz."""
    from app.engine.iteration import CHAIN_STALE
    from app.models import Variation
    from app.core.chain import successors
    stopped, clips, codes, frames = [], 0, [], []
    with session_scope() as s:
        scene = s.get(Scene, scene_id)
        # Kn, K(n-1)'den düzenleniyorsa (devam ya da kesme) eskir: bütün sonraki halkalar, durumlarından bağımsız
        if scope == "chain" and chain_enabled():
            later = successors(s, scene)
            frames = [x.scene_code for x in later]
            for x in later:                   # eski kare onaylı görünmesin: onay beklemeye döner, eski not silinir
                if x.board_key and x.board_status != "changes_requested":
                    x.board_status, x.board_note = "pending", None
        for sc in dependents(s, scene, scope):
            marked = 0
            for v in s.scalars(select(Variation).where(Variation.scene_id == sc.id,
                                                       Variation.status.in_(("passed", "refined", "generated")))):
                if not (v.params or {}).get("stale_keyframe"):
                    v.params = {**(v.params or {}), "stale_keyframe": reason}
                    marked += 1
            clips += marked
            if marked or sc.id != scene.id:
                sc.visual_hash = None                     # görüntü yeniden üretilecek (plan VISUAL der)
            if sc.id == scene.id and scope != "self":
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
    if codes or clips or frames:
        killswitch.event(project_id, "chain_invalidated", scene_id=scene_id, reason=reason, codes=codes, clips=clips,
                         frames=frames)
    return {"codes": codes, "clips": clips, "stopped": len(stopped), "frames": frames}


def cut_anchor(s, scene, parent, assets, version, wide: bool | None = None) -> dict:
    """Kesme karesinin image 1'i ve kameranın ona göre tarifi. {"name", "key", "cast", "camera", "text"}.
    - Kamera yaklaşıyor ya da aynı ölçekte dönüyorsa (SH02 orta -> SH03 yakın): önceki çekimin son karesi; kamera ona göre
      göreli tarif edilir. Kıvılcım: SH02 ve SH03 aynı geniş plandan çizilince model aynı yeni açıya gitmişti (jump cut).
    - Kamera uzaklaşıyorsa (yakın -> iki kişilik orta): mekânın durağan görseli, yoksa sahnenin ilk geniş planı; istem
      önceki çekimin bakış açısını tekrarlamamasını söyler.
    wide: yöntemden (core/chain.cut_method): True geniş plandan çizim, False önceki çekimin son karesi (kırp + iyileştir,
    açı LoRA'sı); None ölçeğe göre."""
    from app.core.chain import SCALE_STEP, ancestors, plan_of
    current, previous = camera_of(scene), camera_of(parent)
    fp_of = lambda sc: visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), version)  # noqa: E731
    near = SCALE_STEP.get(current.get("shot"), 2) >= SCALE_STEP.get(previous.get("shot"), 2) if wide is None else not wide
    if near and parent.board_key and parent.board_status != "changes_requested":
        return {"name": parent.scene_code, "key": last_frame_key(parent, fp_of), "cast": plan_of(s, parent)["cast"],
                "camera": previous,
                "text": f"Image 1 is the previous shot ({parent.scene_code}); this shot cuts to a new camera position"}
    avoid = f" It must not repeat the previous shot's viewpoint ({parent.scene_code})."
    for a in assets or ():
        key = ((getattr(a, "references", None) or {}).get("still") or {}).get("key")
        if getattr(a, "type", None) == "location" and key and storage.exists(key):
            return {"name": a.name, "key": key, "cast": [], "camera": {"shot": "wide", "side": 0, "angle": "eye"},
                    "text": f"Image 1 shows the location ({a.name}) of this scene", "avoid": avoid}
    chain = ancestors(s, scene)
    head = chain[0] if chain else parent
    for cand in (head, parent):
        if cand.board_key and cand.board_status != "changes_requested" and storage.exists(clean_key(cand.board_key)):
            return {"name": cand.scene_code, "key": clean_key(cand.board_key), "cast": plan_of(s, cand)["cast"],
                    "camera": camera_of(cand), "avoid": avoid if cand.id != parent.id else "",
                    "text": f"Image 1 shows this location in an earlier shot ({cand.scene_code})"}
    return {"name": parent.scene_code, "key": clean_key(parent.board_key), "cast": plan_of(s, parent)["cast"],
            "camera": previous, "text": "Image 1 is the previous shot of this scene"}


def last_frame_key(scene, fp_of) -> str:
    """Çekimin videosunun son karesi: güncel ve kapıdan geçmiş son kare (Kn_end), yoksa ilk karesi."""
    if scene.end_board_key and end_usable(scene, fp_of(scene)):
        return clean_key(scene.end_board_key)
    return clean_key(scene.board_key)


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
    from app.core.chain import cut_method, plan_of, stale, transition_of
    from app.core.h3mode import edit_mode, edit_references, effective_mode, keyframe_model, strict_refs
    from app.core.states import parts_of, state_key, targets as state_targets
    from app.engine.iteration import compose_for_scene, loras_of
    from app.quality import scorer
    with session_scope() as s:
        scene, project = s.get(Scene, scene_id), s.get(Project, project_id)
        chars, assets, style = load_characters(s, scene_id), load_assets(s, scene_id), bible.active(s, project_id)
        version = style.version if style else None
        fp = visual_fingerprint(scene, chars, assets, version)
        walk_in = plan_of(s, scene)["entering"] if scene.entry_mode == "walk_in" else None
        composed = compose_for_scene(scene, chars, assets, style, frame="start", entering=walk_in)   # ilk karenin durumu
        prompt, extra = composed.prompt, loras_of(chars)
        cfg = project_settings(project.settings)
        strict = strict_refs(cfg)
        model = keyframe_model(scene, chars, assets, strict)
        refs = edit_references(scene, chars, assets, strict)
        state_refs, probes = state_plan(s, scene, composed, chars, assets, cfg, style)
        skey = state_key(composed.states)
        targets = state_targets(composed.states, chars) if skey else []
        refs = with_state_refs(refs, state_refs, skey, targets, parts_of(composed.states, [c.name for c in chars]))
        extra["state_probes"] = probes
        extra["state_critical"] = bool(probes) and state_critical(s, scene, skey, "start", style)
        extra["count_notes"] = count_notes(chars, assets)
        parent, usable = _chain_parent(s, scene, lambda sc: visual_fingerprint(
            sc, load_characters(s, sc.id), load_assets(s, sc.id), version)) if chain_enabled() else (None, False)
        if parent is not None and not usable and not scene.keyframe_key:
            # zincirdeki önceki kare güncel değil (eski, eksik ya da incelemede): bu kare referanslardan "bağımsız" çizilmez
            # (Kıvılcım SH06 eski K5 yüzünden zincirsiz çizilmişti); önce önceki kare çizilir
            killswitch.event(project_id, "chain_waiting", scene_id=scene_id, code=scene.scene_code,
                             parent=parent.scene_code, stale=True)
            return scene.board_key or ""
        chain_stale = stale(scene, parent if usable else None)
        extra["base_references"] = refs
        if parent is not None and usable:             # Kn: image 1 = K(n-1), sonra karakterlerin durağan görselleri
            extra["color_ref"] = clean_key(parent.board_key)
            plan = plan_of(s, scene)
            transition = transition_of(scene)
            image1 = {"name": parent.scene_code, "key": clean_key(parent.board_key), "kind": "frame"}
            if transition == "cut":                   # kesme: yöntem (kırp + iyileştir, açı LoRA'sı, geniş plan)
                method = cut_method(camera_of(parent), camera_of(scene), scene.cut_method)
                anchor = cut_anchor(s, scene, parent, assets, version, wide=method == "wide")
                image1 = {"name": anchor["name"], "key": anchor["key"], "kind": "frame"}
                plan = {**plan, "kept": [c for c in plan["cast"] if c in anchor["cast"]],
                        "removed": [c for c in anchor["cast"] if c not in plan["cast"]],
                        "anchor_camera": anchor["camera"], "anchor_text": anchor["text"],
                        "avoid": anchor.get("avoid", "")}
                extra["jump_ref"] = last_frame_key(parent, lambda sc: visual_fingerprint(
                    sc, load_characters(s, sc.id), load_assets(s, sc.id), version))
                extra["cut"] = cut_plan(method, anchor, camera_of(scene), parent.scene_code, fp,
                                        [n for n in plan["cast"] if n not in (plan.get("carried") or [])] or plan["cast"])
            entering = plan.get("entering") if scene.entry_mode == "walk_in" else []
            extra["chain"] = {"key": image1["key"], "transition": transition, "code": parent.scene_code,
                              "sha": parent.board_sha, "plan": plan}
            refs = [image1, *[r for r in refs if r["kind"] == "character" and r["name"] not in (entering or [])][:2]]
            model = "qwen_edit"
        extra.update(edit_references=refs, negative=composed.negative, edit_mode=edit_mode(cfg),
                     camera=camera_of(scene), state_text=composed.exception)
        dialogue = dialogue_of(s, scene_id)
        target = scorer.target_for(scene, chars, style)
        mode = effective_mode(scene, chars, assets) if (scene.engine or "h3") == "h3" else "fl2va"
        info = dict(code=scene.scene_code, camera=camera_of(scene), duration=scene.duration_s,
                    board_key=scene.board_key, board_hash=scene.board_hash, board_status=scene.board_status,
                    board_source=scene.board_source, keyframe_key=scene.keyframe_key, board_sha=scene.board_sha,
                    render={"emotion": scene.emotion, "camera": camera_of(scene),
                            "characters": [c.name for c in chars], "action": scene.action_complexity,
                            "lighting": style.lighting_prefix if style else ""})
    key = f"storyboard/{scene_id}/{fp}.jpg"
    source = "upload" if info["keyframe_key"] else board_source(mode, explicit, model)
    if extra.get("chain") and source not in ("qwen_edit", "local", "upload"):
        extra.pop("chain")                            # zincir Qwen-Image-Edit ister (iş akışı yoksa referanslardan)
        extra.pop("cut", None)
        extra["edit_references"] = extra["base_references"]
    if extra.get("cut"):
        extra["target"] = target
    stored = storage.exists(key) and storage.exists(clean_key(key))
    # ücretsiz moddan kalan yerel kare ya da karakterli çekimin metinden çizilmiş karesi: karakter referansından yeniden
    upgrade = outdated(info["board_source"], source)
    redraw = explicit and (source in PAID_SOURCES or settings.dry_run) and stored   # yönetmenin isteği: yeni seed'lerle
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
            if drawn.source == "qwen_edit" or any(x.get("type") == "state_mismatch" for x in drawn.anomalies or ()):
                extra_notes = "; ".join((drawn.detail or {}).get("review") or [])
                note = f"{REVIEW}: {identity_text(drawn)} ({drawn.attempts} deneme)" + \
                    (f"; {extra_notes}" if extra_notes else "")
            else:
                shown = f"{drawn.score:.2f}" if drawn.score is not None else "kusurlu kare"
                note = f"Kalite kapısı: storyboard karesi geçemedi ({shown}, {drawn.attempts} deneme)"
            fields.update(board_status="changes_requested", board_note=note)
            killswitch.event(project_id, "board_rejected", scene_id=scene_id, code=info["code"], score=drawn.score,
                             note=note)
        elif drawn is not None and (drawn.detail or {}).get("review"):      # sınırda: geçti, yönetmen bakmalı
            fields.update(board_status="pending",
                          board_note="İnceleme önerilir: " + "; ".join(drawn.detail["review"]))
        elif drawn is not None:                         # yeni deneme temiz geçti: eski denemenin notu kalmasın
            fields["board_note"] = None
            if changed or info["board_status"] == "changes_requested" or drawn.sha != info.get("board_sha"):
                fields["board_status"] = "pending"      # yeni görsel yeni onay ister (eski onay başka bir kareyi onaylamıştı)
        elif changed or info["board_status"] == "changes_requested":
            fields["board_status"] = "pending"          # yeni kare yeni onay ister
        advance_scene(project_id, scene_id, **fields)
        if drawn is not None and info.get("board_sha") and drawn.sha != info["board_sha"]:
            invalidate_dependents(project_id, scene_id, f"{info['code']} karesi değişti")   # eski karelerden klip yok
        killswitch.event(project_id, "board_ready", scene_id=scene_id, code=info["code"],
                         **({"chain": extra["chain"]["code"]} if extra.get("chain") else {}),
                         **({"cut_method": (drawn.detail or {}).get("cut_method") or extra["cut"]["method"]}
                            if extra.get("cut") and drawn is not None else {}))
        info["board_status"] = fields.get("board_status", info["board_status"])
    make_end_board(project_id, scene_id)
    if keep_status:
        return key
    if standalone or (cfg["require_board_approval"] and info["board_status"] != "approved"):
        advance_scene(project_id, scene_id, status="board_review")
    return key


# ---------- çekim sonu karesi (Kn_end) ----------

END_FAIL = "Son kare kapıdan geçemedi"
END_MODE = "lightning"                 # Qwen-Image-Edit Lightning (~15 GPU-sn, ~0,007 $): kare Kn'den küçük bir değişiklik


def end_key(scene_id, fp: str, parent_sha: str | None) -> str:
    return f"storyboard/{scene_id}/{fp}_end_{(parent_sha or 'none')[:12]}.jpg"


def parent_token(scene) -> str | None:
    """Son karenin çizildiği Kn'nin kimliği: board_sha; zincirden önce çizilmiş karede (sha'sı yok; Kıvılcım SH01) kare
    anahtarının özeti. Böylece eski karelere de son kare çizilir ve sonraki halkalar "eski" sayılmaz."""
    if getattr(scene, "board_sha", None):
        return scene.board_sha
    if getattr(scene, "board_key", None):
        import hashlib
        return "k" + hashlib.sha256(scene.board_key.encode()).hexdigest()[:31]
    return None


def end_wanted(s, scene, cfg: dict, chars=(), assets=()) -> bool:
    """Çekim sonu karesi gerekli mi: proje ayarı chain_end_frame açık, zincir çizilebilir, H3 FL2VA üretimi ve sonraki
    çekim continuous değil (kesme, yeni sahne ya da sekansın sonu): video sonraki çekime "yolculuk" yapmasın, kendi
    eylemiyle bitsin."""
    from app.core.chain import next_continuous
    from app.core.h3mode import effective_mode
    if not cfg.get("chain_end_frame", True) or not chain_enabled():
        return False
    if (scene.engine or "h3") != "h3" or (scene.method or "generate") != "generate" \
            or effective_mode(scene, chars, assets) != "fl2va":
        return False
    return next_continuous(s, scene) is None


def end_fresh(scene, fp: str) -> bool:
    """Son kare bu görsel özetin ve bu Kn'nin karesinden çizilmiş mi."""
    token = parent_token(scene)
    return bool(scene.end_board_key and token and scene.end_board_key == end_key(scene.id, fp, token)
                and scene.end_board_parent == token and storage.exists(clean_key(scene.end_board_key)))


def end_usable(scene, fp: str) -> bool:
    """FL2VA last_frame olarak kullanılabilir mi: güncel ve kapıdan geçmiş."""
    return end_fresh(scene, fp) and not (scene.end_board_note or "").startswith(END_FAIL)


def make_end_board(project_id: str, scene_id: str, redraw: bool = False) -> str | None:
    """Kn_end: Kn'den Qwen-Image-Edit Lightning ile, çekimin kendi eylemi tamamlanmış hâli (ör. SH05: Bolt'un gözleri
    yanmış). Kimlik ve değişmez kapılarından (benzerlik) ve Kn ile süreklilikten geçer; geçemezse video son karesiz
    üretilir (not düşülür). Ücretsiz modda yerel kare, Lightning fiyatıyla sanal. Değişirse çekimin klipleri "eski kareden"
    işaretlenir."""
    import time
    from app.core.h3mode import edit_references, strict_refs
    from app.engine.iteration import compose_for_scene
    from app.quality import scorer
    with session_scope() as s:
        scene, project = s.get(Scene, scene_id), s.get(Project, project_id)
        chars, assets, style = load_characters(s, scene_id), load_assets(s, scene_id), bible.active(s, project_id)
        cfg = project_settings(project.settings)
        fp = visual_fingerprint(scene, chars, assets, style.version if style else None)
        if not end_wanted(s, scene, cfg, chars, assets):
            return None
        board_ok = scene.board_key and scene.board_hash == fp \
            and scene.board_status != "changes_requested" and storage.exists(clean_key(scene.board_key))
        if not board_ok or (end_fresh(scene, fp) and not redraw):
            return scene.end_board_key if board_ok else None
        composed = compose_for_scene(scene, chars, assets, style, frame="end")       # son karenin durumu
        from app.core.states import parts_of, state_key, targets as state_targets
        state_refs, probes = state_plan(s, scene, composed, chars, assets, cfg, style)
        skey = state_key(composed.states)
        refs = [r for r in with_state_refs(edit_references(scene, chars, assets, strict_refs(cfg)), state_refs, skey,
                                           state_targets(composed.states, chars) if skey else [],
                                           parts_of(composed.states, [c.name for c in chars]))
                if r["kind"] == "character"][:2]
        critical = bool(probes) and state_critical(s, scene, skey, "end", style)
        source = "local" if settings.dry_run else board_source("fl2va", False, "qwen_edit")
        if source != "qwen_edit" and not settings.dry_run:
            return None                                   # Qwen-Image-Edit yok: son kare çizilmez, video son karesiz
        parent_sha, old_sha = parent_token(scene), scene.end_board_sha
        target = scorer.target_for(scene, chars, style)
        kn = clean_key(scene.board_key)
        info = dict(code=f"{scene.scene_code} son kare", keyframe_key=None,
                    render={"emotion": scene.emotion, "camera": camera_of(scene),
                            "characters": [c.name for c in chars], "action": scene.action_complexity,
                            "lighting": style.lighting_prefix if style else ""})
        extra = {"end": True, "chain": {"key": kn, "transition": "continuous", "code": scene.scene_code},
                 "edit_references": [{"name": scene.scene_code, "key": kn, "kind": "frame"}, *refs],
                 "negative": composed.negative, "edit_mode": END_MODE, "camera": camera_of(scene),
                 "label": "h3:storyboard:end", "state_probes": probes, "state_critical": critical, "color_ref": kn,
                 "count_notes": count_notes(chars, assets),
                 "estimate": pricing.estimate_keyframe("h3", model="qwen_edit", edit_mode=END_MODE)}
        lines = [f"{scene.scene_code} · son kare · {camera_text(camera_of(scene))}"]
        code = scene.scene_code
    key = end_key(scene_id, fp, parent_sha)
    drawn = draw(project_id, scene_id, fp, key, "qwen_edit" if source == "qwen_edit" else "local", composed.prompt,
                 extra, info, lines, cfg, target, salt=f"end{time.time_ns() if redraw else ''}")
    if not drawn.passed:
        note = f"{END_FAIL}: {identity_text(drawn)} ({drawn.attempts} deneme); video son karesiz üretilir"
    elif (drawn.detail or {}).get("review"):
        note = "İnceleme önerilir: " + "; ".join(drawn.detail["review"])
    else:
        note = None
    fields = dict(end_board_key=key, end_board_sha=drawn.sha, end_board_parent=parent_sha, end_board_score=drawn.score,
                  end_board_note=note)
    if not drawn.passed and any(x.get("type") == "state_mismatch" for x in drawn.anomalies or ()):
        # kritik durum son karede tutmadı: video son karesiz de başlamasın, yönetmen baksın
        fields.update(board_status="changes_requested",
                      board_note=f"{REVIEW}: son kare {identity_text(drawn)} ({drawn.attempts} deneme)")
    advance_scene(project_id, scene_id, **fields)
    killswitch.event(project_id, "end_board_ready", scene_id=scene_id, code=code, passed=drawn.passed, note=note)
    if old_sha and drawn.sha != old_sha:                  # videonun son karesi değişti: eski klipler kullanılmaz
        invalidate_dependents(project_id, scene_id, f"{code} son karesi değişti", scope="self")
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
