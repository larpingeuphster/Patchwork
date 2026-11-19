import asyncio
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
        asyncio.run(addon.request(f))
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
        asyncio.run(addon.request(f2))
        assert f2.response.status_code == 200
        assert f2.response.content == b"OggS-fake"
        assert f2.response.headers["content-type"] == "audio/ogg"


def test_gzip_request_body(tmp_path):
    addon, _, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}])
        f.request.encode("gzip")
        asyncio.run(addon.request(f))
        assert f.request.headers["content-encoding"] == "gzip"
        assert json.loads(f.request.get_text())[0]["assetId"] == 900


def test_single_get_flows(tmp_path):
    addon, _, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("GET", "/v1/asset/?id=100")
        asyncio.run(addon.request(f))
        assert f.request.path == "/v1/asset/?id=900"

        f = flow("GET", "/v1/asset/?id=200")
        asyncio.run(addon.request(f))
        assert f.response.content == b"OggS-fake"


def test_other_hosts_untouched(tmp_path):
    addon, hits, _ = make_addon(tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}], host="example.com")
        asyncio.run(addon.request(f))
        assert json.loads(f.request.get_text())[0]["assetId"] == 100
        assert not hits


def test_missing_file_returns_404(tmp_path):
    addon, _, sound = make_addon(tmp_path)
    sound.unlink()
    with taddons.context(addon):
        f = flow("GET", rules.FILE_PREFIX + rules.file_token(str(sound)))
        asyncio.run(addon.request(f))
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


def test_url_swap_downloads_once_and_serves(tmp_path, monkeypatch):
    from patchwork import downloads
    from patchwork.config import KIND_URL

    calls = []

    def fake_fetch(url, dest):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"RIFF-webp")
        return dest

    monkeypatch.setattr(downloads, "fetch", fake_fetch)
    url = "https://files.catbox.moe/n3nhmi.webp"
    cfg = Config(swaps=[Swap("NG", [13854780042], KIND_URL, url)])
    addon = SwapAddon(cfg, downloads_dir=tmp_path / "dl")
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 13854780042}])
        asyncio.run(addon.request(f))
        f.response = tutils.tresp(content=b'[{"requestId": "a", "locations": []}]')
        addon.response(f)
        location = json.loads(f.response.get_text())[0]["locations"][0]["location"]
        for _ in range(2):
            f2 = flow("GET", location.split(HOST, 1)[1])
            asyncio.run(addon.request(f2))
            assert f2.response.content == b"RIFF-webp"
            assert f2.response.headers["content-type"] == "image/webp"
    assert calls == [url]


def test_broken_link_gives_404(tmp_path, monkeypatch):
    from patchwork import downloads
    from patchwork.config import KIND_URL

    def boom(url, dest):
        raise downloads.DownloadError("files.catbox.moe: 404")

    monkeypatch.setattr(downloads, "fetch", boom)
    url = "https://files.catbox.moe/gone.png"
    addon = SwapAddon(Config(swaps=[Swap("x", [1], KIND_URL, url)]), downloads_dir=tmp_path)
    with taddons.context(addon):
        f = flow("GET", rules.FILE_PREFIX + rules.file_token(url))
        asyncio.run(addon.request(f))
        assert f.response.status_code == 404


def test_allow_gate_pauses_swaps(tmp_path):
    seen = []

    def allow(place):
        seen.append(place)
        return place == 17625359962

    cfg = Config(swaps=[Swap("Sky", [100], KIND_ASSET, "900")])
    addon = SwapAddon(cfg, allow=allow, downloads_dir=tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}])
        asyncio.run(addon.request(f))
        assert json.loads(f.request.get_text())[0]["assetId"] == 100  # paused: no header, not in RIVALS
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 100}])
        f.request.headers["Roblox-Place-Id"] = "17625359962"
        asyncio.run(addon.request(f))
        assert json.loads(f.request.get_text())[0]["assetId"] == 900
    assert seen == [None, 17625359962]


def test_remove_and_stats(tmp_path):
    from patchwork.config import KIND_REMOVE
    addon = SwapAddon(Config(swaps=[Swap("gone", [5], KIND_REMOVE, "")]), downloads_dir=tmp_path)
    with taddons.context(addon):
        f = flow("POST", "/v1/assets/batch", [{"requestId": "a", "assetId": 5}, {"requestId": "b", "assetId": 6}])
        asyncio.run(addon.request(f))
        f.response = tutils.tresp(content=b'[{"requestId":"a","locations":[{"location":"x"}]},{"requestId":"b","locations":[]}]')
        addon.response(f)
        out = json.loads(f.response.get_text())
        assert out[0]["errors"][0]["code"] == 404 and "locations" not in out[0]
        g = flow("GET", "/v1/asset/?id=5")
        asyncio.run(addon.request(g))
        assert g.response.status_code == 404
        u = flow("GET", "/v9/weird/123456")
        asyncio.run(addon.request(u))
        assert u.response is None
    st = addon.stats
    assert (st.lookups, st.assets, st.swapped) == (3, 3, 2)
    assert st.unknown_paths == {"assetdelivery.roblox.com/v9/weird/<id>"}


def test_tls_hooks_count_trust_and_refusals(tmp_path):
    from types import SimpleNamespace
    addon = SwapAddon(Config(), downloads_dir=tmp_path)
    ok = SimpleNamespace(conn=SimpleNamespace(sni="assetdelivery.roblox.com"))
    other = SimpleNamespace(conn=SimpleNamespace(sni="example.com"))
    addon.tls_established_client(ok)
    addon.tls_established_client(other)
    addon.tls_failed_client(ok)
    addon.tls_failed_client(ok)
    assert addon.stats.trusted == 1 and addon.stats.rejected == 2


def test_cdn_downloads_pass_through_and_are_counted(tmp_path):
    cfg = Config(swaps=[Swap("Sky", [100], KIND_ASSET, "900")],
                 intercept_hosts=["assetdelivery.roblox.com", "fts.rbxcdn.com"])
    addon = SwapAddon(cfg, downloads_dir=tmp_path)
    with taddons.context(addon):
        f = flow("GET", "/abc123def/texture", host="fts.rbxcdn.com")
        asyncio.run(addon.request(f))
        assert f.response is None
    assert addon.stats.downloads == 1 and addon.stats.lookups == 1


def test_counts_connections_closed_without_a_request(tmp_path):
    from types import SimpleNamespace
    addon = SwapAddon(Config(), downloads_dir=tmp_path)
    silent = SimpleNamespace(id="c1", alpn=b"h2")
    busy = SimpleNamespace(id="c2", alpn=None)
    with taddons.context(addon):
        for client in (silent, busy):
            addon.tls_established_client(SimpleNamespace(conn=SimpleNamespace(sni="assetdelivery.roblox.com"),
                                                         context=SimpleNamespace(client=client)))
        f = flow("GET", "/v1/asset/?id=1")
        f.client_conn.id = "c2"
        addon.requestheaders(f)
        addon.client_disconnected(silent)
        addon.client_disconnected(busy)
        addon.client_disconnected(SimpleNamespace(id="unknown"))
    assert addon.stats.silent_closes == 1
    assert addon.stats.protocols == {"h2", "http/1.1"}
