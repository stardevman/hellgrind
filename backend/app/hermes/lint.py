"""Üretim öncesi kontrol: model eksik bilgiyi uydurur.

Çekimde geçen her karakter, mekân ve nesnenin onaylı bir referansı yoksa görüntü üretimi başlamaz. Referans verilmeyen
bir taksiye model rastgele yolcular ekler; bu yüzden prompt'a "yalnızca listelenen karakterler" kısıtı da eklenir.

Proje ayarları:
  lint_strictness   off (kontrol yok) | warn (olay yazılır, üretim sürer) | block (varsayılan: sahne `blocked` olur)
  asset_min_status  çekimdeki varlıkların ulaşması gereken en az durum; varsayılan locked
"""
from dataclasses import dataclass, field

from sqlalchemy import select

from app.models import Asset, Character, SceneAsset, SceneCharacter

STATUS_ORDER = ("draft", "still_approved", "motion_tested", "locked")
TYPE_TR = {"character": "karakter", "location": "mekân", "prop": "nesne"}
STATUS_TR = {"draft": "taslak", "still_approved": "durağan onaylı", "motion_tested": "hareket testi onaylı",
             "locked": "kilitli", None: "varlık kaydı yok"}

CAST_NEGATIVE = "extra people, crowd, bystanders, passengers, unlisted characters"


@dataclass(frozen=True)
class Missing:
    asset_id: str | None      # karakterin varlık kaydı hiç yoksa None
    type: str
    name: str
    status: str | None


@dataclass
class LintResult:
    strictness: str
    min_status: str
    missing: list[Missing] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def blocks(self) -> bool:
        return self.strictness == "block" and bool(self.missing)

    def message(self) -> str:
        return describe(self.missing, self.min_status)


def rank(status: str | None) -> int:
    return STATUS_ORDER.index(status) if status in STATUS_ORDER else -1


def find_missing(items: list[tuple[str | None, str, str, str | None]], min_status: str) -> list[Missing]:
    """items: [(varlık kimliği, tür, ad, durum)] -> eşiğin altında kalanlar (tür ve ada göre sıralı, tekrarsız)."""
    need = rank(min_status)
    seen, out = set(), []
    for asset_id, type_, name, status in items:
        key = asset_id or f"{type_}:{name}"
        if key in seen or rank(status) >= need:
            continue
        seen.add(key)
        out.append(Missing(asset_id, type_, name, status))
    order = list(TYPE_TR)
    return sorted(out, key=lambda m: (order.index(m.type), m.name))


BEHIND = "behind"               # dizi varlığının bölümdeki sabit sürümü kütüphanenin gerisinde


def describe(missing: list[Missing], min_status: str) -> str:
    """Panelde ve `last_error` alanında gösterilen Türkçe açıklama."""
    if not missing:
        return ""
    late = [m for m in missing if m.status == BEHIND]
    rest = [m for m in missing if m.status != BEHIND]
    parts = []
    if rest:
        items = ", ".join(f"{m.name} ({TYPE_TR[m.type]}, {STATUS_TR.get(m.status, m.status)})" for m in rest)
        parts.append(f"Üretim engellendi: onaylı referansı olmayan varlıklar var: {items}. "
                     f"Gereken en az durum: {STATUS_TR[min_status]}.")
    if late:
        names = ", ".join(m.name for m in late)
        parts.append(f"{'Üretim engellendi: ' if not rest else ''}dizi kütüphanesinde yeni sürümü olan varlıklar var: "
                     f"{names}. Bölüm eski sürüme sabit; Varlıklar sayfasında etkisini görüp yeni sürüme geçin.")
    return " ".join(parts)


def scene_items(s, scene_id) -> list[tuple[str | None, str, str, str | None]]:
    """Çekimde geçen varlıklar: kadrodaki karakterler (varlıkları üzerinden) ve bağlı mekân ve nesneler."""
    chars = s.execute(select(Character.name, Asset.id, Asset.status)
                      .join(SceneCharacter, SceneCharacter.character_id == Character.id)
                      .outerjoin(Asset, Asset.id == Character.asset_id)
                      .where(SceneCharacter.scene_id == scene_id)).all()
    linked = s.execute(select(Asset.id, Asset.type, Asset.name, Asset.status)
                       .join(SceneAsset, SceneAsset.asset_id == Asset.id)
                       .where(SceneAsset.scene_id == scene_id)).all()
    return ([(str(aid) if aid else None, "character", name, status) for name, aid, status in chars]
            + [(str(aid), type_, name, status) for aid, type_, name, status in linked])


def check_scene(s, scene_id, cfg: dict) -> LintResult:
    strictness, min_status = cfg.get("lint_strictness", "block"), cfg.get("asset_min_status", "locked")
    result = LintResult(strictness, min_status)
    if strictness != "off":
        result.missing = find_missing(scene_items(s, scene_id), min_status)
    from app.core.series import behind                 # dizi varlığı: bölüm eski sürüme sabit, kütüphane ilerledi
    late = behind(s, scene_id)
    if late:                                           # lint kapalı olsa da: eski özetle yeni referans karışmasın
        result.strictness = "block"
        result.missing += [Missing(x["asset_id"], x["type"], x["name"], BEHIND) for x in late]
    return result


# ---------- prompt kısıtı (style/composer.py kullanır) ----------

def cast_constraint(names: list[str]) -> str:
    """Kadroda olmayan kişilerin üretilmemesi için prompt'a eklenen kısıt (sağlayıcılar İngilizce istemi daha iyi izler)."""
    if not names:
        return "No people or characters appear in this shot."
    return f"Only the listed characters appear: {', '.join(names)}. No other people or figures."

