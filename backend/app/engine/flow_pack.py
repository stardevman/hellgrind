"""Google Flow köprüsü: çekim başına elle üretim paketi. Ağ yok, ücretli modül yüklenmez, para harcanmaz.

Google Flow'un (Veo 3.1) programla erişilen bir API'si yoktur; kredileri yalnızca Flow'un web arayüzünde harcanır.
Bu yüzden hat Flow'u çağırmaz: yönetmen paketi indirir, klipleri Flow'da üretir, indirdiği klipleri panelden toplu
yükler. Yüklenen klip dış çekim olarak girer (Faz 15: görüntü üretilmez; anomali kontrolü, ses, renk ve montaj
yine çalışır). Flow'un ürettiği ses atılır; diyalog ve müzik hattın kendisinden gelir.

Paket, fal motorlarının (fal_plan.py) aynı parçalarını kullanır: onaylı referanslar, onaylı başlangıç ve bitiş kareleri,
bestecinin istemi ve negatiflerden pozitif kısıtlar. Fark: Flow'da referanslar numarayla değil, "Ingredients" alanına
eklenen görsellerle verilir; istem her varlığı adıyla ve tarifiyle yazar.

Flow modları:
- Frames to Video: onaylı başlangıç karesi varsa (yönetmenin yüklediği kare ya da onaylı storyboard karesi), bitiş
  karesi varsa o da.
- Ingredients to Video: kare yoksa; Veo 3.1 en çok 3 referans görseli alır (karakterler önce).

Kredi tahmini (Google Flow, Ekim 2026, Ultra dışı planlar; çıktı başına): Lite 10, Fast 20, Quality 100. Lite ve Fast
4, 6 ya da 8 sn; Quality yalnızca 8 sn. Flow kredisi abonelikten gelir, dolar bütçesine yazılmaz.
"""
import re
from dataclasses import dataclass, field

FLOW_CREDITS = {"lite": 10, "fast": 20, "quality": 100}
FLOW_SECONDS = {"lite": (4, 6, 8), "fast": (4, 6, 8), "quality": (8,)}
FLOW_MAX_INGREDIENTS = 3
FLOW_MODE_TR = {"frames": "Frames to Video", "ingredients": "Ingredients to Video", "text": "Text to Video"}
CODE = re.compile(r"S\d{3,}_SH\d{2,}", re.IGNORECASE)


def flow_seconds(duration_s: float | None, quality: str = "fast") -> tuple[int, str | None]:
    """Flow'da seçilecek süre ve (gerekirse) Türkçe not. Çekimden kısa olmayan en kısa seçenek; 8 sn'yi aşan çekim
    Flow'da uzatılır (Extend) ya da montajda kısalır."""
    d = float(duration_s or 5.0)
    options = FLOW_SECONDS[quality]
    for sec in options:
        if sec + 1e-6 >= d:
            return sec, None
    return options[-1], (f"Çekim {d:g} sn, Flow en çok {options[-1]} sn üretir: klibi Flow'da uzat (Extend) "
                         "ya da kısa klip yükle (montaj klibin süresini kullanır).")


def code_of(filename: str) -> str | None:
    """Dosya adındaki çekim kodu ("S001_SH02_flow.mp4" -> "S001_SH02"); yoksa None."""
    m = CODE.search(filename or "")
    return m.group(0).upper() if m else None


@dataclass
class FlowShot:
    code: str
    mode: str                                  # frames | ingredients | text
    prompt: str
    seconds: int
    duration_s: float
    start_key: str | None = None
    end_key: str | None = None
    ingredients: list[dict] = field(default_factory=list)      # [{"name", "kind", "key"}]
    notes: list[str] = field(default_factory=list)
    credits: int = 0

    def as_dict(self) -> dict:
        return {"code": self.code, "mode": self.mode, "mode_label": FLOW_MODE_TR[self.mode], "prompt": self.prompt,
                "seconds": self.seconds, "duration_s": self.duration_s, "start_key": self.start_key,
                "end_key": self.end_key, "ingredients": self.ingredients, "notes": self.notes, "credits": self.credits}


