"""Animatik: onaylı storyboard kareleri geçici ya da final sesle, sesin süresine göre dizilir (ücretsiz, yerel FFmpeg).

Her çekim tek bir durağan karedir: onaylı storyboard karesi (yazısız hâli), yoksa çekim kartı (kod, başlık, tarif,
diyalog; referans modunda kare çizilmez). Çekimin bütün replikleri ölçülmüşse süre sesten gelir (giriş payı + replikler +
çıkış payı), değilse çekimin süresi kullanılır. Replikler animatiğin zaman çizelgesindeki yerlerine konur; seslendirilmemiş
sentez replikleri önce geçici sesle üretilir. Çıktı içerik-adreslidir: animatic/{proje}/{özet}.mp4 ve yanında kesme
listesi .json (çekimler, sekanslar, toplam süre). Hiçbir ücretli servis çağrılmaz (geçici ses ücretli modda ElevenLabs
olabilir; o da bütçe rezervasyonundan geçer).

Hareketli animatik (post/staging.py): çekimin görseli varsa (yönetmenin yüklediği ilk kare, onaylı storyboard karesi ya da
kadronun ve mekânın referans görselleri) durağan kare yerine sahnelenmiş kısa bir klip çizilir: görseller kesik kâğıt gibi
yerleşir, karakterler duyguya göre oynar, kamera çekimin hareketini yapar, replikler alt yazı olarak görünür. Sahneleme
planı çekimin `staging` alanındadır (yönetmen düzenler); boşsa kurallardan çıkarılır. Görseli olmayan çekim kart kalır.
"""
import json
import os
import subprocess
import textwrap

from PIL import Image, ImageDraw
from sqlalchemy import select

from app import storage
from app.core.series import episode_characters_q
from app.config import settings
from app.core.hashing import stable_hash
from app.db import session_scope
from app.models import Character, Project, Scene, Sequence
from app.post import voice
from app.post.mux import AudioInput, mix_audio
from app.post.pipeline import workspace

WIDTH, HEIGHT, FPS = 1280, 720, 24
BG, PANEL, TEXT, DIM, ACCENT = "#111214", "#1a1b1e", "#dcdee3", "#9ca0a9", "#4d8dff"


def _still(sc: Scene) -> tuple[str | None, str]:
    """(kare anahtarı, kaynak): onaylı storyboard karesinin yazısız hâli ya da None (kart çizilir)."""
    if sc.board_key and sc.board_status == "approved":
        from app.engine.storyboard import clean_key
        clean = clean_key(sc.board_key)
        return (clean if storage.exists(clean) else sc.board_key), "board"
    return None, "card"


def plan(s, project_id: str) -> list[dict]:
    """Çekim başına: kare, süre, replikler (çekim içi ofsetle), sekans. Ölçülmemiş replikli çekim kendi süresini korur."""
    names = {c.id: c.name for c in s.scalars(episode_characters_q(project_id))}
    seqs = {q.id: q for q in s.scalars(select(Sequence).where(Sequence.project_id == project_id))}
    out = []
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order)):
        lines = voice.dialogue(s, sc.id)
        measured = [float(t.duration_s) for t in lines if t.file_key and t.duration_s]
        if lines and len(measured) == len(lines):
            offsets, total = voice.retime(measured)
            source = "voice"
        else:
            offsets, total = [float(t.start_offset_s) for t in lines], float(sc.duration_s or 5.0)
            source = "shot"
        key, still = _still(sc)
        q = seqs.get(sc.sequence_id)
        out.append({"scene_id": str(sc.id), "scene_code": sc.scene_code, "description": sc.description or "",
                    "duration_s": round(total, 3),
                    "timing": source, "still": still, "image_key": key,
                    "heading": (sc.script_text or "").split("\n")[0],
                    "sequence": q.code if q else "", "sequence_title": q.title if q else "",
                    "lines": [{"key": t.file_key, "offset_s": off, "gain_db": float(t.gain_db),
                               "duration_s": float(t.duration_s) if t.duration_s else None,
                               "character": names.get(t.character_id) or "Anlatıcı", "text": t.text or ""}
                              for t, off in zip(lines, offsets)]})
    return out


# ---------- hareketli animatik (sahneleme) ----------

def frame_key(sc: Scene) -> tuple[str | None, str | None]:
    """(sahnelemenin karesi, sürüm özeti): yönetmenin yüklediği ilk kare, yoksa onaylı storyboard karesi."""
    if sc.keyframe_key:
        return sc.keyframe_key, sc.keyframe_hash or sc.keyframe_key
    key, src = _still(sc)
    return (key, sc.visual_hash or key) if src == "board" else (None, None)


