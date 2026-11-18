from patchwork import rules
from patchwork.config import KIND_ASSET, KIND_FILE, Swap

HOST = "assetdelivery.roblox.com"


def table(tmp_path):
    return rules.SwapTable([
        Swap("Sky", [100, 101], KIND_ASSET, "900"),
        Swap("Hit", [200], KIND_FILE, str(tmp_path / "hit.ogg")),
        Swap("Off", [300], KIND_ASSET, "901", enabled=False),
        Swap("Dupe", [100], KIND_ASSET, "902"),
    ])


def test_table_lookup(tmp_path):
    t = table(tmp_path)
    assert t.lookup(100).value == "900"  # first swap wins
    assert t.lookup(300) is None  # disabled
    assert t.lookup(5) is None
    assert t.served_for_token(rules.file_token(str(tmp_path / "hit.ogg"))).name == "Hit"


def test_batch_round_trip(tmp_path):
    t = table(tmp_path)
    body = [
        {"requestId": "0", "assetId": 100, "assetType": "Image"},
        {"requestId": "1", "assetId": "101"},
        {"requestId": "2", "assetId": 200},
        {"requestId": "3", "assetId": 7},
        {"requestId": "4", "assetName": "no id"},
    ]
    changed, files, hits = rules.rewrite_batch_request(body, t)
    assert changed
    assert body[0]["assetId"] == 900 and body[1]["assetId"] == "900"  # type preserved
    assert body[2]["assetId"] == 200  # file swaps keep the request
    assert body[3]["assetId"] == 7
    assert files == {"2": str(tmp_path / "hit.ogg")}
    assert [h.asset_id for h in hits] == [100, 101, 200]

    response = [
        {"requestId": "0", "locations": [{"location": "https://c1.rbxcdn.com/a"}]},
        {"requestId": "2", "errors": [{"code": 404}]},
        {"requestId": "3", "locations": [{"location": "https://c1.rbxcdn.com/b"}]},
    ]
    assert rules.rewrite_batch_response(response, files, HOST)
    assert response[1]["locations"][0]["location"] == rules.file_url(HOST, str(tmp_path / "hit.ogg"))
    assert "errors" not in response[1]
    assert response[2]["locations"][0]["location"].endswith("/b")


def test_batch_without_request_ids_uses_position(tmp_path):
    t = table(tmp_path)
    body = [{"assetId": 1}, {"assetId": 200}]
    _, files, _ = rules.rewrite_batch_request(body, t)
    response = [{"locations": []}, {"locations": []}]
    rules.rewrite_batch_response(response, files, HOST)
    assert response[0]["locations"] == []
    assert response[1]["locations"][0]["location"].startswith(f"https://{HOST}/patchwork/file/")


def test_batch_ignores_garbage(tmp_path):
    t = table(tmp_path)
    assert rules.rewrite_batch_request({"not": "a list"}, t) == (False, {}, [])
    assert rules.rewrite_batch_request([1, None, {"assetId": True}], t) == (False, {}, [])


def test_is_batch_path():
    assert rules.is_batch_path("/v1/assets/batch")
    assert rules.is_batch_path("/v1/assets/batch/?x=1")
    assert not rules.is_batch_path("/v1/asset")


def test_single_query_asset_swap(tmp_path):
    d = rules.decide_single("/v1/asset/?id=100&serverplaceid=5", table(tmp_path))
    assert d.new_path == "/v1/asset/?id=900&serverplaceid=5"
    assert d.hit.asset_id == 100


def test_single_query_file_swap(tmp_path):
    t = table(tmp_path)
    assert rules.decide_single("/asset?ID=200", t).serve_file
    assert rules.decide_single("/v2/asset?id=200", t).rewrite_json


def test_single_path_style(tmp_path):
    t = table(tmp_path)
    assert rules.decide_single("/v1/assetId/100", t).new_path == "/v1/assetId/900"
    assert rules.decide_single("/v2/assetId/101/version/3?x=1", t).new_path == "/v2/assetId/900/version/3?x=1"
    assert rules.decide_single("/v2/assetId/200", t).rewrite_json
    assert rules.decide_single("/v2/assetId/5", t) is None
    assert rules.decide_single("/v1/asset?id=abc", t) is None
    assert rules.decide_single("/v1/other?id=100", t) is None


def test_json_single_rewrite():
    body = {"locations": [{"location": "x"}], "errors": [1]}
    assert rules.rewrite_json_response(body, HOST, "C:/a.png")
    assert body["locations"][0]["location"] == rules.file_url(HOST, "C:/a.png")
    assert "errors" not in body
    assert not rules.rewrite_json_response([1], HOST, "x")


def test_content_types(tmp_path):
    from pathlib import Path
    assert rules.content_type_for(Path("a.PNG")) == "image/png"
    assert rules.content_type_for(Path("a.ogg")) == "audio/ogg"
    assert rules.content_type_for(Path("a.mesh")) == "application/octet-stream"


def test_remove_swaps(tmp_path):
    from patchwork.config import KIND_REMOVE
    t = rules.SwapTable([Swap("gone", [5, 6], KIND_REMOVE, "")])
    body = [{"requestId": "a", "assetId": 5}, {"requestId": "b", "assetId": 7}]
    changed, files, hits = rules.rewrite_batch_request(body, t)
    assert not changed and files == {"a": None} and hits[0].replacement == "removed"
    resp = [{"requestId": "a", "locations": [{"location": "https://c0.rbxcdn.com/x"}]},
            {"requestId": "b", "locations": [{"location": "https://c0.rbxcdn.com/y"}]}]
    assert rules.rewrite_batch_response(resp, files, HOST)
    assert "locations" not in resp[0] and resp[0]["errors"][0]["code"] == 404
    assert resp[1]["locations"][0]["location"].endswith("/y")
    assert rules.decide_single("/v1/asset/?id=6", t).remove
    assert rules.decide_single("/v2/assetId/5", t).remove
    assert t.served_for_token(rules.file_token("")) is None


def test_is_single_path():
    assert rules.is_single_path("/v1/asset/?id=1")
    assert rules.is_single_path("/v2/assetId/123/version/2")
    assert not rules.is_single_path("/v1/something-else")