def _ingredient_sentence(r, names: list[str]) -> str:
    from app.hermes import translate
    from app.style.composer import sentence
    desc = translate.to_english(r.description, names) if r.description else ""
    if desc.lower().startswith(r.name.lower() + ","):
        desc = desc[len(r.name) + 1:].strip()
    label = {"character": r.name, "prop": f"The prop {r.name}", "location": f"The location {r.name}"}[r.kind]
    parts = [sentence(f"{label} looks exactly like the ingredient image \"{r.name}\": {desc}") if desc
             else sentence(f"{label} looks exactly like the ingredient image \"{r.name}\"")]
    if r.invariants:
        parts.append(sentence(f"{r.name} must always have: " + "; ".join(translate.to_english(i, names)
                                                                          for i in r.invariants)))
    if r.kind == "location":
        parts.append("Use the location image only for the look of the place, not for the camera position.")
    return " ".join(parts)


def shot(s, scene, quality: str = "fast") -> FlowShot:
    """Çekimin Flow paketi (kuru: hiçbir şey yüklenmez, hiçbir şey üretilmez)."""
    from app.config import project_settings
    from app.core.chain import plan_of
    from app.core.h3mode import strict_refs
    from app.core.hashing import visual_fingerprint
    from app.core.state import load_assets, load_characters
    from app.engine.fal_plan import references, start_frame, style_line
    from app.engine.iteration import compose_for_scene, last_frame_of
    from app.models import Project
    from app.style import bible
    from app.style.constraints import constrained_prompt
    cfg = project_settings(s.get(Project, scene.project_id).settings)
    chars, assets = load_characters(s, scene.id), load_assets(s, scene.id)
    style = bible.active(s, scene.project_id)
    version = style.version if style else None
    fp = visual_fingerprint(scene, chars, assets, version)
    refs = references(scene, chars, assets, strict_refs(cfg), FLOW_MAX_INGREDIENTS)
    start_key, _ = start_frame(scene, fp)
    end_key = last_frame_of(s, scene, version, cfg)[0] if start_key else None
    mode = "frames" if start_key else ("ingredients" if refs else "text")
    walk_in = plan_of(s, scene)["entering"] if scene.entry_mode == "walk_in" else None
    composed = compose_for_scene(scene, chars, assets, style, entering=walk_in)
    names = [ch.name for ch in chars] + [a.name for a in assets if a.type == "character"]
    body = composed.prompt
    if composed.prefix and body.startswith(composed.prefix):
        body = body[len(composed.prefix):].strip()
    lines = [style_line(style)]
    if mode == "ingredients":
        lines += [_ingredient_sentence(r, names) for r in refs]
    lines += [body, "No on-screen text, no subtitles, no music."]
    prompt, _ = constrained_prompt(" ".join(x for x in lines if x), composed.negative)
    seconds, note = flow_seconds(scene.duration_s, quality)
    notes = [note] if note else []
    total = len(references(scene, chars, assets, strict_refs(cfg), 99))
    if mode == "ingredients" and total > len(refs):
        notes.append(f"Flow en çok {FLOW_MAX_INGREDIENTS} malzeme görseli alır: {total - len(refs)} referans pakete "
                     "girmedi (karakterler önce).")
    if mode == "text":
        notes.append("Onaylı kare ya da referans görseli yok: Flow yalnızca metinden üretir, karakterler tutarsız olabilir.")
    from app.hermes.translate import looks_turkish
    if looks_turkish(body):
        notes.append("İstem Türkçe görünüyor: Veo İngilizce istemi daha iyi izler (Ayarlar: PROMPT_TRANSLATION=on, "
                     "yerel ve ücretsiz çeviri).")
    return FlowShot(code=scene.scene_code, mode=mode, prompt=prompt, seconds=seconds, duration_s=float(scene.duration_s or 0),
                    start_key=start_key, end_key=end_key,
                    ingredients=[{"name": r.name, "kind": r.kind, "key": r.key} for r in refs] if mode == "ingredients" else [],
                    notes=notes, credits=FLOW_CREDITS[quality])