def shot_images(s, sc: Scene) -> tuple[dict, dict]:
    """(görsel adı -> nesne anahtarı, kurallar planı girdisi; kare kutusu henüz yok). Görsel adları: frame,
    character:<ad>, location:<ad>, prop:<ad>."""
    from app.core.state import load_assets, load_characters
    from app.engine.fal_plan import references
    from app.models import SceneCharacter
    chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
    images, digests = {}, {}
    fk, fh = frame_key(sc)
    if fk:
        images["frame"], digests["frame"] = fk, fh
    for r in references(sc, chars, assets, strict=False, limit=12):
        images[f"{r.kind}:{r.name}"] = r.key
    pos = {cid: p for cid, p in s.execute(select(SceneCharacter.character_id, SceneCharacter.screen_position)
                                          .where(SceneCharacter.scene_id == sc.id))}
    cast = [{"name": ch.name, "position": pos.get(ch.id), "has_image": f"character:{ch.name}" in images} for ch in chars]
    location = next((a.name for a in assets if a.type == "location" and f"location:{a.name}" in images), None)
    info = {"camera": dict(sc.camera_plan or sc.camera or {}), "emotion": sc.emotion, "cast": cast, "location": location,
            "_digests": digests}
    return images, info


def has_staging(images: dict) -> bool:
    return bool(images)


def captions_of(shot: dict) -> list[dict]:
    """Repliklerin alt yazıları: çekim içi başlangıç, süre (yoksa sonraki repliğe ya da çekim sonuna kadar)."""
    lines = [ln for ln in shot["lines"] if (ln.get("text") or "").strip()]
    out = []
    for i, ln in enumerate(lines):
        t0 = float(ln["offset_s"])
        nxt = float(lines[i + 1]["offset_s"]) if i + 1 < len(lines) else shot["duration_s"]
        t1 = t0 + float(ln["duration_s"]) + 0.3 if ln.get("duration_s") else nxt
        out.append({"t0": round(t0, 3), "t1": round(min(t1, shot["duration_s"]), 3), "text": ln["text"].strip()})
    return out


def effective_plan(sc: Scene, images: dict, info: dict, duration_s: float, local: dict) -> dict:
    """Yönetmenin planı (çekimin staging alanı) ya da kurallar planı. local: indirilmiş görsellerin yolları."""
    if sc.staging:
        return dict(sc.staging)
    return rules_for(info, duration_s, local)


def rules_for(info: dict, duration_s: float, local: dict) -> dict:
    """Kuralların planı (karenin kesik kâğıt kutusu indirilmiş kareden)."""
    from app.post import staging
    frame = None
    if local.get("frame"):
        im = Image.open(local["frame"])
        page = staging.is_page(im)
        frame = (im.width, im.height, page, staging.cutout(im).info["box"] if page else None)
    return staging.rules_plan({**info, "duration_s": duration_s, "frame": frame})


def clip_key(sc: Scene, images: dict, info: dict, duration_s: float, caps: list, dummy: bool, tag: str) -> str:
    """Sahnelenmiş klibin içerik-adresli anahtarı, çizmeden (indirme yok): planın kaynağı (yönetmenin planı ya da
    kuralların girdisi), görsellerin sürüm özetleri, süre, alt yazılar, kukla ve künye. Kurallar planı bu girdilerden
    deterministik çıkar."""
    from app.post import staging
    digests = info.get("_digests") or {}
    source = dict(sc.staging) if sc.staging else {"rules": {k: v for k, v in info.items() if k != "_digests"}}
    digest = stable_hash([staging.RENDERER_VERSION, source, {k: digests.get(k, v) for k, v in images.items()},
                          round(float(duration_s), 3), caps, dummy, tag])
    return f"animatic/{sc.project_id}/{'guide' if dummy else 'shots'}/{digest}.mp4"


def staged_clip(s, sc: Scene, shot: dict, tmp: str, dummy: bool = False, label: str | None = None) -> str | None:
    """Çekimin sahnelenmiş klibi (nesne anahtarı). İçerik-adreslidir: aynı plan, görsel ve süre aynı anahtarı verir,
    varsa yeniden çizilmez. Görseli olmayan çekim: None."""
    from app.post import staging
    images, info = shot_images(s, sc)
    if not has_staging(images):
        return None
    caps = captions_of(shot) if not dummy else []
    tag = label if label is not None else sc.scene_code
    key = clip_key(sc, images, info, shot["duration_s"], caps, dummy, tag)
    if storage.exists(key):
        return key
    local = {}
    for i, (name, k) in enumerate(sorted(images.items())):
        if storage.exists(k):
            local[name] = storage.download(k, os.path.join(tmp, f"{sc.scene_code}_{i}{os.path.splitext(k)[1] or '.png'}"))
    plan_ = effective_plan(sc, images, info, shot["duration_s"], local)
    out = staging.render(plan_, local, shot["duration_s"], os.path.join(tmp, f"{os.path.basename(key)}"), dummy=dummy,
                         captions=caps, label=tag)
    storage.put_file(out, key)
    return key


