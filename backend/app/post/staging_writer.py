"""Sahneleme planını Claude'a yazdırmak: istek (sistem metni, çekim bilgisi, karenin görseli) ve yanıtın planı.

Ağ yok, ücretli modül yüklenmez: çağrının kendisi `app/live/staging_llm.py`'dedir (kilit, izin ve bütçe oradan geçer).
Model çekimin karesini görür (yerleşim, kimin nerede olduğu), kuralların planını başlangıç olarak alır ve aynı biçimde
(post/staging.py) bir plan yazar: grubu karakterlere böler (crop), hareketleri anlatımın zamanlamasına oturtur, kamera
hareketine bir gerekçe verir. Gerekçe "note" alanında Türkçe durur ve panelde görünür.
"""
import base64
import io
import json
import re

from PIL import Image

from app.post import staging

MAX_IMAGE_SIDE = 1024            # modele giden karenin uzun kenarı (belirteç tasarrufu)

SYSTEM = f"""You are the staging director of a 2D animated series. For ONE shot you write the staging plan that a
deterministic renderer turns into an animatic clip (cut-out paper animation on top of the shot's first frame) and into a
flat "dummy" guide video for the video model. Return ONLY one JSON object, no prose, no code fence.

Plan schema (coordinates are fractions of the 16:9 frame, 0-1; times are seconds inside the shot):
{{
 "note": "1-3 short Turkish sentences for the director: what the staging does and why (plain words, no jargon)",
 "background": {{"kind": "paper"}} | {{"kind": "image", "image": "frame" | "location:<name>"}},
 "layers": [  // drawn in this order (later = in front), at most 12
   {{"id": "<character name>", "image": "frame" | "character:<name>" | "prop:<name>" | "dummy", "cutout": true,
    "crop": [x0, x1],          // optional: a vertical slice of a group picture, as fractions of the image width
    "x": .., "y": .., "h": ..,  // optional for image "frame": omit them and the cut-out stays exactly where it is
    "t0": .., "t1": ..,         // optional: visible only in this time span
    "keys": [{{"t": .., "x": .., "y": .., "h": ..}}],   // optional moves, eased between keys
    "actions": [{{"type": .., "t0": .., "t1": .., ...}}]}}
 ],
 "camera": [{{"t": .., "zoom": 1-2.5, "cx": .., "cy": ..}}],   // eased between keys; zoom 1 = whole frame
 "captions": true
}}
x = horizontal centre of the layer, y = its bottom edge (feet), h = its height. A layer cut from the frame keeps its
place when x, y, h are omitted; with "crop" it is the slice's own place. Slices of one group share the group's y and h.

Actions (all optional parameters have sensible defaults):
- breathe {{period 2.6, amp 0.01}}: idle breathing, use it under everything that should feel alive
- sigh {{amp 1}}: rise, collapse, settle (t0-t1 is the whole sigh, 1.2-2 s)
- laugh {{freq 4, amp 0.01, tilt 1.2, phase 0}}: quick bounces with a shoulder rock; vary freq/phase per character
- droop {{sink 0.015, tilt -2.5}}: sinks and tilts, then HOLDS the pose after t1 (sadness, shame, tiredness)
- lean {{tilt 3, dx 0}}: weight shifts and HOLDS; positive tilt leans the top LEFT, negative leans it RIGHT
- hop {{amp 0.04}}: anticipation squat, jump, land (0.6-0.9 s)
- shake {{amp 0.006}}: fast sideways shake (anger, refusal, a stifled giggle)
- tremble {{amp 0.004}}: small jitter (fear, cold)

Directing rules:
- Every camera move needs a reason (reveal, emotion, curiosity, follow the action). If none, keep it static.
- Keep the subject readable: at least about a third of the frame height, never cropped by a zoom.
- Anticipation, action, follow-through. Give each beat a short hold so the audience can read the face.
- Fit the beats to the narration timing given in "lines" (t0-t1): the key action lands on or right after its line.
- Give each character a different rhythm (freq, phase, timing); a group should never move as one block.
- If the frame shows several characters in one picture, split it with "crop" at the gaps between them (use the
  picture: the boundaries are vertical lines where one character ends and the next begins; slices must cover the whole
  group without gaps, e.g. [0, 0.5], [0.5, 0.7], [0.7, 1]) and name each slice after the character it shows.
- A painted full-frame picture (not a white page) is the background: only the camera can move it.
- Use only the images listed in "images". All times stay inside 0..duration_s.
- Valid action types are exactly: {", ".join(staging.ACTIONS)}.
"""

