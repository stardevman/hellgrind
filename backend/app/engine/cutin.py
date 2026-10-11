"""Kesme karesinin kamerayı gerçekten değiştiren yolları (ağ yok; ücretli çağrılar storyboard.cut_frame içinde).

Kıvılcım SH03: Qwen-Image-Edit-2511 image 1'in kompozisyonunu korudu; "Move the camera closer and 35° to the left" üç
denemede de SH02'nin son karesiyle aynı kamerayı verdi (jump cut). Kamera bu yüzden metinle değil şu yollarla değişir:
- Kırp + iyileştir (cut_in): önceki çekimin son karesinde odak karakterin kutusu bulunur (insan olmayan: DINOv2 yama
  haritası; insan: yüz kutusu), ölçeğe göre 16:9 kırpılır, H3 boyutuna büyütülür ve Qwen-Edit Lightning "iyileştirme"
  geçişiyle netleştirilir. Karakter o karede yoksa önce aynı kamerayla karakterin yerleştirildiği bir ara kare çizilir.
- Açı LoRA'sı (angles): Qwen-Image-Edit-2511 Multiple Angles LoRA, "<sks> azimut yükseklik uzaklık" (core/chain.angle_tokens).
"""
from PIL import Image

H3_SIZE = (1280, 704)
ASPECT = H3_SIZE[0] / H3_SIZE[1]
# kırpımın kutuya oranı (boy ve en, sonra 16:9'a genişletilir) ve karakterin kırpımda kaplaması istenen alan
CROP_FACTOR = {"extreme_close_up": 1.3, "close_up": 1.8, "medium": 2.8, "over_shoulder": 2.8, "pov": 2.8,
               "wide": 4.0, "extreme_wide": 6.0}
AREA_TARGET = {"extreme_close_up": 0.5, "close_up": 0.32, "medium": 0.12, "over_shoulder": 0.1, "pov": 0.1}
MIN_FACTOR = {"extreme_close_up": 0.5, "close_up": 0.7}   # yakın planda kırpım gövdeyi kesebilir (baştan); diğerleri
MIN_FACTOR_DEFAULT = 1.15       # kutudan dar kırpılmaz (karakterin başı ve ayakları kesilmesin)
HEADROOM = 0.1                  # kırpım kutudan kısaysa üstte kırpım boyunun bu payı kadar boşluk (baş kesilmez)
FACTOR_STEP = 0.05
PRESENT_MIN = 0.03              # karakter yamalarının kare payı bunun altındaysa karakter karede yok (Bolt'suz SH02: 0,013)
CUT_IN_SCALE = 1.5              # jump-cut kapısı: aynı kameranın >= 1,5 kat yakınlaşması geçerli cut-in
REFINE_MODE = "lightning"       # iyileştirme geçişi: Lightning (4 adım; kompozisyon kırpımdan gelir)
MAX_ZOOM = 2.5                  # bundan fazla büyütülen kırpımda iyileştirme sahneyi yeniden uyduruyor: kırp + iyileştir
                                # uygulanmaz (Kıvılcım SH03 2,03× tuttu; Nora SH02 geniş planda boy boy karakterden yakın
                                # plan 3,32×: üç denemede de iç oran 0,00, arka plansız gökyüzünde boy boy Nora)


def union(boxes: list[tuple]) -> tuple:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def crop_box(box: tuple, fill: float, width: int, height: int, shot: str | None) -> dict:
    """Kırpma dikdörtgeni (16:9, kare sınırında): kutunun CROP_FACTOR katı. Karakterin kırpımdaki tahmini alanı
    (fill x kırpımla kesişen kutu alanı / kırpım alanı) AREA_TARGET'in altındaysa çarpan daraltılır: yakın planda kutunun
    altına kadar (MIN_FACTOR; kırpım baştan sabitlenir, gövde kesilir), diğerlerinde kutudan dar değil. Kıvılcım SH03:
    oturan küçük Bolt'un kutusu dik; 16:9 kırpım 1,15 katta bile alanı ~%17'de bırakıyordu, yakın planın kadraj kapısı
    >= %25 ister. {"rect": (x0, y0, x1, y1), "factor", "area", "zoom"}."""
    x0, y0, x1, y1 = box
    bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    fill = max(0.0, min(1.0, fill))
    target = AREA_TARGET.get(shot or "", 0.0)
    floor = MIN_FACTOR.get(shot or "", MIN_FACTOR_DEFAULT)

    def place(f: float) -> tuple[tuple[float, float, float, float], float]:
        ch = max(f * bh, f * bw / ASPECT)
        cw = ch * ASPECT
        if cw > width / CUT_IN_SCALE:              # en az 1,5 kat yakınlaşma (daha azı jump cut sayılır)
            cw, ch = width / CUT_IN_SCALE, width / CUT_IN_SCALE / ASPECT
        if ch > height / CUT_IN_SCALE:
            ch, cw = height / CUT_IN_SCALE, height / CUT_IN_SCALE * ASPECT
        top = cy - ch / 2.0 if ch >= bh else y0 - HEADROOM * ch       # kutudan kısa: baş kalır, alt kesilir
        left = min(max(0.0, cx - cw / 2.0), width - cw)
        top = min(max(0.0, top), height - ch)
        ix = max(0.0, min(x1, left + cw) - max(x0, left))
        iy = max(0.0, min(y1, top + ch) - max(y0, top))
        return (left, top, left + cw, top + ch), fill * ix * iy / (cw * ch)

    factor = CROP_FACTOR.get(shot or "", 2.0)
    rect, area = place(factor)
    while area < target and factor - FACTOR_STEP >= floor - 1e-9:
        factor = round(factor - FACTOR_STEP, 3)
        rect, area = place(factor)
    return {"rect": tuple(round(v) for v in rect), "factor": factor, "area": round(area, 3),
            "zoom": round(width / (rect[2] - rect[0]), 2)}


