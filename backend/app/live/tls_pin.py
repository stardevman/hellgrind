"""H3 sunucusunun (vast.ai Instance Portal) TLS sertifikası: doğrula, sabitle ya da kapat (Ayarlar › MiniMax H3 › TLS).

- **verify:** olağan doğrulama (gerçek sertifikalı adres, ör. trycloudflare).
- **pin** (yedek ve yeni makinenin varsayılanı): portalın sertifikası kendinden imzalıdır. İlk başarılı bağlantıda (kimlik
  doğrulamalı sağlık yanıtı 200) sertifikanın SHA-256 parmak izi kaydedilir (`h3_tls_pins`, adres başına) ve sonraki her
  bağlantıda el sıkışmadan hemen sonra, istek (ve belirteç) gönderilmeden önce aranır. Farklı sertifika üretimi durdurur
  (olası araya girme); yönetmen makineyi bilerek yenilediyse Ayarlar'dan sabitlemeyi sıfırlar. setup.sh parmak izini
  kurulum günlüğüne yazar: panelde görünenle karşılaştırılabilir.
- **off:** doğrulama kapalı (trafik yine şifreli).

Sabitleme yapılamazsa (el sıkışmadan sertifika okunamadı, kayıt yazılamadı) bağlantı doğrulamasız sürer ve panelde uyarı
görünür. Ağ kodu: yalnızca ücretli akışın ve bağlantı sınamasının içinden çağrılır.
"""
import hashlib
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpcore
import httpx

from app.config import settings

log = logging.getLogger("hellgrind.live.tls")

MODES = ("verify", "pin", "off")
BACKEND: httpcore.NetworkBackend | None = None    # testler sahte soket bağlar


class PinMismatch(RuntimeError):
    """Sunucu sabitlenen sertifikadan farklı bir sertifika sundu: istek gönderilmedi."""

    def __init__(self, endpoint: str, expected: str, seen: str):
        self.endpoint, self.expected, self.seen = endpoint, expected, seen
        super().__init__(f"H3 sunucusunun sertifikası sabitlenen parmak izinden farklı ({endpoint}; beklenen "
                         f"{short(expected)}, gelen {short(seen)}): araya girme olabilir, istek gönderilmedi. Makineyi "
                         "bilerek yenilediyseniz Ayarlar › MiniMax H3 › Makine'den sabitlemeyi sıfırlayın.")