REPAIR = "Your plan has these problems:\n{errors}\nReturn the corrected plan as one JSON object only."


def frame_jpeg(path: str) -> bytes:
    """Modele giden kare: en çok MAX_IMAGE_SIDE, JPEG."""
    im = Image.open(path).convert("RGB")
    im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def frame_info(path: str) -> dict:
    """Karenin özeti: beyaz sayfa mı, kesik kâğıdın kutusu (kare oranı)."""
    im = Image.open(path)
    w, h = im.size
    out = {"width": w, "height": h, "white_page": staging.is_page(im)}
    if out["white_page"]:
        x0, y0, x1, y1 = staging.cutout(im).info["box"]
        out["cutout_box"] = {"x0": round(x0 / w, 4), "y0": round(y0 / h, 4), "x1": round(x1 / w, 4), "y1": round(y1 / h, 4)}
    return out


def shot_input(sc, shot: dict, images: dict, info: dict, rules: dict, frame_path: str | None) -> dict:
    """Modele giden çekim bilgisi (JSON). shot: animatikteki çekim (süre, replikler)."""
    from app.post.animatic import captions_of
    spec = sc.prompt_spec or {}
    return {
        "shot": sc.scene_code,
        "duration_s": shot["duration_s"],
        "heading": (sc.script_text or "").split("\n")[0],
        "description": sc.description or spec.get("prompt") or "",
        "camera": dict(sc.camera_plan or sc.camera or {}),
        "emotion": sc.emotion,
        "cast": [{"name": c["name"], "screen_position": c.get("position")} for c in info.get("cast") or []],
        "location": info.get("location"),
        "lines": captions_of(shot),
        "images": sorted(images),
        "frame": frame_info(frame_path) if frame_path else None,
        "rules_plan": {k: v for k, v in rules.items() if k not in ("version", "source")},
    }


def user_text(inp: dict) -> str:
    return ("Write the staging plan for this shot. The attached picture (if any) is the shot's first frame.\n"
            + json.dumps(inp, ensure_ascii=False, indent=1))


def image_b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data else None


def parse(text: str) -> dict | list | None:
    """Yanıttaki ilk JSON nesnesi (kod çiti ya da çevre metni olsa bile)."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except json.JSONDecodeError:
        return None


def check(plan, duration_s: float, images: dict) -> list[str]:
    """Şema doğrulaması + görsel adları ve zamanlar."""
    errs = staging.validate(plan)
    if errs or not isinstance(plan, dict):
        return errs
    for L in plan.get("layers") or []:
        img = L.get("image")
        if img and img != "dummy" and img not in images:
            errs.append(f"{L['id']}: '{img}' görseli yok (olanlar: {', '.join(sorted(images)) or 'yok'}).")
    times = [float(k["t"]) for k in plan.get("camera") or [] if isinstance(k.get("t"), (int, float))]
    for L in plan.get("layers") or []:
        times += [float(k["t"]) for k in L.get("keys") or [] if isinstance(k, dict) and isinstance(k.get("t"), (int, float))]
        for a in L.get("actions") or []:
            times += [float(a[k]) for k in ("t0", "t1") if isinstance(a.get(k), (int, float))]
    if any(t < 0 or t > duration_s + 0.5 for t in times):
        errs.append(f"Zamanlar 0 ile {duration_s:g} sn arasında olmalı.")
    return errs


def finalize(plan: dict, model: str | None, usd: float) -> dict:
    out = {k: v for k, v in plan.items() if k not in ("version", "source")}
    note = out.get("note")
    out["note"] = note.strip()[:600] if isinstance(note, str) else ""
    return {**out, "version": 1, "source": "claude", "model": model, "usd": round(float(usd), 4)}