STEP_ZOOM = 1.6                 # karaktersiz kesme: ölçek adımı başına merkezden yakınlaşma (en çok 3 kat)


def centre_crop(width: int, height: int, steps: int) -> dict:
    """Karaktersiz kesme (odak yok): kare merkezinden ölçek adımı başına STEP_ZOOM kat yakınlaşma, 16:9."""
    zoom = min(3.0, max(CUT_IN_SCALE, STEP_ZOOM ** max(1, steps)))
    cw = min(width / zoom, height * ASPECT / zoom)
    ch = cw / ASPECT
    left, top = (width - cw) / 2.0, (height - ch) / 2.0
    return {"rect": (round(left), round(top), round(left + cw), round(top + ch)), "factor": None, "area": None,
            "zoom": round(width / cw, 2)}


def crop_scale(src: str, rect: tuple, out: str) -> str:
    """Kırpım H3 tuvaline (1280x704) büyütülür (Lanczos)."""
    with Image.open(src) as im:
        im.convert("RGB").crop(rect).resize(H3_SIZE, Image.LANCZOS).save(out, "JPEG", quality=95)
    return out


def focus_box(image: Image.Image, target, names: list[str]) -> dict | None:
    """Odak karakterlerin birleşik kutusu: {"box", "fill", "coverage", "found": {ad: kutu}}. Odak karakterlerden biri
    karede yoksa (yama payı PRESENT_MIN altı, yüz yok) None. Gerçek skorlayıcı (DINOv2, SFace) gerekir."""
    from app.quality import scorer
    found = {}
    for name in names:
        box = scorer.character_box(image, target, [name])
        if box is None or (box.get("kind") == "patch" and box.get("coverage", 0) < PRESENT_MIN):
            return None
        found[name] = box
    if not found:
        return None
    if len(found) == 1:
        only = next(iter(found.values()))
        return {**only, "found": {k: v["box"] for k, v in found.items()}}
    boxes = [b["box"] for b in found.values()]
    merged = union(boxes)
    area = sum(b["fill"] * (b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]) for b in found.values())
    return {"box": merged, "fill": round(area / max(1.0, (merged[2] - merged[0]) * (merged[3] - merged[1])), 3),
            "coverage": round(sum(b.get("coverage", 0) for b in found.values()), 3),
            "found": {k: v["box"] for k, v in found.items()}}


def shot_box(found: dict, shot: str | None) -> tuple:
    """Kırpılacak kutu: insanın yakın planında baş-omuz kutusu, diğerlerinde karakter kutusu."""
    if shot in ("close_up", "extreme_close_up") and found.get("head"):
        return tuple(found["head"])
    return tuple(found["box"])


# ---------- istemler ----------

def place_prompt(prompt: str, refs: list[dict], focus: list[str], code: str, looks: list[str], once: str) -> str:
    """Ara kare: odak karakter önceki çekimin karesinde yok (SH03'ün Bolt'u SH02'de yok). Kamera ve oda aynen kalır,
    karakter sahneye yerleştirilir; sonra bu kareden kırpılır."""
    names = " and ".join(focus)
    return " ".join(x for x in (
        f"Image 1 is the previous shot ({code}){'; ' + '; '.join(looks) if looks else ''}.",
        "Keep image 1 exactly: the same camera position, angle, lens, framing, room, props and lighting.",
        f"Add {names} into this scene where the shot describes, clearly visible, not hidden or cut off, at a natural "
        f"size for this room: {prompt}", once,
        "A single cinematic film frame, 16:9, no text, no captions.") if x).strip()


def refine_prompt(looks: list[str], once: str, state: str = "") -> str:
    """İyileştirme geçişi: image 1 büyütülmüş kırpım. Kompozisyon, kamera ve ışık aynen; yalnızca netlik ve ayrıntı.
    state: bestecinin durum istisnası ("…: the eyes are dark and unlit, emitting no light.")."""
    return " ".join(x for x in (
        f"Image 1 is an enlarged close crop of a film frame{'; ' + '; '.join(looks) if looks else ''}.",
        "Sharpen it and add fine detail: keep the composition, camera position, framing and lighting exactly as in "
        "image 1; do not move, add or remove anything.", state, once,
        "A single cinematic film frame, 16:9, no text, no captions.") if x).strip()


def angles_prompt(tokens: dict, focus: list[str], looks: list[str], prompt: str, once: str, state: str = "") -> str:
    """Açı karesi (Multiple Angles LoRA): istem LoRA'nın biçimiyle başlar ("<sks> front-left quarter view low-angle shot
    close-up"); ardından odak karakter, tasarım ve durum, çekimin tarifi."""
    who = f" of {' and '.join(focus)}" if focus else ""
    return " ".join(x for x in (
        f"{tokens['text']}.",
        f"The same room, props and lighting as image 1, seen from this new camera angle: a {tokens['distance']}{who}"
        f"{'; ' + '; '.join(looks) if looks else ''}.", once, prompt, state if state not in prompt else "",
        "A single cinematic film frame, 16:9, no text, no captions.") if x).strip()
