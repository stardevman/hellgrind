"""Stil kılavuzu: sürümler, etkin sürüm ve etki önizlemesi.

Çekimlerin aynı filme ait görünmesini en çok tutarlı ışık dili sağlar; bu yüzden ışık öneki, hareket tarzı, render
kuralları ve negatifler tek bir sürümlü kayıtta durur. Etkin sürüm görsel özete girer: kılavuz değişince etkilenen çekimler
yeniden üretilir. Bu yüzden değişiklik önce önizlenir (hangi çekimler, kaç sanal dolar) ve ancak yönetmen etkinleştirince
geçerli olur. Sürüm oluşturmak ve önizlemek hiçbir şey üretmez.
"""
from sqlalchemy import func, select, update

from app.budget import pricing
from app.config import project_settings
from app.core.hashing import visual_fingerprint
from app.core.state import load_assets, load_characters
from app.engine.providers import route_engine
from app.models import Project, Scene, StyleBible

# Hareket eden şeyler temiz 3D, çevre boyalı: yoğun boyalı doku hareket halinde bozulur.
DEFAULT_RULES = {"character": "clean_3d", "prop": "clean_3d", "location": "painterly"}
# 2D animasyonda karakter ve nesneler temiz çizgi + düz gölge (cel), arka planlar boyalı: sektörün yerleşik ayrımı.
DEFAULT_RULES_2D = {"character": "stylized_2d", "prop": "stylized_2d", "location": "painterly"}
MEDIA = ("2d", "3d")
MEDIUM_TR = {"2d": "2D", "3d": "3D"}
# Kılavuzu olmayan proje ve 030 öncesi sürümler: eski davranış (3D cümlesi ve kuralları).
LEGACY_MEDIUM = "3d"

# Panelde "Örnek" düğmesinin doldurduğu, profesyonel ekibin her çekimde tekrarladığı formül.
EXAMPLE = {
    "lighting_prefix": "Late-afternoon golden-hour lighting, low warm sun, long dramatic shadows, warm highlights, "
                       "strong depth and dimensionality.",
    "motion_style": "Cartoony motion, squash and stretch, exaggerated anticipation and follow-through.",
    "rules": dict(DEFAULT_RULES),
    "negative": "photorealistic skin, motion blur smear, flickering textures",
    "palette": ["#E4A23E", "#C2573F", "#2A2D31", "#86A5B8"],
    "notes": "Suluboya mekân + altın saat ışığı; hareket eden karakter ve nesneler temiz 3D.",
    "medium": "3d",
}
# Aynı formülün 2D hâli (animasyon dizisi): boyalı arka plan, temiz çizgili ve düz gölgeli karakterler.
EXAMPLE_2D = {
    **EXAMPLE,
    "rules": dict(DEFAULT_RULES_2D),
    "negative": "3D render, photorealistic skin, motion blur smear, flickering line art",
    "notes": "Suluboya arka plan + altın saat ışığı; karakter ve nesneler temiz çizgi, düz gölge (cel).",
    "medium": "2d",
}


def medium_of(style) -> str:
    """Kılavuzun çizim türü; kılavuz yoksa ya da alan boşsa eski davranış (3d)."""
    value = getattr(style, "medium", None) if style is not None else None
    return value if value in MEDIA else LEGACY_MEDIUM


def default_rules(style) -> dict:
    """Çizim türünün varsayılan render kuralları (kılavuzda o tür için kural yoksa kullanılır)."""
    return dict(DEFAULT_RULES_2D if medium_of(style) == "2d" else DEFAULT_RULES)


def active(s, project_id) -> StyleBible | None:
    return s.scalar(select(StyleBible).where(StyleBible.project_id == project_id, StyleBible.is_active))


def active_version(s, project_id) -> int | None:
    return s.scalar(select(StyleBible.version).where(StyleBible.project_id == project_id, StyleBible.is_active))


def versions(s, project_id) -> list[StyleBible]:
    return s.scalars(select(StyleBible).where(StyleBible.project_id == project_id)
                     .order_by(StyleBible.version.desc())).all()


def get_version(s, project_id, version: int) -> StyleBible | None:
    return s.scalar(select(StyleBible).where(StyleBible.project_id == project_id, StyleBible.version == version))


def create(s, project_id, **fields) -> StyleBible:
    """Yeni (etkin olmayan) sürüm. Proje satırı kilitlenir: eşzamanlı iki kayıt aynı sürüm numarasını almaz."""
    s.execute(select(Project.id).where(Project.id == project_id).with_for_update())
    number = (s.scalar(select(func.max(StyleBible.version)).where(StyleBible.project_id == project_id)) or 0) + 1
    row = StyleBible(project_id=project_id, version=number, is_active=False, **fields)
    s.add(row)
    s.flush()
    return row


