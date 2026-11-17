import json
from pathlib import Path

import pytest

from patchwork import presets
from patchwork.config import KIND_ASSET, KIND_FILE, KIND_URL, Swap, swap_from_inputs

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
        {"name": "ok", "replace_ids": ["5"], "mode": "id", "with_id": "6"},
        {"name": "gone", "replace_ids": [1], "mode": "remove"},
        {"name": "bad", "replace_ids": [], "mode": "id", "with_id": 1},
        {"name": "badurl", "replace_ids": [1], "mode": "cdn", "cdn_url": "ftp://x"},
        "junk",
    ]}
    result = presets.parse_preset(data)
    assert [s.name for s in result.swaps] == ["ok"]
    assert result.swaps[0].targets == [5] and result.swaps[0].value == "6"
    assert len(result.skipped) == 4
    assert 'mode "remove"' in result.skipped[0]


def test_round_trips(tmp_path):
    swaps = presets.load_preset(EXAMPLE).swaps
    text, skipped = presets.export_fleasion(swaps)
    assert not skipped
    assert json.loads(text) == json.loads(EXAMPLE.read_text())
    again = presets.parse_preset(json.loads(presets.export_patchwork(swaps)))
    assert again.swaps == swaps


def test_fleasion_export_leaves_out_local_files(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    text, skipped = presets.export_fleasion([Swap("local", [1], KIND_FILE, str(f))])
    assert skipped == ["local"] and json.loads(text) == {"replacement_rules": []}


def test_patchwork_relative_files(tmp_path):
    (tmp_path / "Sky").mkdir()
    (tmp_path / "Sky" / "up.png").write_bytes(b"x")
    preset = tmp_path / "sky.json"
    preset.write_text(json.dumps({"swaps": [{"name": "up", "targets": [1], "kind": "file", "value": "Sky/up.png"}]}))
    swap = presets.load_preset(preset).swaps[0]
    assert Path(swap.value) == (tmp_path / "Sky" / "up.png").resolve()


@pytest.mark.parametrize("text", ["not json", "[]", '{"other": 1}', '{"replacement_rules": 5}'])
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
    for ids, value in (("", "5"), ("1", ""), ("1", "/nope.png"), ("abc", "5")):
        with pytest.raises(ValueError):
            swap_from_inputs("", ids, value, cfg)
