"""Run the real proxy (regular mode, since local redirect mode needs Windows) end to end."""

import http.client
import http.server
import json
import socket
import threading

import pytest

pytest.importorskip("mitmproxy")

from patchwork.config import Config, Swap  # noqa: E402
from patchwork.proxy import ProxyRunner  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def upstream():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append(body)
            out = json.dumps([
                {"requestId": i["requestId"], "locations": [{"location": "https://c0.rbxcdn.com/x"}]}
                for i in body
            ]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()


def via_proxy(proxy_port, method, url, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, url, body=data, headers={"content-type": "application/json"})
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_start_swap_stop_restart(tmp_path, upstream):
    up_port, seen = upstream
    png = tmp_path / "sky.png"
    png.write_bytes(b"\x89PNG-fake")
    cfg = Config(
        swaps=[Swap("Sky", [1], "asset", "2"), Swap("Tex", [3], "file", str(png))],
        intercept_hosts=["127.0.0.1"],
    )
    hits = []
    proxy_port = free_port()
    runner = ProxyRunner(cfg, tmp_path, hits.append, lambda *_: None, mode=f"regular@{proxy_port}")

    for _ in range(2):  # a second start must work after stop
        runner.start(timeout=30)
        assert runner.running
        status, raw = via_proxy(proxy_port, "POST", f"http://127.0.0.1:{up_port}/v1/assets/batch",
                                [{"requestId": "0", "assetId": 1}, {"requestId": "1", "assetId": 3}])
        assert status == 200
        assert seen[-1][0]["assetId"] == 2
        location = json.loads(raw)[1]["locations"][0]["location"]
        path = location.split("127.0.0.1", 1)[1]
        status, data = via_proxy(proxy_port, "GET", f"http://127.0.0.1:{up_port}{path}")
        assert (status, data) == (200, b"\x89PNG-fake")
        runner.stop()
        assert not runner.running

    assert len(hits) == 4


def test_start_failure_is_reported(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        runner = ProxyRunner(Config(), tmp_path, lambda h: None, lambda *_: None, mode=f"regular@{port}")
        with pytest.raises(RuntimeError, match="listen"):
            runner.start(timeout=30)
        assert not runner.running
