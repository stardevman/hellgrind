"""Ücretli çağrıların ortak yolu (Faz 18): kilit, rezervasyon, yeniden deneme.

Önce `guard` çalışır: ücretsiz modda ya da anahtar yokken çağrı deftere hiç iz bırakmadan durur. Sonra her deneme kendi
rezervasyonunu alır; başarılı çağrı işlenir (commit), başarısız olan iade edilir (release). Yalnızca geçici hatalar
(429, 5xx, zaman aşımı) yeniden denenir. fal yanıtı fatura tutarı döndürmediği için işlenen tutar tahmindir.
"""
import logging

from app.budget import killswitch
from app.engine.providers import TransientError

log = logging.getLogger("hellgrind.paid")

MAX_ATTEMPTS = 3


def call(project_id: str, estimate: float, label: str, key_name: str, fn, *, scene_id=None, variation_id=None,
         attempts: int = MAX_ATTEMPTS):
    from app.live import guard                  # ücretli kilit: yalnızca fonksiyon içinde
    guard(key_name)
    for attempt in range(1, attempts + 1):
        try:
            with killswitch.spend(project_id, estimate, label, scene_id=scene_id, variation_id=variation_id) as charge:
                result = fn()
                charge.actual = estimate
            return result
        except TransientError as e:
            log.warning("%s geçici hata (deneme %d/%d): %s", label, attempt, attempts, e)
    raise TransientError(f"{label}: {attempts} denemede de başarısız")
