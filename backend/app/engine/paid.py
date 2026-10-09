"""Ücretli çağrıların ortak yolu (Faz 18): kilit, rezervasyon, yeniden deneme.

Önce `guard` çalışır: ücretsiz modda ya da anahtar yokken çağrı deftere hiç iz bırakmadan durur. Sonra her deneme kendi
rezervasyonunu alır; başarılı çağrı işlenir (commit), başarısız olan iade edilir (release). Yalnızca geçici hatalar
(429, 5xx, zaman aşımı) yeniden denenir. fal yanıtı fatura tutarı döndürmediği için işlenen tutar tahmindir; video
sağlayıcısının sonucu (GenResult) kendi bedelini taşır (MiniMax H3: ölçülen GPU süresi).
"""
import logging

from app.budget import killswitch
from app.engine.providers import GenResult, TransientError

log = logging.getLogger("hellgrind.paid")

MAX_ATTEMPTS = 3


def available(key_name: str) -> bool:
    """Bu servisin anahtarı tanımlı mı (değeri okunmaz). fal.ai isteğe bağlıdır: anahtarı yoksa ücretli modda da storyboard
    karesi yerel çizilir ve dudak senkronu atlanır; video MiniMax H3 ile kendi sunucumuzda üretilir."""
    from app.live import key_state                  # yalnızca kilit modülü; ücretli paket yüklenmez
    return key_state(key_name)["set"]


def call(project_id: str, estimate: float, label: str, key_name: str, fn, *, scene_id=None, variation_id=None,
         asset_id=None, attempts: int = MAX_ATTEMPTS, details: dict | None = None):
    from app.live import guard                  # ücretli kilit: yalnızca fonksiyon içinde
    guard(key_name)
    for attempt in range(1, attempts + 1):
        try:
            with killswitch.spend(project_id, estimate, label, scene_id=scene_id, variation_id=variation_id,
                                  asset_id=asset_id, details={**(details or {}), "attempt": attempt}) as charge:
                result = fn()
                # Video sağlayıcısı kendi bedelini bildirir (MiniMax H3: sunucunun ölçtüğü GPU süresi); diğerleri tahmindir.
                charge.actual = result.cost_usd if isinstance(result, GenResult) else estimate
            return result
        except TransientError as e:
            log.warning("%s geçici hata (deneme %d/%d): %s", label, attempt, attempts, e)
    raise TransientError(f"{label}: {attempts} denemede de başarısız")
