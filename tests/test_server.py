import http.client
import json

import pytest

from patchwork.controller import Controller
from patchwork.server import UiServer


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    ctl = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs")
    server = UiServer(ctl)
    server.start()
    yield server
    server.stop()
    ctl.shutdown()


def request(server, method, path, body=None, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    headers = {"Host": host or f"127.0.0.1:{server.port}", "Content-Type": "application/json"}
    if token:
        headers["X-Patchwork-Token"] = server.token if token is True else token
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read(), resp


def test_serves_page_with_csp(srv):
    status, body, resp = request(srv, "GET", "/", token=False)
    assert status == 200 and b"Patchwork" in body
    assert "script-src 'self'" in resp.getheader("Content-Security-Policy")
    assert request(srv, "GET", "/app.js", token=False)[0] == 200
    assert request(srv, "GET", "/../server.py", token=False)[0] == 404
    assert request(srv, "GET", "/%2e%2e/server.py", token=False)[0] == 404


def test_api_requires_token_and_host(srv):
    assert request(srv, "POST", "/api/snapshot", {"args": {}}, token=False)[0] == 403
    assert request(srv, "POST", "/api/snapshot", {"args": {}}, token="wrong")[0] == 403
    assert request(srv, "POST", "/api/snapshot", {"args": {}}, host="evil.example:80")[0] == 403
    assert request(srv, "GET", "/", token=False, host="evil.example")[0] == 403


def test_api_calls(srv):
    status, body, _ = request(srv, "POST", "/api/snapshot", {"args": {}})
    data = json.loads(body)
    assert status == 200 and data["ok"] and data["result"]["editing"] == "Default"

    status, body, _ = request(srv, "POST", "/api/save_swap", {"args": {"name": "a", "ids": "1", "replace": "https://x.example/a.png"}})
    assert json.loads(body) == {"ok": True, "result": {"index": 0}}

    _, body, _ = request(srv, "POST", "/api/save_swap", {"args": {"name": "", "ids": "", "replace": "5"}})
    assert json.loads(body)["ok"] is False and "asset ID" in json.loads(body)["error"]

    assert request(srv, "POST", "/api/shutdown", {"args": {}})[0] == 404  # not exposed
    assert request(srv, "POST", "/api/snapshot", {"args": {"bogus": 1}})[0] == 400

    _, body, _ = request(srv, "GET", "/api/events?after=0")
    events = json.loads(body)["events"]
    assert any(e["kind"] == "state" for e in events)
