import json
from pathlib import Path

import pytest

from patchwork import presets
from patchwork.config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap, swap_from_inputs

EXAMPLE = Path(__file__).with_name("fleasion_example.json")


def test_real_fleasion_preset():
    result = presets.load_preset(EXAMPLE)
    assert result.format == "Fleasion"
    assert not result.skipped
    assert len(result.swaps) == 8
    first = result.swaps[0]
    assert (first.name, first.targets, first.kind) == ("Crosshair left", [1001], KIND_URL)
    assert first.value == "https://files.example.com/left.webp"
    heads = result.swaps[3]
    assert (heads.kind, heads.targets, heads.value) == (KIND_ASSET, [2001, 2002], "70643163489676")
    assert result.swaps[4].enabled is False
    assert result.link_hosts() == ["files.example.com", "raw.githubusercontent.com"]


def test_fleasion_unknown_modes_and_bad_rules_are_skipped():
    data = {"replacement_rules": [
        {"name": "ok", "replace_ids": ["5"], "mode": "id", "with_id": "600"},
        {"name": "gone", "replace_ids": [1], "mode": "remove"},
        {"name": "bad", "replace_ids": [], "mode": "id", "with_id": 1},
        {"name": "badurl", "replace_ids": [1], "mode": "cdn", "cdn_url": "ftp://x"},
        "junk",
    ]}
    result = presets.parse_preset(data)
    assert [s.name for s in result.swaps] == ["ok", "gone"]
    assert result.swaps[0].targets == [5] and result.swaps[0].value == "600"
    assert result.swaps[1].kind == KIND_REMOVE
    assert len(result.skipped) == 3


def test_round_trips(tmp_path):
    swaps = presets.load_preset(EXAMPLE).swaps
    text, skipped = presets.export_fleasion(swaps)
    assert not skipped
    assert json.loads(text) == json.loads(EXAMPLE.read_text())
    again = presets.parse_preset(json.loads(presets.export_patchwork(swaps)))
    assert again.swaps == swaps


def test_fleasion_export_writes_local_files_and_removals(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    text, skipped = presets.export_fleasion([
        Swap("local", [1], KIND_FILE, str(f)),
        Swap("gone", [2, 3], KIND_REMOVE, ""),
    ])
    assert skipped == []
    assert json.loads(text) == {"replacement_rules": [
        {"name": "local", "replace_ids": [1], "mode": "local", "enabled": True, "local_path": str(f)},
        {"name": "gone", "replace_ids": [2, 3], "mode": "id", "enabled": True},
    ]}


def test_fleasion_groups_local_and_remove(tmp_path):
    here = tmp_path / "hit.ogg"
    here.write_bytes(b"x")
    data = {"replacement_rules": [
        {"name": "remove", "replace_ids": [10, 11], "mode": "id", "enabled": True},
        {"name": "zero", "replace_ids": [12], "mode": "id", "enabled": True, "with_id": 0},
        {"name": "here", "replace_ids": [13], "mode": "local", "enabled": False, "local_path": str(here)},
        {"name": "elsewhere", "replace_ids": [14], "mode": "local", "enabled": True,
         "local_path": "C:/Users/Someone/Downloads/x.png"},
        {"type": "group", "name": "Bow", "expanded": True, "children": [
            {"name": "shoot", "replace_ids": [15], "mode": "id", "enabled": True, "with_id": 99},
            {"name": "Arrow", "replace_ids": [16], "mode": "local", "enabled": True, "local_path": "C:/a/arrow.obj"},
            {"type": "group", "name": "Inner", "children": [
                {"name": "deep", "replace_ids": [17], "mode": "cdn", "enabled": True, "cdn_url": "https://x.example/d.png"},
            ]},
        ]},
    ]}
    r = presets.parse_preset(data)
    by = {s.name: s for s in r.swaps}
    assert by["remove"].kind == KIND_REMOVE and by["remove"].targets == [10, 11]
    assert by["zero"].kind == KIND_REMOVE
    assert by["here"].kind == KIND_FILE and by["here"].enabled is False
    assert by["elsewhere"].kind == KIND_FILE
    assert by["Bow / shoot"].value == "99"
    assert by["Bow / Inner / deep"].kind == KIND_URL
    assert not r.skipped
    assert by["Bow / Arrow"].kind == KIND_FILE and by["Bow / Arrow"].value == "C:/a/arrow.obj"  # converted when used
    assert r.missing_files == ["elsewhere: C:/Users/Someone/Downloads/x.png", "Bow / Arrow: C:/a/arrow.obj"]


def test_patchwork_relative_files(tmp_path):
    (tmp_path / "Sky").mkdir()
    (tmp_path / "Sky" / "up.png").write_bytes(b"x")
    preset = tmp_path / "sky.json"
    preset.write_text(json.dumps({"swaps": [{"name": "up", "targets": [1], "kind": "file", "value": "Sky/up.png"}]}))
    swap = presets.load_preset(preset).swaps[0]
    assert Path(swap.value) == (tmp_path / "Sky" / "up.png").resolve()


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"other": 1}', '{"replacement_rules": 5}'])
def test_bad_files(tmp_path, text):
    p = tmp_path / "x.json"
    p.write_text(text)
    with pytest.raises(ValueError):
        presets.load_preset(p)


