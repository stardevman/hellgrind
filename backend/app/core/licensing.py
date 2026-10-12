"""MiniMax H3 lisans koruması (MiniMax H3 Community License).

§V.3: H3 ya da çıktıları MiniMax H3 ve onun türevleri dışındaki hiçbir yapay zekâ modelini geliştirmek için kullanılamaz.
Hattın model eğitimine giden tek yolu kürasyon kaydıdır (Faz 17: projeye özel kalite modeli için eğitim verisi). Bu
yüzden kayıt dışa aktarılırken gerçek H3 çıktılarından türeyen kararlar (bir H3 klibine bağlı olanlar ve H3 klibi üretilmiş
sahnelerin sahne düzeyindeki kararları) eğitim verisinden çıkarılır. Hatta LoRA ya da başka model eğitimi yapan kod yoktur;
H3 dönüş videosu kareleri yalnızca H3'e referans olarak (Ref2VA) ve yerel skorlayıcının ölçümünde (eğitim değil) kullanılır.

Yalnızca gerçek H3 çıktıları kısıtlıdır: ücretsiz modda "h3" motorlu çekimler yerel prosedürel motorla çizilir (istek
kimliği "local-" ile başlar), H3 çıktısı değildir.
"""
from dataclasses import dataclass, field

from sqlalchemy import or_, select

from app.models import Asset, Scene, Variation

NOTICE = """MiniMax H3 çıktıları

Bu paketteki aşağıdaki çekimler MiniMax H3 ile üretildi (MiniMax H3 Community License):
{shots}

- Çıktılar MiniMax H3 ve türevleri dışındaki hiçbir yapay zekâ modelini geliştirmek ya da eğitmek için kullanılamaz (§V.3).
  Bu yüzden kurasyon.jsonl bu çekimlerin kararlarını içermez.
- Çıktılar Avrupa Birliği, Birleşik Krallık, Güney Kore ve ABD'de MiniMax'in ayrı izni olmadan kullanılamaz, dağıtılamaz
  ve gösterilemez (§V.4, Excluded Territories).
- Ticari bir ürün ya da hizmette arayüzde "MiniMax H3" adı görünmelidir (§IV.2).
Lisans: https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE
"""


@dataclass
class Restricted:
    variations: set[str] = field(default_factory=set)
    scenes: set[str] = field(default_factory=set)
    provenance: dict[str, dict] = field(default_factory=dict)      # sahne kodu -> kaynak bilgisi (rafine #1'den)

    def covers(self, scene_id, variation_id) -> bool:
        if variation_id is not None:
            return str(variation_id) in self.variations
        return scene_id is not None and str(scene_id) in self.scenes


def _real_h3():
    # Yalnızca kendi barındırdığımız MiniMax H3 (Community License). fal.ai'deki H3 Max "Commercial use" etiketlidir:
    # kısıt yok (yönetmenin kararı, 2026-10-12).
    return (Variation.engine == "h3") & Variation.external_request_id.is_not(None) \
        & ~Variation.external_request_id.startswith("local-")


def restricted(s, project_id) -> Restricted:
    """Projenin gerçek H3 çıktıları (çekim varyasyonları ve varlık hareket testleri) ve bunların sahneleri."""
    out = Restricted()
    rows = s.execute(select(Variation.id, Variation.scene_id, Variation.refined_rank, Variation.params, Scene.scene_code)
                     .outerjoin(Scene, Scene.id == Variation.scene_id).outerjoin(Asset, Asset.id == Variation.asset_id)
                     .where(_real_h3(), or_(Scene.project_id == project_id, Asset.project_id == project_id))).all()
    for vid, scene_id, rank, params, code in rows:
        out.variations.add(str(vid))
        if scene_id is not None:
            out.scenes.add(str(scene_id))
            prov = ((params or {}).get("provider_meta") or {}).get("provenance")
            if prov and (rank == 1 or code not in out.provenance):
                out.provenance[code] = prov
    return out


def notice(r: Restricted) -> str | None:
    """Paket için bildirim metni; H3 çıktısı yoksa None."""
    if not r.provenance and not r.scenes:
        return None
    lines = []
    for code, p in sorted(r.provenance.items()):
        lines.append(f"- {code}: {p.get('model')} {p.get('model_revision') or ''} · {p.get('task')} · arka uç "
                     f"{p.get('backend')} · sunucu konumu {p.get('server_location') or 'bilinmiyor'}".replace("  ", " "))
    return NOTICE.format(shots="\n".join(lines) or "- (kaynak bilgisi kayıtlı değil)")