# ---------- kukla rehberi: video modeline hareket referansı ----------

GUIDE_SECONDS = {"h3max": (2.0, 15.0), "seedance25": (1.8, 30.0)}     # şema: tek video ve toplam süre sınırı


def guide_shot(sc: Scene) -> dict:
    """Rehberin zamanı videonunkidir: çekimin üretim süresi (parça zaman çizelgesiyle aynı), alt yazısız."""
    return {"duration_s": round(float(sc.duration_s or 5.0), 3), "lines": []}


def guide_key(s, sc: Scene) -> str | None:
    """Çekimin kukla rehberinin anahtarı (çizmeden). Görseli yoksa None."""
    images, info = shot_images(s, sc)
    if not has_staging(images):
        return None
    return clip_key(sc, images, info, guide_shot(sc)["duration_s"], [], True, "")


def part_guide(s, part: list, engine: str) -> dict:
    """Parçanın rehberi: çekimlerin kukla rehberleri sırayla, tek video. {"key", "seconds", "shots", "warning"}; rehber
    gönderilmeyecekse key None (yalnızca açık olan çekimler varsa uyarıyla)."""
    on = [x for x in part if getattr(x, "motion_guide", False)]
    out = {"key": None, "seconds": 0.0, "shots": [x.scene_code for x in on], "warning": None}
    if not on:
        return out
    if engine not in GUIDE_SECONDS:
        out["warning"] = "Kukla rehberi yalnızca H3 Max ve Seedance 2.5 reference-to-video ile gönderilir."
        return out
    if len(on) < len(part):
        out["warning"] = (f"Kukla rehberi parçanın bütün çekimlerinde açık değil ({', '.join(x.scene_code for x in on)} "
                          f"açık): rehber gönderilmez.")
        return out
    keys = [guide_key(s, x) for x in part]
    if not all(keys):
        none = [x.scene_code for x, k in zip(part, keys) if not k]
        out["warning"] = f"Kukla rehberi çizilemiyor (görseli yok: {', '.join(none)}): rehber gönderilmez."
        return out
    seconds = round(sum(guide_shot(x)["duration_s"] for x in part), 3)
    lo, hi = GUIDE_SECONDS[engine]
    if not lo <= seconds <= hi:
        out["warning"] = f"Kukla rehberi {seconds:g} sn; model {lo:g}-{hi:g} sn arası kabul eder: rehber gönderilmez."
        return out
    key = keys[0] if len(keys) == 1 else f"animatic/{part[0].project_id}/guide/part_{stable_hash(keys)}.mp4"
    out.update(key=key, seconds=seconds)
    return out


def ensure_part_guide(s, part: list, engine: str, tmp: str) -> str | None:
    """Parçanın rehberini depoda hazırlar (yoksa çekim rehberlerini çizip birleştirir; yerel, ücretsiz)."""
    g = part_guide(s, part, engine)
    if not g["key"] or storage.exists(g["key"]):
        return g["key"]
    keys = [staged_clip(s, x, guide_shot(x), tmp, dummy=True, label="") for x in part]
    if len(keys) == 1:
        return keys[0]
    lst = os.path.join(tmp, "guide.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{storage.download(k, os.path.join(tmp, f'g{i}.mp4'))}'\n" for i, k in enumerate(keys))
    out = os.path.join(tmp, "guide_part.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-movflags", "+faststart", out], check=True)
    storage.put_file(out, g["key"])
    return g["key"]


def card(shot: dict, path: str) -> str:
    """Karesi olmayan çekimin kartı: grafit zemin, çekim kodu, başlık, tarif ve diyalog."""
    from app.engine.procedural import _font
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, WIDTH - 40, HEIGHT - 40], fill=PANEL, outline="#2c2e33", width=2)
    d.text((80, 80), shot["scene_code"], font=_font(54), fill=ACCENT)
    d.text((80, 150), shot["heading"][:70], font=_font(26), fill=DIM)
    y = 210
    for line in textwrap.wrap(shot["description"], 62)[:5]:
        d.text((80, y), line, font=_font(30), fill=TEXT)
        y += 42
    y += 20
    for ln in shot["lines"][:3]:
        for i, part in enumerate(textwrap.wrap(f"{ln['character']}: {ln['text']}", 66)[:2]):
            d.text((80 if i == 0 else 110, y), part, font=_font(26), fill=DIM)
            y += 36
    d.text((80, HEIGHT - 100), "storyboard karesi yok", font=_font(22), fill="#6f737c")
    img.save(path, "PNG")
    return path