def test_merge_skips_duplicates():
    a = Swap("a", [1], KIND_ASSET, "2")
    added, dupes = presets.merge([a], [Swap("renamed", [1], KIND_ASSET, "2"), Swap("b", [3], KIND_ASSET, "4")])
    assert dupes == 1 and [s.name for s in added] == ["b"]


def test_swap_from_inputs(tmp_path):
    cfg = tmp_path / "configs"
    (cfg / "Stick").mkdir(parents=True)
    (cfg / "Stick" / "stick.mesh").write_bytes(b"x")
    f = swap_from_inputs("", "1 2", "rbxassetid://55", cfg)
    assert (f.kind, f.value, f.targets, f.name) == (KIND_ASSET, "55", [1, 2], "asset 55")
    assert swap_from_inputs("", "1", "https://www.roblox.com/library/77/sky", cfg).value == "77"
    assert swap_from_inputs("", "1", "https://files.catbox.moe/a.webp", cfg).kind == KIND_URL
    s = swap_from_inputs("Stick", "1", "/Stick/stick.mesh", cfg)
    assert s.kind == KIND_FILE and Path(s.value) == (cfg / "Stick" / "stick.mesh").resolve()
    assert swap_from_inputs("", "1 2", "Remove", cfg).kind == KIND_REMOVE
    assert swap_from_inputs("", "1", "none", cfg).describe() == "removed"
    for ids, value in (("", "5"), ("1", ""), ("1", "/nope.png"), ("abc", "5")):
        with pytest.raises(ValueError):
            swap_from_inputs("", ids, value, cfg)


# --- the Fleasion rewrite (feat/rewrite-ui-v2) -------------------------------------------------


def test_rewrite_rules(tmp_path):
    (tmp_path / "StickObj").mkdir()
    (tmp_path / "StickObj" / "stick.obj").write_text("v 0 0 0")
    data = [  # a bare list of rules is a config too
        {"name": "no mode means id", "replace_ids": [10], "with_id": 999},
        {"name": "legacy remove flag", "replace_ids": ["11"], "remove": True},
        {"name": "pack file", "replace_ids": [12], "mode": "local", "local_path": "/StickObj/stick.obj"},
        {"name": "empty cdn removes", "replace_ids": [13], "mode": "cdn", "cdn_url": ""},
        {"name": "off", "replace_ids": [14], "mode": "id", "with_id": 998, "enabled": False},
        {"name": "words", "replace_ids": ["Image", "15", "3:4"], "mode": "id", "with_id": 997},
        {"name": "reserved", "replace_ids": [16], "mode": "id", "with_id": 13},
        {"name": "filtered", "replace_ids": [17], "mode": "cdn", "cdn_url": "https://x.test/a.png",
         "filters": {"min_kb": 5}},
        {"type": "group", "name": "Bow", "expanded": True, "children": [
            {"name": "string", "replace_ids": [18], "mode": "remove"},
        ]},
    ]
    p = tmp_path / "Sticks.json"
    p.write_text(json.dumps(data), encoding="utf-16")  # Fleasion recovers non-UTF-8 files too
    r = presets.load_preset(p)
    by = {s.name: s for s in r.swaps}
    assert by["no mode means id"].kind == KIND_ASSET and by["no mode means id"].value == "999"
    assert by["legacy remove flag"].kind == KIND_REMOVE and by["empty cdn removes"].kind == KIND_REMOVE
    assert Path(by["pack file"].value) == tmp_path / "StickObj" / "stick.obj" and not r.missing_files
    assert by["off"].enabled is False
    assert by["words"].targets == [15]
    assert "reserved" not in by
    assert by["Bow / string"].kind == KIND_REMOVE
    assert by["filtered"].kind == KIND_URL
    joined = "\n".join(r.skipped)
    assert '"Image"' in joined and '"3:4"' in joined and "with_id 13" in joined and "filters" in joined


def test_configs_folder(tmp_path):
    configs = tmp_path / "FleasionNT" / "configs"
    (configs / "Pack").mkdir(parents=True)
    (configs / "A.json").write_text(json.dumps({"replacement_rules": [{"name": "a", "replace_ids": [1], "with_id": 100}]}))
    (configs / "B.json").write_text(json.dumps({"replacement_rules": []}))
    (configs / "Pack" / "nested.json").write_text("{}")  # nested JSON is an asset, not a config
    (configs / "Broken.json").write_text("{")
    (configs.parent / "settings.json").write_text(json.dumps({"enabled_configs": ["A"]}))
    found, enabled = presets.load_folder(configs)
    assert [n for n, _ in found] == ["A", "B", "Broken"]
    assert enabled == ["A"]
    assert found[0][1].swaps[0].value == "100" and found[2][1].skipped


def test_fleasion_configs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert presets.fleasion_configs_dir() is None
    (tmp_path / "FleasionNT" / "configs").mkdir(parents=True)
    assert presets.fleasion_configs_dir() == tmp_path / "FleasionNT" / "configs"