def activate(s, project_id, version: int) -> None:
    """Önce eskisini kapatır, sonra yenisini açar (proje başına tek etkin sürüm kısmi benzersiz indeksle korunur)."""
    s.execute(update(StyleBible).where(StyleBible.project_id == project_id, StyleBible.is_active).values(is_active=False))
    s.flush()
    s.execute(update(StyleBible).where(StyleBible.project_id == project_id, StyleBible.version == version)
              .values(is_active=True))


def mark_stale(s, project_id, previous: int | None, version: int) -> list[str]:
    """Stil değişince tamamlanmış çekimler yeniden üretilmez, "eski stil" diye işaretlenir (scenes.style_stale). Yeni
    stilde görsel özeti değişmeyen çekimlerin işareti kalkar (ör. eski sürüme dönüldü). Dönen: işaretli çekim kodları."""
    marked = []
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id, Scene.visual_hash.is_not(None))):
        fresh = visual_fingerprint(sc, load_characters(s, sc.id), load_assets(s, sc.id), version)
        if fresh == sc.visual_hash:
            sc.style_stale = None
            continue
        held = stale_view(sc)
        sc.style_stale = {"version": held["version"] if held else previous, "visual_hash": sc.visual_hash,
                          "fresh": fresh}
        marked.append(sc.scene_code)
    return marked


def stale_view(sc) -> dict | None:
    """Geçerli "eski stil" işareti ({"version": klibin stil sürümü}); çekim yeniden üretildiyse None."""
    m = sc.style_stale or None
    if not m or m.get("visual_hash") != sc.visual_hash:
        return None
    return {"version": m.get("version")}


def style_held(sc, fresh: str) -> bool:
    """Bu çekimin görsel özeti YALNIZCA stil yüzünden mi değişti (işaretten bu yana başka girdi değişmedi). Öyleyse
    toplu ve otomatik dağıtımlar görüntüyü yeniden üretmez; yönetmen ayrıca ister (restyle)."""
    m = sc.style_stale or None
    return bool(m and m.get("visual_hash") == sc.visual_hash and m.get("fresh") == fresh)


def effective_style(asset, bible) -> str:
    """Varlığın render stili: varlığa özel istisna, yoksa kılavuzun tür kuralı, o da yoksa çizim türünün varsayılanı."""
    rules = (bible.rules if bible is not None else None) or {}
    return asset.render_style or rules.get(asset.type) or default_rules(bible)[asset.type]


def visual_cost(cfg: dict, engine: str, duration: float) -> tuple[float, float]:
    """Bir çekimin görsel aşamasının sanal maliyeti: (beklenen, en kötü). Beklenen: bir test turu ve bir tam dalga.
    En kötü: tüm düzeltme turları ve `full_n` tam varyasyon."""
    from app.engine.fal_plan import is_fal, resolution
    if is_fal(engine):                           # fal: çekim başına tek klip (kalite kapısından kalırsa fal_clip_attempts)
        clip = pricing.estimate_video(engine, resolution(engine, None, cfg), duration)
        return round(clip, 4), round(clip * max(1, int(cfg.get("fal_clip_attempts", 2))), 4)
    probe = pricing.estimate_video(engine, cfg["probe_resolution"], duration)
    full = pricing.estimate_video(engine, cfg["full_resolution"], duration)
    expected = cfg["probe_n"] * probe + min(cfg["wave_size"], cfg["full_n"]) * full
    worst = (cfg["max_correction_rounds"] + 1) * cfg["probe_n"] * probe + cfg["full_n"] * full
    return round(expected, 4), round(worst, 4)


def impact(s, project_id, version: int) -> dict:
    """`version` etkinleşirse ne olur: üretilmiş hangi çekimler yeniden üretilir ve tahmini sanal maliyeti.
    Henüz üretilmemiş çekimler ayrıca listelenir (ilk üretimlerinde bu stil kullanılır). Hiçbir şey yazmaz."""
    cfg = project_settings(s.get(Project, project_id).settings)
    current = active_version(s, project_id)
    affected, pending = [], []
    expected_total = worst_total = 0.0
    for sc in s.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq_order, Scene.scene_code)):
        if sc.visual_hash is None:
            pending.append(sc.scene_code)
            continue
        chars, assets = load_characters(s, sc.id), load_assets(s, sc.id)
        if visual_fingerprint(sc, chars, assets, version) == sc.visual_hash:
            continue
        expected, worst = visual_cost(cfg, sc.engine or route_engine(sc, cfg), sc.duration_s)
        expected_total, worst_total = expected_total + expected, worst_total + worst
        affected.append({"scene_code": sc.scene_code, "scene_id": str(sc.id), "status": sc.status,
                         "style_only": visual_fingerprint(sc, chars, assets, current) == sc.visual_hash,
                         "expected_usd": expected, "worst_usd": worst})
    return {"version": version, "active_version": current, "affected": affected, "pending": pending,
            "expected_usd": round(expected_total, 4), "worst_usd": round(worst_total, 4)}