def fingerprint(der: bytes) -> str:
    """openssl biçimi: AB:CD:..."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize(fp: str) -> str:
    return "".join(c for c in (fp or "").lower() if c in "0123456789abcdef")


def short(fp: str) -> str:
    return fp[:23] + "…" if len(fp) > 23 else fp


def endpoint_of(url: str) -> str:
    u = urlparse(url)
    return f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"


# ---------- kayıt ----------

def stored(endpoint: str) -> dict | None:
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:
        row = s.execute(text("SELECT sha256, pinned_at FROM h3_tls_pins WHERE endpoint = :e"), {"e": endpoint}).first()
    return {"sha256": row[0], "pinned_at": row[1].isoformat()} if row else None


def save(endpoint: str, fp: str) -> None:
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:
        s.execute(text("""INSERT INTO h3_tls_pins (endpoint, sha256) VALUES (:e, :f)
                          ON CONFLICT (endpoint) DO UPDATE SET sha256 = EXCLUDED.sha256, pinned_at = now()"""),
                  {"e": endpoint, "f": fp})
    _flag("fallback", None)
    _flag("mismatch", None)
    log.info("H3 sertifikası sabitlendi: %s %s", endpoint, fp)
    _event("h3_tls_pinned", endpoint=endpoint, fingerprint=fp)


def forget(endpoint: str | None = None) -> int:
    """Sabitlemeyi siler (adres verilmezse hepsi). Bir sonraki başarılı bağlantı yeniden sabitler."""
    from sqlalchemy import text
    from app.db import session_scope
    with session_scope() as s:
        if endpoint:
            n = s.execute(text("DELETE FROM h3_tls_pins WHERE endpoint = :e"), {"e": endpoint}).rowcount
        else:
            n = s.execute(text("DELETE FROM h3_tls_pins")).rowcount
    _flag("mismatch", None)
    return n


def _k(name: str) -> str:
    from app.live import vast_box
    return vast_box._k(f"tls_{name}")


def _flag(name: str, value: dict | None) -> None:
    try:
        from app.budget import killswitch
        if value is None:
            killswitch.r().delete(_k(name))
        else:
            killswitch.r().set(_k(name), json.dumps({**value, "at": time.time()}, ensure_ascii=False), ex=30 * 24 * 3600)
    except Exception:                                         # noqa: BLE001 — uyarı yazılamasa da bağlantı sürer
        log.warning("TLS uyarısı yazılamadı: %s", name)


def flags() -> dict:
    """Panel için: son sabitleme sorunu (fallback) ve son sertifika uyuşmazlığı (mismatch)."""
    from app.budget import killswitch
    out = {}
    for name in ("fallback", "mismatch"):
        raw = killswitch.r().get(_k(name))
        out[name] = json.loads(raw) if raw else None
    return out


def _event(kind: str, **data) -> None:
    from app.live import vast_box
    vast_box._event(kind, **data)


def state() -> dict:
    """Panel: kip, H3 adresinin sabitlenmiş parmak izi ve uyarılar (yalnızca kayıt okunur; ağ yok)."""
    url = settings.h3_endpoint_url
    endpoint = endpoint_of(url) if url.startswith("https://") else None
    pin = None
    try:
        pin = stored(endpoint) if endpoint else None
    except Exception:                                         # noqa: BLE001 — şema yoksa (eski veritabanı) boş
        pass
    try:
        warnings = flags()
    except Exception:                                         # noqa: BLE001
        warnings = {"fallback": None, "mismatch": None}
    return {"mode": settings.h3_tls_mode, "endpoint": endpoint, "pin": pin, **warnings}


# ---------- bağlantı ----------

@dataclass
class Observer:
    """El sıkışmada görülen sertifikayı kaydeder; beklenen parmak izi varsa uymayanı keser."""
    endpoint: str
    expected: str | None = None
    seen: str | None = None
    unavailable: str | None = None
    _confirmed: bool = field(default=False, repr=False)
    _warned: bool = field(default=False, repr=False)

    def check(self, stream) -> None:
        ssl_object = stream.get_extra_info("ssl_object")
        der = None
        try:
            der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        except Exception as e:                                # noqa: BLE001
            self.unavailable = f"sertifika okunamadı ({type(e).__name__})"
        if not der:                                           # sabitleme yapılamıyor: doğrulamasız sürer, uyarı
            self.unavailable = self.unavailable or "el sıkışmada sertifika okunamadı"
            if not self._warned:
                self._warned = True
                fallback(self.endpoint, self.unavailable)
            return
        self.seen = fingerprint(der)
        if self.expected and normalize(self.seen) != normalize(self.expected):
            _flag("mismatch", {"endpoint": self.endpoint, "expected": self.expected, "seen": self.seen})
            _event("h3_tls_mismatch", endpoint=self.endpoint, expected=self.expected, seen=self.seen)
            raise PinMismatch(self.endpoint, self.expected, self.seen)

    def confirm(self) -> None:
        """Kimlik doğrulamalı başarılı yanıttan sonra: ilk kez görülen parmak izi sabitlenir; okunamadıysa uyarı yazılır."""
        if self._confirmed:
            return
        self._confirmed = True
        if self.seen and not self.expected and not self.unavailable:
            try:
                save(self.endpoint, self.seen)
            except Exception as e:                            # noqa: BLE001
                fallback(self.endpoint, f"parmak izi kaydedilemedi ({type(e).__name__})")


def fallback(endpoint: str, reason: str) -> None:
    log.warning("H3 sertifika sabitleme yapılamadı (%s): %s; doğrulamasız sürülüyor", endpoint, reason)
    _flag("fallback", {"endpoint": endpoint, "reason": reason})
    _event("h3_tls_fallback", endpoint=endpoint, reason=reason)


class _Stream(httpcore.NetworkStream):
    def __init__(self, inner, observer: Observer):
        self._inner, self._observer = inner, observer

    def read(self, max_bytes, timeout=None):
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        self._inner.write(buffer, timeout)

    def close(self):
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        tls = self._inner.start_tls(ssl_context, server_hostname, timeout)
        try:
            self._observer.check(tls)                         # istek baytlarından önce
        except BaseException:
            tls.close()
            raise
        return _Stream(tls, self._observer)

    def get_extra_info(self, info):
        return self._inner.get_extra_info(info)


class _Backend(httpcore.NetworkBackend):
    def __init__(self, observer: Observer, inner: httpcore.NetworkBackend):
        self._observer, self._inner = observer, inner

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _Stream(self._inner.connect_tcp(host, port, timeout, local_address, socket_options), self._observer)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):   # pragma: no cover — kullanılmaz
        return _Stream(self._inner.connect_unix_socket(path, timeout, socket_options), self._observer)

    def sleep(self, seconds):
        self._inner.sleep(seconds)


def _unverified_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                            # zincir denetlenmez: güven parmak izindedir
    return ctx


class PinnedTransport(httpx.HTTPTransport):
    def __init__(self, observer: Observer):
        super().__init__(verify=False)
        self._pool = httpcore.ConnectionPool(ssl_context=_unverified_context(),
                                             network_backend=_Backend(observer, BACKEND or httpcore.SyncBackend()))


def observer_for(url: str) -> Observer | None:
    """pin kipinde ve https adreste gözlemci (sabitlenmiş parmak izi varsa onu bekler); diğer durumlarda None."""
    if settings.h3_tls_mode != "pin" or not url.startswith("https://"):
        return None
    endpoint = endpoint_of(url)
    try:
        pin = stored(endpoint)
    except Exception as e:                                    # noqa: BLE001 — kayıt okunamazsa sabitleme yapılamaz
        fallback(endpoint, f"sabitleme kaydı okunamadı ({type(e).__name__})")
        return None
    return Observer(endpoint, expected=pin["sha256"] if pin else None)


def client_options(url: str, observer: Observer | None) -> dict:
    """httpx.Client için transport/verify seçenekleri."""
    if observer is not None:
        return {"transport": PinnedTransport(observer)}
    if settings.h3_tls_mode == "verify" or not url.startswith("https://"):
        return {"verify": True}
    return {"verify": False}                                  # off ya da sabitleme yapılamadı
