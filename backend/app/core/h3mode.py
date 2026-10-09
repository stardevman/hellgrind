"""Çekimin MiniMax H3 koşulu ve kimlik beklentisi (ağ yok; planlayıcı, görsel özet, üretim ve panel aynı kuralı kullanır).

- **Koşul (h3_mode):** yönetmen seçtiyse o. Seçmediyse: kendi ilk karesini yüklediyse FL2VA; çekimde onaylı referansı olan
  bir karakter varsa Ref2VA (storyboard karesi metinden çizilir, karakter referansı içermez: FL2VA'da kimlik kayar);
  yoksa FL2VA.
- **Yüz beklentisi (face_expected):** yönetmen seçtiyse o. Seçmediyse karakter varsa evet; kamera aşırı geniş plansa ya da
  tarif sırt çekimi/silüet diyorsa hayır.
"""
import re

APPROVED = ("still_approved", "motion_tested", "locked")
NO_FACE = re.compile(r"\b(from behind|back to (the )?camera|seen from the back|silhouett\w*|sırt\w*|arkadan|"
                     r"silüet\w*|siluet\w*)", re.I)


def has_approved_reference(chars, assets=()) -> bool:
    by_id = {a.id: a for a in assets or ()}
    for ch in chars or ():
        if not getattr(ch, "reference_images", None):
            continue
        asset = by_id.get(getattr(ch, "asset_id", None))
        if getattr(ch, "asset_id", None) is None or (asset is not None and asset.status in APPROVED):
            return True
    return False


def effective_mode(scene, chars=(), assets=()) -> str:
    explicit = getattr(scene, "h3_mode", None)
    if explicit in ("fl2va", "ref2va"):
        return explicit
    if getattr(scene, "keyframe_key", None):
        return "fl2va"
    return "ref2va" if has_approved_reference(chars, assets) else "fl2va"


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


def fl2va_identity_warning(scene, chars=(), assets=()) -> str | None:
    """FL2VA seçili ve çekimde karakter var: ilk kare metinden çizilir, karakter referansı kullanılmaz."""
    if chars and effective_mode(scene, chars, assets) == "fl2va" and not getattr(scene, "keyframe_key", None):
        return "Storyboard karesi karakter referansı kullanmıyor; kimlik kayabilir."
    return None
