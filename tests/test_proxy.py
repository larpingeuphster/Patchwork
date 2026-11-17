import json

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import taddons, tflow, tutils  # noqa: E402

from patchwork import rules  # noqa: E402
from patchwork.config import KIND_ASSET, KIND_FILE, Config, Swap  # noqa: E402
from patchwork.proxy import SwapAddon, ensure_ca, host_pattern  # noqa: E402

HOST = "assetdelivery.roblox.com"


def make_addon(tmp_path):
    sound = tmp_path / "hit.ogg"
    sound.write_bytes(b"OggS-fake")
    cfg = Config(swaps=[
        Swap("Sky", [100], KIND_ASSET, "900"),
        Swap("Hit", [200], KIND_FILE, str(sound)),
    ])
    hits = []
    return SwapAddon(cfg, hits.append), hits, sound


def flow(method, path, body=None, host=HOST):
    req = tutils.treq(method=method.encode(), host=host, port=443, scheme=b"https", path=path.encode(),
                      content=json.dumps(body).encode() if body is not None else b"")
    req.headers["host"] = host
    return tflow.tflow(req=req)


def test_batch_flow(tmp_path):
    addon, hits, sound = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [
            {"requestId": "a", "assetId": 100},
            {"requestId": "b", "assetId": 200},
        ])
        addon.request(f)
        assert json.loads(f.request.get_text())[0]["assetId"] == 900
        assert [h.asset_id for h in hits] == [100, 200]

        f.response = tutils.tresp(content=json.dumps([
            {"requestId": "a", "locations": [{"location": "https://c0.rbxcdn.com/x"}]},
            {"requestId": "b", "locations": [{"location": "https://c0.rbxcdn.com/y"}]},
        ]).encode())
        addon.response(f)
        out = json.loads(f.response.get_text())
        assert out[0]["locations"][0]["location"] == "https://c0.rbxcdn.com/x"
        file_url = out[1]["locations"][0]["location"]
        assert file_url == rules.file_url(HOST, str(sound))

        # Roblox then downloads the file URL, which Patchwork answers from disk.
        f2 = flow("GET", file_url.split(HOST, 1)[1])
        addon.request(f2)
        assert f2.response.status_code == 200
        assert f2.response.content == b"OggS-fake"
        assert f2.response.headers["content-type"] == "audio/ogg"


def test_gzip_request_body(tmp_path):
    addon, _, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}])
        f.request.encode("gzip")
        addon.request(f)
        assert f.request.headers["content-encoding"] == "gzip"
        assert json.loads(f.request.get_text())[0]["assetId"] == 900


def test_single_get_flows(tmp_path):
    addon, _, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("GET", "/v1/asset/?id=100")
        addon.request(f)
        assert f.request.path == "/v1/asset/?id=900"

        f = flow("GET", "/v1/asset/?id=200")
        addon.request(f)
        assert f.response.content == b"OggS-fake"


def test_other_hosts_untouched(tmp_path):
    addon, hits, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}], host="example.com")
        addon.request(f)
        assert json.loads(f.request.get_text())[0]["assetId"] == 100
        assert not hits


def test_missing_file_returns_404(tmp_path):
    addon, _, sound = make_addon(tmp_path)
    sound.unlink()
    with taddons.context(addon):
        f = flow("GET", rules.FILE_PREFIX + rules.file_token(str(sound)))
        addon.request(f)
        assert f.response.status_code == 404


def test_ensure_ca_is_stable(tmp_path):
    first = ensure_ca(tmp_path)
    assert "BEGIN CERTIFICATE" in first
    assert ensure_ca(tmp_path) == first


def test_host_pattern():
    import re
    pat = re.compile(host_pattern(HOST))
    assert pat.match("assetdelivery.roblox.com:443")
    assert pat.match("assetdelivery.roblox.com")
    assert not pat.match("evil-assetdelivery.roblox.com")
    assert not pat.match("assetdelivery.roblox.com.evil.com")
