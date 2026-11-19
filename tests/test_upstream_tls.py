"""Patchwork must verify Roblox's real server, and trust what Windows trusts.

Simulates antivirus "HTTPS scanning": the asset server's certificate is signed
by a CA that isn't in certifi. Without that CA Patchwork can't connect; once
it's in the trust list (as Windows' trusted CAs are), requests go through and
get swapped.
"""

import datetime
import http.client
import json
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("mitmproxy")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from patchwork.config import Config, Swap  # noqa: E402
from patchwork.proxy import ProxyRunner, ca_dir, upstream_trust  # noqa: E402


def _cert(subject, issuer_name, issuer_key, key, ca, san=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if san:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def fake_av_server(tmp_path):
    """An HTTPS asset server whose certificate chains to an 'antivirus' CA."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Fake Antivirus Web Shield Root", "Fake Antivirus Web Shield Root", ca_key, ca_key, True)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert("localhost", "Fake Antivirus Web Shield Root", ca_key, key, False, san="localhost")
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            out = b'[{"requestId":"0","locations":[{"location":"https://c0.rbxcdn.com/x"}]}]'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "key.pem")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], ca.public_bytes(serialization.Encoding.PEM).decode(), seen
    httpd.shutdown()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _batch_through_proxy(proxy_port, up_port, home):
    ctx = ssl.create_default_context(cafile=str(ca_dir(home) / "mitmproxy-ca-cert.pem"))
    conn = http.client.HTTPSConnection("127.0.0.1", proxy_port, timeout=10, context=ctx)
    conn.set_tunnel("localhost", up_port)
    conn.request("POST", "/v1/assets/batch", body=json.dumps([{"requestId": "0", "assetId": 1}]),
                 headers={"content-type": "application/json"})
    return conn.getresponse().status


@pytest.mark.parametrize("trust_av_ca", [False, True])
def test_upstream_verification(tmp_path, fake_av_server, trust_av_ca):
    up_port, av_ca_pem, seen = fake_av_server
    home = tmp_path / "home"
    errors = []
    proxy_port = _free_port()
    runner = ProxyRunner(
        Config(swaps=[Swap("s", [1], "asset", "2")], intercept_hosts=["localhost"]),
        home, lambda h: None, lambda level, msg: errors.append((level, msg)),
        mode=f"regular@{proxy_port}", extra_upstream_pems=(av_ca_pem,) if trust_av_ca else (),
    )
    runner.start(timeout=30)
    try:
        if trust_av_ca:
            assert _batch_through_proxy(proxy_port, up_port, home) == 200
            assert seen and seen[-1][0]["assetId"] == 2  # swapped on the way through
        else:
            with pytest.raises((ssl.SSLError, OSError, http.client.HTTPException)):
                status = _batch_through_proxy(proxy_port, up_port, home)
                if status >= 500:
                    raise OSError(status)
            assert not seen
            assert any("couldn't make a trusted connection" in m for _, m in errors), errors
    finally:
        runner.stop()


def test_upstream_trust_includes_windows_store(tmp_path):
    import certifi

    path = upstream_trust(tmp_path, ("-----BEGIN CERTIFICATE-----\nEXTRA\n-----END CERTIFICATE-----\n",))
    text = path.read_text()
    assert text.startswith(open(certifi.where()).read()[:200])
    assert "EXTRA" in text
    certifi_count = open(certifi.where()).read().count("BEGIN CERTIFICATE")
    if sys.platform == "win32":
        # Windows always has trusted roots, so the list must be longer than certifi's.
        assert text.count("BEGIN CERTIFICATE") > certifi_count + 1
