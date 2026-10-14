"""Animatik: onaylı storyboard kareleri geçici ya da final sesle, sesin süresine göre dizilir (ücretsiz, yerel FFmpeg).

Her çekim tek bir durağan karedir: onaylı storyboard karesi (yazısız hâli), yoksa çekim kartı (kod, başlık, tarif,
diyalog; referans modunda kare çizilmez). Çekimin bütün replikleri ölçülmüşse süre sesten gelir (giriş payı + replikler +
çıkış payı), değilse çekimin süresi kullanılır. Replikler animatiğin zaman çizelgesindeki yerlerine konur; seslendirilmemiş
sentez replikleri önce geçici sesle üretilir. Çıktı içerik-adreslidir: animatic/{proje}/{özet}.mp4 ve yanında kesme
listesi .json (çekimler, sekanslar, toplam süre). Hiçbir ücretli servis çağrılmaz (geçici ses ücretli modda ElevenLabs
olabilir; o da bütçe rezervasyonundan geçer).
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
        out.append({"scene_code": sc.scene_code, "description": sc.description or "", "duration_s": round(total, 3),
                    "timing": source, "still": still, "image_key": key,
                    "heading": (sc.script_text or "").split("\n")[0],
                    "sequence": q.code if q else "", "sequence_title": q.title if q else "",
                    "lines": [{"key": t.file_key, "offset_s": off, "gain_db": float(t.gain_db),
                               "character": names.get(t.character_id) or "Anlatıcı", "text": t.text or ""}
                              for t, off in zip(lines, offsets)]})
    return out


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
    key = f"animatic/{project_id}/{stable_hash([shots, WIDTH, HEIGHT, FPS])}.mp4"
    cut_key = key[:-4] + ".json"
    clips, sequences, at = [], [], 0.0
    for sh in shots:
        clips.append({"scene_code": sh["scene_code"], "in_s": round(at, 3), "out_s": round(at + sh["duration_s"], 3),
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
           "voiced": sum(1 for sh in shots if sh["timing"] == "voice"), "boards": sum(1 for sh in shots if sh["still"] == "board")}
    storage.client().put_object(Bucket=settings.s3_bucket, Key=cut_key, Body=json.dumps(cut).encode(),
                                ContentType="application/json")
    with session_scope() as s:
        s.get(Project, project_id).animatic_key = key
    return {"key": key, "duration_s": total, "shots": len(shots)}