README = """Google Flow paketi: {title}
=====================================

Bu paket hiçbir ücretli servisi çağırmaz. Klipleri Google Flow'da (labs.google/flow) kendin üretirsin;
krediler Flow aboneliğinden düşer.

Her çekim için:
1. Klasörü aç (ör. S001_SH01). istem.txt içindeki metni Flow'un istem alanına yapıştır.
2. Modu seç (istem.txt'nin başında yazar):
   - Frames to Video: ilk_kare.jpg'yi başlangıç karesi, varsa son_kare.jpg'yi bitiş karesi olarak ekle.
   - Ingredients to Video: malzemeler/ klasöründeki görselleri Ingredients alanına ekle.
3. Süreyi istem.txt'de yazan değere ayarla ({quality_label} önerilir), üret, beğendiğin klibi indir.
4. İndirdiğin dosyanın adına çekim kodunu koy (ör. S001_SH01.mp4 ya da S001_SH01_flow2.mp4).

Hepsi bitince panelde Storyboard sayfasındaki "Flow kliplerini yükle" düğmesiyle dosyaları birlikte seç.
Panel dosyaları çekim koduna göre eşler; her klip dış çekim olarak girer: görüntü üretilmez, anomali kontrolü,
ses, renk ve montaj yine çalışır. Flow'un kendi sesi atılır; diyalog ve müzik panelden gelir.

Tahmini kredi ({quality_label}, çıktı başına {per} kredi): {shots} çekim x {per} = {total} kredi.
Flow istem başına birden çok çıktı üretiyorsa (ör. 2 ya da 4) kredi o kadar katlanır.
Müşteriye teslimden önce Flow çıktılarının ticari kullanım ve filigran koşullarını kontrol et.
"""

QUALITY_TR = {"lite": "Veo 3.1 Lite", "fast": "Veo 3.1 Fast", "quality": "Veo 3.1 Quality"}


def prompt_file(sh: FlowShot, quality: str) -> str:
    head = [f"Çekim: {sh.code}", f"Flow modu: {FLOW_MODE_TR[sh.mode]}",
            f"Model: {QUALITY_TR[quality]} · süre: {sh.seconds} sn (çekim {sh.duration_s:g} sn)"]
    if sh.mode == "frames":
        head.append("Başlangıç karesi: ilk_kare.jpg" + (" · bitiş karesi: son_kare.jpg" if sh.end_key else ""))
    if sh.ingredients:
        head.append("Malzemeler: " + ", ".join(i["name"] for i in sh.ingredients))
    head += [f"Not: {n}" for n in sh.notes]
    return "\n".join(head) + "\n\n--- İstem (Flow'a yapıştır) ---\n" + sh.prompt + "\n"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("_") or "varlik"


def build_zip(title: str, shots: list[FlowShot], quality: str, out_path: str, work: str) -> str:
    """Paketi zip olarak yazar: BENIOKU.txt, plan.csv, çekim başına istem.txt, kareler ve malzemeler."""
    import csv
    import io
    import os
    import zipfile
    from app import storage

    def add_key(z, key: str, arc: str) -> None:
        if not key or not storage.exists(key):
            return
        ext = os.path.splitext(key)[1] or ".jpg"
        local = storage.download(key, os.path.join(work, _safe(arc) + ext))
        z.write(local, arc + ext)

    per = FLOW_CREDITS[quality]
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BENIOKU.txt", README.format(title=title, quality_label=QUALITY_TR[quality], per=per,
                                                shots=len(shots), total=per * len(shots)))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["cekim", "flow_modu", "sure_sn", "cekim_suresi_sn", "kredi", "notlar"])
        for sh in shots:
            w.writerow([sh.code, FLOW_MODE_TR[sh.mode], sh.seconds, f"{sh.duration_s:g}", sh.credits, " | ".join(sh.notes)])
        z.writestr("plan.csv", buf.getvalue())
        for sh in shots:
            z.writestr(f"{sh.code}/istem.txt", prompt_file(sh, quality))
            if sh.start_key:
                add_key(z, sh.start_key, f"{sh.code}/ilk_kare")
            if sh.end_key:
                add_key(z, sh.end_key, f"{sh.code}/son_kare")
            for i, ing in enumerate(sh.ingredients, start=1):
                add_key(z, ing["key"], f"{sh.code}/malzemeler/{i:02d}_{_safe(ing['name'])}")
    return out_path
