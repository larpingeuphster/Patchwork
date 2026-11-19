import pytest

from patchwork.config import KIND_ASSET, KIND_FILE, Config, Swap, parse_asset_ids


def test_round_trip(tmp_path):
    cfg = Config(swaps=[
        Swap("Sky", [1, 2], KIND_ASSET, "99"),
        Swap("Hit sound", [3], KIND_FILE, str(tmp_path / "hit.ogg"), enabled=False),
    ])
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded == cfg


def test_missing_file_gives_defaults(tmp_path):
    cfg = Config.load(tmp_path / "nope.json")
    assert cfg.swaps == []
    assert cfg.intercept_hosts == ["assetdelivery.roblox.com", "fts.rbxcdn.com", "contentdelivery.roblox.com"]
    assert cfg.processes == ["RobloxPlayerBeta.exe"]


@pytest.mark.parametrize("kwargs", [
    dict(name="x", targets=[], kind=KIND_ASSET, value="1"),
    dict(name="x", targets=[0], kind=KIND_ASSET, value="1"),
    dict(name="x", targets=[1], kind=KIND_ASSET, value="abc"),
    dict(name="x", targets=[1], kind=KIND_FILE, value=" "),
    dict(name="x", targets=[1], kind="other", value="1"),
])
def test_invalid_swaps(kwargs):
    with pytest.raises(ValueError):
        Swap(**kwargs)


def test_parse_asset_ids():
    text = "123, 456 rbxassetid://789 https://www.roblox.com/library/1011/sky2 https://x/asset/?id=1213"
    assert parse_asset_ids(text) == [123, 456, 789, 1011, 1213]
    with pytest.raises(ValueError):
        parse_asset_ids("hello")
