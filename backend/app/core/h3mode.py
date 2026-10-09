"""Çekimin MiniMax H3 koşulu, ilk karesinin modeli ve kimlik beklentisi (ağ yok; planlayıcı, görsel özet, üretim ve panel
aynı kuralı kullanır).

- **Koşul (h3_mode):** yönetmen seçtiyse o; seçmediyse FL2VA. Karakterli çekimde kimliği ilk kare taşır: kare
  Qwen-Image-Edit-2511 ile karakterin onaylı referansından çizilir (image 1 ana karakter; image 2 / 3 ikinci karakter ya
  da mekân). Ref2VA yalnızca çekim bazında elle seçilir (kare çizilmez).
- **İlk kare modeli (keyframe_model):** onaylı referanslı karakter varsa ve Ayarlar'da KEYFRAME_CHARACTER_PROVIDER
  qwen_edit ise qwen_edit; değilse qwen (Qwen-Image, yalnızca metinden). "Onaylı" proje ayarına bağlıdır (strict):
  lint_strictness block ise varlık en az still_approved olmalı; warn/off ise (onay akışı atlanır, ör. deneme ön ayarı)
  yüklenmiş referans yeterlidir.
- **Yüz beklentisi (face_expected):** yönetmen seçtiyse o. Seçmediyse karakter varsa evet; kamera aşırı geniş plansa ya da
  tarif sırt çekimi/silüet diyorsa hayır.
"""
import re

APPROVED = ("still_approved", "motion_tested", "locked")
NO_FACE = re.compile(r"\b(from behind|back to (the )?camera|seen from the back|silhouett\w*|sırt\w*|arkadan|"
                     r"silüet\w*|siluet\w*)", re.I)


MAX_EDIT_IMAGES = 3                   # Qwen-Image-Edit-2511: image 1..3


def strict_refs(cfg: dict | None) -> bool:
    """Referans onay şartı: lint_strictness block (varsayılan) ise varlık onaylı olmalı; warn/off ise yüklü olması yeter."""
    return (cfg or {}).get("lint_strictness", "block") == "block"


def _approved(ch, by_id: dict, strict: bool = True) -> bool:
    asset = by_id.get(getattr(ch, "asset_id", None))
    return not strict or getattr(ch, "asset_id", None) is None or (asset is not None and asset.status in APPROVED)


def still_key(ch) -> str | None:
    """Karakterin referans görseli: durağan görsel, yoksa ilk referans görseli."""
    keys = list(getattr(ch, "reference_images", None) or [])
    return next((k for k in keys if "/still/" in k), keys[0] if keys else None)


def has_approved_reference(chars, assets=(), strict: bool = True) -> bool:
    by_id = {a.id: a for a in assets or ()}
    return any(getattr(ch, "reference_images", None) and _approved(ch, by_id, strict) for ch in chars or ())


def _mentioned_first(scene, chars) -> list:
    """Ana karakter önde: çekim tarifinde ilk adı geçen; geçmeyenler ad sırasıyla sonda."""
    text = " ".join(str(x) for x in ((getattr(scene, "prompt_spec", None) or {}).get("prompt", ""),
                                     getattr(scene, "description", None) or "")).lower()
    return sorted(chars, key=lambda ch: (text.find(ch.name.lower()) if ch.name.lower() in text else len(text) + 1,
                                         ch.name))


def edit_references(scene, chars=(), assets=(), strict: bool = True) -> list[dict]:
    """Qwen-Image-Edit karesinin görselleri, sırayla (image 1, 2, 3): onaylı referanslı karakterlerin durağan görselleri
    (ilki ana karakter), yer kalırsa onaylı mekânın durağan görseli. [{"name", "key", "kind": character|location}]."""
    by_id = {a.id: a for a in assets or ()}
    out = [{"name": ch.name, "key": key, "kind": "character"} for ch in _mentioned_first(scene, list(chars or ()))
           if _approved(ch, by_id, strict) and (key := still_key(ch))][:MAX_EDIT_IMAGES]
    if out and len(out) < MAX_EDIT_IMAGES:
        for a in assets or ():
            key = ((getattr(a, "references", None) or {}).get("still") or {}).get("key")
            if getattr(a, "type", None) == "location" and (not strict or a.status in APPROVED) and key:
                out.append({"name": a.name, "key": key, "kind": "location"})
                break
    return out


def keyframe_model(scene, chars=(), assets=(), strict: bool = True) -> str:
    """İlk karenin modeli: qwen_edit (karakterin onaylı referansından) ya da qwen (yalnızca metinden)."""
    from app.config import settings
    if settings.keyframe_character_provider == "qwen_edit" and edit_references(scene, chars, assets, strict):
        return "qwen_edit"
    return "qwen"


def effective_mode(scene, chars=(), assets=()) -> str:
    """Yönetmenin seçimi; seçmediyse FL2VA (karakterli çekimde ilk kare karakter referansından çizilir)."""
    explicit = getattr(scene, "h3_mode", None)
    return explicit if explicit in ("fl2va", "ref2va") else "fl2va"


def face_expected(scene, chars=()) -> bool:
    explicit = getattr(scene, "face_expected", None)
    if explicit is not None:
        return bool(explicit)
    if not chars:
        return False
    if ((getattr(scene, "camera", None) or {}).get("shot") or "") == "extreme_wide":
        return False
    text = " ".join(str(x) for x in ((getattr(scene, "prompt_spec", None) or {}).get("prompt", ""),
                                     getattr(scene, "description", None) or ""))
    return not NO_FACE.search(text)


def fl2va_identity_warning(scene, chars=(), assets=(), strict: bool = True) -> str | None:
    """Karakterli FL2VA çekiminin ilk karesi düz Qwen-Image'dan (yalnızca metinden) geldiyse ya da gelecekse: karakter
    referansı kullanılmadı. Yüklenen ilk karede, Qwen-Image-Edit / Flux Kontext karesinde ve Ref2VA'da uyarı yok."""
    if not chars or effective_mode(scene, chars, assets) != "fl2va" or getattr(scene, "keyframe_key", None):
        return None
    source = getattr(scene, "board_source", None)
    if source == "comfyui" or (source not in ("qwen_edit", "flux") and keyframe_model(scene, chars, assets, strict) == "qwen"):
        return "Storyboard karesi karakter referansı kullanmıyor; kimlik kayabilir."
    return None
