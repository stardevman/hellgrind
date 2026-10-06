"""H3 sunucusunun sertifikası: ilk başarılı bağlantıda SHA-256 parmak izi sabitlenir, sonra her bağlantıda istek
gönderilmeden önce aranır; farklı sertifika üretimi durdurur, sertifika okunamazsa doğrulamasız sürülür ve uyarı yazılır.
Gerçek httpx/httpcore istemcisi sahte bir soket katmanı üzerinde çalışır; ağa çıkılmaz.
"""
import hashlib

import httpcore
import pytest
from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.live import PaidCallBlocked, tls_pin, vast_h3

URL = "https://198.51.100.7:41234"
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"


class FakeSSL:
    def __init__(self, der):
        self.der = der

    def getpeercert(self, binary_form=False):
        return self.der if binary_form else {}

    def selected_alpn_protocol(self):
        return "http/1.1"


class FakeStream(httpcore.NetworkStream):
    def __init__(self, net, tls=False):
        self.net, self.tls, self.sent, self.unread = net, tls, b"", RESPONSE

    def read(self, max_bytes, timeout=None):
        chunk, self.unread = self.unread[:max_bytes], self.unread[max_bytes:]
        return chunk

    def write(self, buffer, timeout=None):
        self.sent += buffer
        self.net.sent.append(buffer)

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.net.handshakes += 1
        return FakeStream(self.net, tls=True)

    def get_extra_info(self, info):
        if info == "ssl_object" and self.tls:
            return FakeSSL(self.net.der) if self.net.der is not None else None
        return None


class FakeNet(httpcore.NetworkBackend):
    """Sunucunun sunduğu sertifika `der`; None: el sıkışmada sertifika okunamıyor."""

    def __init__(self, der: bytes | None = b"portal-cert-A"):
        self.der, self.sent, self.handshakes = der, [], 0

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return FakeStream(self)

    def sleep(self, seconds):
        pass


@pytest.fixture
def net(pg, monkeypatch):
    from app import runtime_config as rc
    monkeypatch.setattr(rc, "VERSION_KEY", "hg:test:config:version")    # geliştirme yığınının kaydına dokunulmaz
    monkeypatch.setattr(rc, "AUDIT_KEY", "hg:test:config:audit")
    fake = FakeNet()
    monkeypatch.setattr(tls_pin, "BACKEND", fake)
    monkeypatch.setattr(vast_h3, "TRANSPORT", None)
    monkeypatch.setattr(settings, "h3_tls_mode", "pin")
    monkeypatch.setattr(settings, "h3_backend", "comfyui")
    monkeypatch.setattr(settings, "h3_endpoint_url", URL)
    with session_scope() as s:
        s.execute(text("DELETE FROM h3_tls_pins"))
    from app.live import vast_box
    for name in ("tls_fallback", "tls_mismatch", "last_project"):
        vast_box._r().delete(vast_box._k(name))
    yield fake
    with session_scope() as s:
        s.execute(text("DELETE FROM h3_tls_pins"))
    rc._redis().delete(rc.VERSION_KEY, rc.AUDIT_KEY)


def test_fingerprints_use_the_openssl_format():
    fp = tls_pin.fingerprint(b"x")
    assert fp == ":".join(hashlib.sha256(b"x").hexdigest().upper()[i:i + 2] for i in range(0, 64, 2))
    assert tls_pin.normalize(fp) == hashlib.sha256(b"x").hexdigest() and tls_pin.normalize(fp.lower()) == tls_pin.normalize(fp)
    assert tls_pin.endpoint_of("https://1.2.3.4:41234/x") == "1.2.3.4:41234" and tls_pin.endpoint_of("https://a.b") == "a.b:443"


def test_the_first_successful_connection_pins_the_certificate(net):
    assert vast_h3.healthy(URL, "tok")
    pin = tls_pin.stored("198.51.100.7:41234")
    assert pin["sha256"] == tls_pin.fingerprint(b"portal-cert-A")
    state = tls_pin.state()
    assert state["pin"]["sha256"] == pin["sha256"] and state["fallback"] is None and state["mismatch"] is None


def test_an_unauthenticated_answer_pins_nothing(net, monkeypatch):
    monkeypatch.setattr("tests.test_tls_pin.RESPONSE", b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
    assert not vast_h3.healthy(URL, "wrong")
    assert tls_pin.stored("198.51.100.7:41234") is None


def test_a_different_certificate_stops_before_the_request_and_token_are_sent(net):
    vast_h3.healthy(URL, "tok")
    net.der, net.sent = b"someone-else", []
    with pytest.raises(tls_pin.PinMismatch, match="sabitlenen parmak izinden farklı"):
        with vast_h3._client(URL, "secret-token") as c:
            c.get("/system_stats")
    assert net.sent == []                                         # istek de belirteç de gönderilmedi
    assert tls_pin.flags()["mismatch"]["seen"] == tls_pin.fingerprint(b"someone-else")
    assert isinstance(vast_h3._classify(tls_pin.PinMismatch("e", "a", "b")), PaidCallBlocked)


def test_production_requests_are_pinned_on_every_connection(net):
    with vast_h3._client(URL, "tok") as c:                       # sabitleme yokken önce sağlık sorgusu sabitler
        assert c.get("/queue").status_code == 200
    assert tls_pin.stored("198.51.100.7:41234") is not None
    assert any(b"GET /system_stats" in s for s in net.sent) and any(b"GET /queue" in s for s in net.sent)
    net.der = b"other"
    with pytest.raises(tls_pin.PinMismatch):
        with vast_h3._client(URL, "tok") as c:
            c.get("/queue")


def test_when_pinning_fails_the_connection_falls_back_to_no_verification_with_a_warning(net):
    net.der = None
    assert vast_h3.healthy(URL, "tok")
    assert tls_pin.stored("198.51.100.7:41234") is None
    assert "sertifika okunamadı" in tls_pin.flags()["fallback"]["reason"]


def test_resetting_the_pin_lets_the_next_connection_pin_again(net, api):
    vast_h3.healthy(URL, "tok")
    net.der = b"rebuilt-box"
    with pytest.raises(tls_pin.PinMismatch):
        vast_h3.healthy(URL, "tok")
    state = api.delete("/api/settings/vast/tls-pin").json()
    assert state["pin"] is None and state["mismatch"] is None
    assert vast_h3.healthy(URL, "tok") and tls_pin.stored("198.51.100.7:41234")["sha256"] == tls_pin.fingerprint(b"rebuilt-box")


def test_verify_and_off_modes_do_not_pin(net, monkeypatch):
    monkeypatch.setattr(settings, "h3_tls_mode", "verify")
    assert tls_pin.observer_for(URL) is None and tls_pin.client_options(URL, None) == {"verify": True}
    monkeypatch.setattr(settings, "h3_tls_mode", "off")
    assert tls_pin.observer_for(URL) is None and tls_pin.client_options(URL, None) == {"verify": False}
    monkeypatch.setattr(settings, "h3_tls_mode", "pin")
    assert tls_pin.observer_for("http://127.0.0.1:8188") is None          # SSH tüneli: TLS yok


def test_a_self_signed_portal_in_verify_mode_explains_how_to_pin():
    import httpx
    exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")
    assert "pin" in vast_h3.account_problem(exc)