def _segment(still: str, seconds: float, out: str) -> str:
    vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={BG},"
          f"fps={FPS},format=yuv420p")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", still, "-t", f"{seconds:.3f}",
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-an", out], check=True)
    return out


def assemble(project_id: str) -> dict:
    """Animatiği birleştirir. Dönen: {"key", "duration_s", "shots"} ya da {"error": ...} (çekim yoksa)."""
    voice.ensure_temp(project_id)                          # seslendirilmemiş replikler: geçici ses
    with session_scope() as s:
        shots = plan(s, project_id)
    if not shots:
        return {"error": "Projede çekim yok."}
    with workspace() as tmp, session_scope() as s:          # çekimlerin sahnelenmiş klipleri (önbellekli)
        for sh in shots:
            try:
                sh["clip_key"] = staged_clip(s, s.get(Scene, sh["scene_id"]), sh, tmp)
            except Exception as e:                       # noqa: BLE001 — bir çekimin çizimi animatiği düşürmez
                sh["clip_key"], sh["staging_error"] = None, f"{type(e).__name__}: {e}"
            if sh["clip_key"]:
                sh["still"] = "motion"
    key = f"animatic/{project_id}/{stable_hash([shots, WIDTH, HEIGHT, FPS])}.mp4"
    cut_key = key[:-4] + ".json"
    clips, sequences, at = [], [], 0.0
    for sh in shots:
        clips.append({"scene_id": sh["scene_id"], "scene_code": sh["scene_code"], "in_s": round(at, 3), "out_s": round(at + sh["duration_s"], 3),
                      "description": sh["description"], "still": sh["still"], "timing": sh["timing"],
                      "sequence": sh["sequence"]})
        if not sequences or sequences[-1]["code"] != sh["sequence"]:
            sequences.append({"code": sh["sequence"], "title": sh["sequence_title"], "in_s": round(at, 3)})
        at += sh["duration_s"]
        sequences[-1]["out_s"] = round(at, 3)
    total = round(at, 3)
    if not storage.exists(key):
        with workspace() as tmp:
            parts, inputs, start = [], [], 0.0
            for i, sh in enumerate(shots):
                if sh.get("clip_key"):
                    parts.append(storage.download(sh["clip_key"], os.path.join(tmp, f"s{i}.mp4")))
                else:
                    still = (storage.download(sh["image_key"], os.path.join(tmp, f"k{i}.jpg")) if sh["image_key"]
                             else card(sh, os.path.join(tmp, f"k{i}.png")))
                    parts.append(_segment(still, sh["duration_s"], os.path.join(tmp, f"s{i}.mp4")))
                for j, ln in enumerate(sh["lines"]):
                    if ln["key"]:
                        path = storage.download(ln["key"], os.path.join(tmp, f"a{i}_{j}{os.path.splitext(ln['key'])[1]}"))
                        inputs.append(AudioInput(path, start + ln["offset_s"], ln["gain_db"]))
                start += sh["duration_s"]
            from app.post.timeline import concat
            picture = concat(parts, os.path.join(tmp, "picture.mp4"))
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", picture]
            if inputs:
                cmd += ["-i", mix_audio(inputs, os.path.join(tmp, "voice.wav"), total)]
            else:
                cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            out = os.path.join(tmp, "animatic.mp4")
            subprocess.run(cmd + ["-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                  "-t", f"{total:.3f}", "-movflags", "+faststart", out], check=True)
            storage.put_file(out, key)
    cut = {"clips": clips, "sequences": sequences, "duration_s": total,
           "voiced": sum(1 for sh in shots if sh["timing"] == "voice"), "boards": sum(1 for sh in shots if sh["still"] == "board"),
           "motion": sum(1 for sh in shots if sh["still"] == "motion"),
           "errors": {sh["scene_code"]: sh["staging_error"] for sh in shots if sh.get("staging_error")}}
    storage.client().put_object(Bucket=settings.s3_bucket, Key=cut_key, Body=json.dumps(cut).encode(),
                                ContentType="application/json")
    with session_scope() as s:
        s.get(Project, project_id).animatic_key = key
    return {"key": key, "duration_s": total, "shots": len(shots)}
