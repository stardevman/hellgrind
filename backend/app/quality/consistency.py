"""Süreklilik kontrolü (Faz 16): ardışık çekimler arasında renk sıçraması.

Karar karakter dışı bölgenin CIELAB ölçüsüyle verilir (post/color.py: üst %22, sol %28, sağ %18): ΔL > 3 ya da ΔE > 4
"renk atlaması"dır; skor = en büyük(|ΔL| / 3, ΔE / 4), 1'i aşarsa atlama. Sıcaklık da aynı bölgenin b farkıdır (karakter
cildi ve lamba gibi içerik ölçüyü etkilemez). Kıvılcım kalibrasyonu: renklendirilmemiş SH01 -> SH02 ΔL -4,2 / ΔE 4,4
(atlama), diğer ardışık çekimler ΔE 0,4-1,3. Eski ölçü (tüm kare HSV ortalamaları, ölçekleri 40/40/80) SH01 -> SH02'yi
0,40 ile kaçırıyordu; histogram mesafesi ve HSV farkları raporda bilgi olarak kalır.

Her çekimin montajlı videosunun orta karesinden HSV histogramları çıkarılır (ton doygunlukla ağırlıklı 12 kutu,
doygunluk 8 kutu, parlaklık 8 kutu) ve Bhattacharyya mesafesi raporlanır. Histogram mesafesi kompozisyon farkını da
ölçtüğü için (aynı sahnede geniş ve yakın çekim) karar, histogramlardan türetilen ortalamalarla verilir: parlaklık,
sıcaklık (kırmızı - mavi) ve doygunluk farkı, ölçeklerine bölünür; en büyüğü 1'i aşarsa renk sıçramasıdır. Ölçekler,
aynı paletli farklı çekimler arasındaki farkın üstünde seçildi (parlaklık ve sıcaklıkta en çok ~27, doygunlukta ~55).

Yalnızca aynı sekanstaki ardışık çekimler karşılaştırılır: sekans geçişlerindeki renk değişimi çoğunlukla bilinçlidir.

İçerik (continuous dikiş): renk/parlaklık aynı olsa da görüntü dikişte zıplayabilir (ör. karakter son karede başka yerde).
Geçişi continuous olan çekimde A'nın son karesi ile B'nin ilk karesi yapısal olarak karşılaştırılır (gri, bulanık,
96x54; ortalaması çıkarılmış normalize çapraz korelasyon, 0-1): SEAM_CONTENT_MIN'in altı "içerik sıçraması".

Öneri: sonraki çekimin renk ayarı, önceki çekimin ortalama parlaklık ve sıcaklığına yaklaşacak şekilde aranır. Aday
ayarlar sonraki çekimin renklendirilmemiş (dudak senkronlu) karesine GRADE aşamasıyla birebir aynı filtreyle uygulanıp
ölçülür; böylece önerilen ayar uygulanınca ölçülecek sonuç tahmin değil, ölçümdür.
"""
import logging
import os
import tempfile
import time

import numpy as np
from PIL import Image
from sqlalchemy import select

from app import storage
from app.config import settings
from app.core.hashing import grade_settings
from app.db import session_scope
from app.models import Project, Scene
from app.post import color
from app.post.grade import clamp, mid_frame
from app.quality.anomaly import probe_duration

log = logging.getLogger("hellgrind.consistency")

SCALE = {"brightness": 40.0, "warmth": 40.0, "saturation": 80.0}
JUMP_SCORE = 1.0
EXPOSURE_STEPS = (-2.0, -1.5, -1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
FINE_EXPOSURE_STEPS = (-0.125, 0.0, 0.125)
TEMPERATURES = (3000, 3500, 4000, 5000, 6500, 8000, 10000, 12000)
SATURATIONS = (0.6, 0.8, 1.0, 1.2, 1.4)
SEAM_CONTENT_MIN = 0.6         # continuous dikişte son kare / ilk kare yapısal benzerliğinin alt sınırı


# ---------- ölçüm ----------

def frame_stats(img: np.ndarray) -> dict:
    """HSV histogramları ve onlardan türetilen ortalamalar."""
    hsv = np.asarray(Image.fromarray(img).convert("HSV")).reshape(-1, 3).astype(float)
    hue = np.histogram(hsv[:, 0], bins=12, range=(0, 256), weights=hsv[:, 1] + 1)[0].astype(float)
    sat = np.histogram(hsv[:, 1], bins=8, range=(0, 256))[0].astype(float)
    val = np.histogram(hsv[:, 2], bins=8, range=(0, 256))[0].astype(float)
    centers = (np.arange(8) + 0.5) * 32
    rgb = img.reshape(-1, 3).astype(float)
    return {"hist": [h / h.sum() for h in (hue, sat, val)],
            "brightness": float((val / val.sum() * centers).sum()),
            "saturation": float((sat / sat.sum() * centers).sum()),
            "warmth": float(rgb[:, 0].mean() - rgb[:, 2].mean()), "lab": color.stats(img),
            "lab_outer": color.stats(img, "outer")}


def content_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """İki karenin yapısal benzerliği (0-1): gri, hafif bulanık 96x54 küçültme, ortalaması çıkarılmış kosinüs (NCC)
    0-1'e taşınır. Aynı kare 1, ilgisiz kareler ~0,5, ters görüntü 0."""
    from PIL import ImageFilter

    def vec(img: np.ndarray) -> np.ndarray:
        g = Image.fromarray(img).convert("L").resize((96, 54)).filter(ImageFilter.GaussianBlur(1))
        v = np.asarray(g, float).ravel()
        v -= v.mean()
        n = np.linalg.norm(v)
        return v / n if n > 1e-6 else v

    x, y = vec(a), vec(b)
    if not x.any() or not y.any():
        return 1.0 if not x.any() and not y.any() else 0.5
    return round(float((x @ y + 1) / 2), 3)


def bhattacharyya(p: np.ndarray, q: np.ndarray) -> float:
    return float(np.sqrt(max(0.0, 1.0 - float(np.sum(np.sqrt(p * q))))))


NEW_SCENE_SLACK = 2.0          # yeni sahne (mekân ya da zaman değişir): farklı bir görünüm beklenir, yalnızca büyük sıçrama


def compare(a: dict, b: dict, camera_change: bool = False, new_scene: bool = False) -> dict:
    """b - a. delta: parlaklık (ΔL), sıcaklık (Δb) ve ΔE karakter dışı bölgenin LAB ölçüsünden; doygunluk tüm kare HSV
    ortalamasından (bilgi). camera_change: kamerası değişen kesme (cut-in, yeni açı): şerit ve merkez dışı ölçülerden
    küçüğü, eşik color.CAMERA_CHANGE_SLACK kadar gevşek (sabit şeritler farklı içerik görür). new_scene: yeni sahne
    başlığıyla gelen çekim; mekân ya da zaman değiştiği için eşik NEW_SCENE_SLACK kadar gevşek."""
    lab = color.difference(a["lab"], b["lab"])
    slack, region = 1.0, "strips"
    if camera_change and "lab_outer" in a and "lab_outer" in b:
        slack = color.CAMERA_CHANGE_SLACK
        outer = color.difference(a["lab_outer"], b["lab_outer"])
        if color.jump_score(outer) < color.jump_score(lab):
            lab, region = outer, "outer"
    if new_scene:
        slack = max(slack, NEW_SCENE_SLACK)
    score = color.jump_score(lab, slack)
    deltas = {"brightness": lab["L"], "warmth": lab["b"], "E": lab["E"]}
    out = {"score": round(score, 3), "delta": deltas, "lab": lab, "region": region, "slack": slack,
           "jump": score > JUMP_SCORE}
    if "saturation" in a and "saturation" in b:          # bilgi: tüm kare HSV (LAB'li hedefte yok)
        deltas["saturation"] = round(b["saturation"] - a["saturation"], 1)
        out["histogram_distance"] = round(float(np.mean([bhattacharyya(x, y) for x, y in zip(a["hist"], b["hist"])])), 3)
    return out


# ---------- öneri ----------

CONTRASTS = (0.85, 0.92, 1.0, 1.08, 1.16, 1.25)
AUTO_BOUNDS = {"exposure": (-0.75, 0.75), "contrast": (0.85, 1.25), "saturation": (0.7, 1.4), "temperature": (5000, 8000)}


def _bounded(g: dict, bounds: dict | None) -> dict:
    g = clamp(g)
    for k, (lo, hi) in (bounds or {}).items():
        g[k] = min(max(g[k], lo), hi)
    return g


def suggest(target: dict, source: str, at: float, current: dict, lut: str | None,
            bounds: dict | None = None) -> tuple[dict, float]:
    """Sonraki çekim için renk ayarı: karakter dışı bölgenin LAB ölçüsü hedefe en yakın sonucu veren aday (koordinat araması:
    pozlama, kontrast, sıcaklık, doygunluk, ince pozlama; amaç ΔE + 0,3 × |ΔsdL|). Aday ayarlar renklendirilmemiş kareye
    GRADE aşamasıyla aynı filtreyle uygulanıp ölçülür. bounds: otomatik eşleştirmenin sınırları. (ayar, o ayarla skor)."""
    cache: dict[str, float] = {}

    def cost(g: dict) -> float:
        key = repr(sorted(g.items()))
        if key not in cache:
            d = color.difference(target["lab"], frame_stats(mid_frame(source, at, g, lut))["lab"])
            # parlaklık önce (atlamanın ana sebebi ve renk ayarının en iyi düzelttiği), sonra ΔE ve sapma
            cache[key] = abs(d["L"]) + d["E"] + 0.3 * abs(d["sdL"])
        return cache[key]

    best = _bounded(current, bounds)
    for _ in range(2):                                   # iki tur: sıcaklık değişince pozlamanın en iyisi de kayar
        for field, values in (("exposure", [best["exposure"] + d for d in EXPOSURE_STEPS]),
                              ("contrast", CONTRASTS + (best["contrast"],)),
                              ("temperature", TEMPERATURES + (best["temperature"],)),
                              ("saturation", SATURATIONS + (best["saturation"],))):
            best = min((_bounded({**best, field: v}, bounds) for v in values), key=cost)
    best = min((_bounded({**best, "exposure": best["exposure"] + d}, bounds) for d in FINE_EXPOSURE_STEPS), key=cost)
    graded = frame_stats(mid_frame(source, at, best, lut))
    return best, compare(target, graded)["score"]


# ---------- proje raporu ----------

def camera_changed(a: Scene, b: Scene) -> bool:
    """b kesmeyle gelir ve kamera değişir (cut-in, açı LoRA'sı, kesme kamerası ya da kural farkı)."""
    from app.core.chain import camera_of, cut_differs, transition_of
    return transition_of(b) == "cut" and (getattr(b, "board_method", None) is not None
                                          or cut_differs(camera_of(a), camera_of(b)))


def _pairs(scenes: list[Scene]) -> list[tuple[Scene, Scene]]:
    """Aynı sekanstaki ardışık tamamlanmış çekimler (filmden çıkarılanlar atlanır: filmde komşu olanlar karşılaştırılır)."""
    done = [sc for sc in scenes if sc.status == "done" and sc.final_video_key and sc.in_cut is not False]
    return [(a, b) for a, b in zip(done, done[1:]) if a.sequence_id == b.sequence_id]


def seam_frames(path: str) -> tuple[np.ndarray, np.ndarray]:
    """(ilk kare, son kare). Son kare: son yarım saniyenin çözülen en son karesi (kısa klipte de bulunur)."""
    import subprocess
    from app.post.grade import FRAME_H, FRAME_W
    first = mid_frame(path, 0.0)
    out = subprocess.run(["ffmpeg", "-v", "error", "-sseof", "-0.5", "-i", path, "-vf", f"scale={FRAME_W}:{FRAME_H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    size = FRAME_W * FRAME_H * 3
    if len(out) < size:
        return first, first
    return first, np.frombuffer(out[-size:], np.uint8).reshape(FRAME_H, FRAME_W, 3)


def check_project(project_id: str, only_scene: str | None = None) -> dict:
    """Süreklilik raporunu üretir ve projeye yazar. only_scene: yalnızca o çekimin komşu geçişleri yeniden ölçülür.
    continuous dikişlerde içerik benzerliği de ölçülür (content, content_jump)."""
    from app.core.chain import transition_of
    with session_scope() as s:
        project = s.get(Project, project_id)
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id)
                           .order_by(Scene.seq_order, Scene.scene_code)).all()
        previous = {(p["from_id"], p["to_id"]): p for p in (project.continuity or {}).get("pairs", [])}
        lut_key = project.lut_key
        s.expunge_all()
    pairs = _pairs(scenes)
    os.makedirs(settings.work_dir, exist_ok=True)
    out = []
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        lut = storage.download(lut_key, os.path.join(tmp, "film.cube")) if lut_key else None
        stats_cache: dict[str, dict] = {}
        edges_cache: dict[str, tuple] = {}

        def edges(key: str) -> tuple:
            if key not in edges_cache:
                path = storage.download(key, os.path.join(tmp, f"e{len(edges_cache)}.mp4"))
                edges_cache[key] = seam_frames(path)
            return edges_cache[key]

        def measured(key: str) -> dict:
            if key not in stats_cache:
                path = storage.download(key, os.path.join(tmp, f"{len(stats_cache)}.mp4"))
                stats_cache[key] = frame_stats(mid_frame(path, probe_duration(path) / 2))
            return stats_cache[key]

        for a, b in pairs:
            ids = (str(a.id), str(b.id))
            old = previous.get(ids)
            if only_scene and only_scene not in ids and old is not None and old.get("final") == [a.final_video_key, b.final_video_key] \
                    and ("content" in old) == (transition_of(b) == "continuous"):
                out.append(old)
                continue
            result = compare(measured(a.final_video_key), measured(b.final_video_key), camera_changed(a, b),
                             new_scene=transition_of(b) == "new_scene")
            pair = {"from": a.scene_code, "to": b.scene_code, "from_id": ids[0], "to_id": ids[1],
                    "final": [a.final_video_key, b.final_video_key], **result,
                    "suggestion": None, "suggestion_score": None, "seam": transition_of(b)}
            if transition_of(b) == "continuous":                # dikiş: A'nın son karesi B'nin ilk karesine uymalı
                pair["content"] = content_similarity(edges(a.final_video_key)[1], edges(b.final_video_key)[0])
                pair["content_jump"] = pair["content"] < SEAM_CONTENT_MIN
            if pair["jump"] and b.lipsync_video_key:
                source = storage.download(b.lipsync_video_key, os.path.join(tmp, f"src_{b.id}.mp4"))
                grade, score = suggest(measured(a.final_video_key), source, probe_duration(source) / 2,
                                       grade_settings(b.grade), lut)
                pair["suggestion"], pair["suggestion_score"] = grade, round(score, 3)
            out.append(pair)
    report = {"checked_at": time.time(), "threshold": JUMP_SCORE, "jump_dl": color.JUMP_DL, "jump_de": color.JUMP_DE,
              "content_threshold": SEAM_CONTENT_MIN,
              "pairs": out}
    with session_scope() as s:
        s.get(Project, project_id).continuity = report
    log.info("süreklilik project=%s geçiş=%d sıçrama=%d", project_id, len(out), sum(p["jump"] for p in out))
    return report



# ---------- otomatik çekim eşleştirme ----------

AUTO_MIN_DE = 2.0              # referansa bundan yakın çekime dokunulmaz (gereksiz renklendirme ve yeniden montaj yok)


def _manual(sc) -> bool:
    """Yönetmenin renk ayarı (otomatik eşleştirmenin uyguladığı değil) ya da eşleştirmesi geri alınmış çekim."""
    auto = sc.grade_auto or {}
    return bool(auto.get("disabled")) or (bool(sc.grade) and not auto.get("applied"))


def auto_match(project_id: str) -> list[str]:
    """Değişen çekimlerin kimlikleri (auto_match_report)."""
    return auto_match_report(project_id)["changed"]


def auto_match_report(project_id: str) -> dict:
    """Sekans başına referans çekim seçilir (karakter dışı LAB ölçüsü sekansın medyanına en yakın olan; ilk çekim değil),
    diğer çekimlerin renk ayarı referansa göre çözülür (pozlama, kontrast, doygunluk, sıcaklık; AUTO_BOUNDS içinde) ve
    yazılır. Yönetmenin elle ayarladığı ya da eşleştirmeyi geri aldığı çekime dokunulmaz. {"changed": [çekim kimlikleri]
    (çağıran GRADE + MUX çalıştırır), "skipped": [{"code", "reason"}], "references": {sekans: referans çekim kodu}}:
    atlanan her çekimin sebebi olaya yazılır (Kıvılcım: düğme "kuyruğa alındı" dedi, hiçbir şey değişmedi, iz yoktu)."""
    from app.core.hashing import GRADE_IDENTITY
    with session_scope() as s:
        scenes = s.scalars(select(Scene).where(Scene.project_id == project_id)
                           .order_by(Scene.seq_order, Scene.scene_code)).all()
        lut_key = s.get(Project, project_id).lut_key
        s.expunge_all()
    groups: dict = {}
    skipped: list[dict] = []
    references: dict = {}
    for sc in scenes:
        if sc.status == "done" and sc.final_video_key and sc.lipsync_video_key:
            groups.setdefault(sc.sequence_id, []).append(sc)
        else:
            skipped.append({"code": sc.scene_code, "reason": "bitmedi (final video yok)"})
    changed = []
    os.makedirs(settings.work_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp:
        lut = storage.download(lut_key, os.path.join(tmp, "film.cube")) if lut_key else None
        for shots in groups.values():
            if len(shots) < 2:
                skipped += [{"code": sc.scene_code, "reason": "sekansta tek bitmiş çekim"} for sc in shots]
                continue
            looks = {}
            for sc in shots:
                path = storage.download(sc.final_video_key, os.path.join(tmp, f"f_{sc.id}.mp4"))
                looks[sc.id] = frame_stats(mid_frame(path, probe_duration(path) / 2))["lab"]
            median = {k: float(np.median([v[k] for v in looks.values()])) for k in ("L", "a", "b")}
            ref = min(shots, key=lambda sc: sum((looks[sc.id][k] - median[k]) ** 2 for k in ("L", "a", "b")))
            references[str(ref.sequence_id)] = ref.scene_code
            for sc in shots:
                if sc.id == ref.id:
                    skipped.append({"code": sc.scene_code, "reason": "referans çekim"})
                    continue
                if _manual(sc):
                    skipped.append({"code": sc.scene_code, "reason": "eşleştirme geri alınmış" if (sc.grade_auto or {})
                                    .get("disabled") else "renk elle ayarlı"})
                    continue
                before = color.difference(looks[ref.id], looks[sc.id])
                if before["E"] <= AUTO_MIN_DE and abs(before["L"]) <= color.JUMP_DL / 2:
                    skipped.append({"code": sc.scene_code, "reason": f"referansa zaten yakın (ΔE {before['E']:.1f})"
                                    .replace(".", ",")})
                    continue
                source = storage.download(sc.lipsync_video_key, os.path.join(tmp, f"s_{sc.id}.mp4"))
                start = grade_settings(sc.grade) if sc.grade else dict(GRADE_IDENTITY)
                solved, score = suggest({"lab": looks[ref.id]}, source, probe_duration(source) / 2, start, lut,
                                        bounds=AUTO_BOUNDS)
                after = color.difference(looks[ref.id], frame_stats(mid_frame(source, probe_duration(source) / 2,
                                                                              solved, lut))["lab"])
                if after["E"] >= before["E"]:
                    skipped.append({"code": sc.scene_code, "reason": f"sınırlar içinde iyileşmedi (ΔE {before['E']:.1f} → "
                                    f"{after['E']:.1f}; fark içerikten olabilir)".replace(".", ",")})
                    continue
                with session_scope() as s:
                    row = s.get(Scene, sc.id)
                    previous = (row.grade_auto or {}).get("previous", row.grade or {}) if (row.grade_auto or {}).get(
                        "applied") else (row.grade or {})
                    row.grade = solved
                    row.grade_auto = {"applied": True, "previous": previous, "reference": ref.scene_code,
                                      "delta_e_before": before["E"], "delta_e_after": after["E"], "at": time.time()}
                changed.append(str(sc.id))
                log.info("otomatik renk %s -> %s: ΔE %.2f -> %.2f", sc.scene_code, ref.scene_code, before["E"], after["E"])
    return {"changed": changed, "skipped": skipped, "references": references}
