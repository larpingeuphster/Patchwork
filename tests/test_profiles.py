import json
import shutil
from pathlib import Path

import pytest

from patchwork.config import KIND_ASSET, KIND_FILE, Config, Swap
from patchwork.profiles import Store, clean_name

EXAMPLE = Path(__file__).with_name("fleasion_example.json")


def test_fresh_store_has_default(tmp_path):
    s = Store.load(tmp_path)
    assert list(s.profiles) == ["Default"] and s.enabled == ["Default"]
    assert (tmp_path / "configs" / "Default.json").exists()


def test_migrates_old_config(tmp_path):
    Config(swaps=[Swap("Sky", [1], KIND_ASSET, "2")], processes=["X.exe"]).save(tmp_path / "config.json")
    s = Store.load(tmp_path)
    assert s.profiles["Default"][0].name == "Sky"
    assert s.processes == ["X.exe"]
    assert (tmp_path / "config.migrated.json").exists() and not (tmp_path / "config.json").exists()
    assert Store.load(tmp_path).profiles["Default"][0].name == "Sky"


def test_dropped_in_fleasion_file_is_a_profile(tmp_path):
    (tmp_path / "configs").mkdir()
    shutil.copy(EXAMPLE, tmp_path / "configs" / "euph.json")
    s = Store.load(tmp_path)
    assert len(s.profiles["euph"]) == 8
    assert s.enabled == []  # not switched on until the user ticks it


def test_profile_operations_and_active_swaps(tmp_path):
    s = Store.load(tmp_path)
    s.profiles["Default"].append(Swap("a", [1], KIND_ASSET, "2"))
    s.save_profile("Default")
    name = s.create("Guns", [Swap("b", [1], KIND_ASSET, "3"), Swap("c", [4], KIND_ASSET, "5")])
    assert s.editing == "Guns"
    s.set_enabled("Guns", True)
    # Default comes first alphabetically and wins asset 1.
    assert [x.name for x in s.active_swaps()] == ["a", "b", "c"]
    dup = s.duplicate("Guns")
    assert dup == "Guns copy" and len(s.profiles[dup]) == 2
    assert s.create("guns") == "guns 2"
    s.rename("Guns", "Weapons")
    assert "Weapons" in s.enabled and not (tmp_path / "configs" / "Guns.json").exists()
    with pytest.raises(ValueError):
        s.rename("Weapons", "Default")
    s.delete("Weapons")
    assert "Weapons" not in s.enabled

    reloaded = Store.load(tmp_path)
    assert set(reloaded.profiles) == {"Default", "Guns copy", "guns 2"}
    assert reloaded.enabled == ["Default"]


def test_undo(tmp_path):
    s = Store.load(tmp_path)
    s.checkpoint()
    s.create("Temp", [Swap("a", [1], KIND_ASSET, "2")])
    s.checkpoint()
    s.profiles["Temp"].clear()
    s.save_profile("Temp")
    assert s.undo()
    assert len(s.profiles["Temp"]) == 1
    assert s.undo()
    assert "Temp" not in s.profiles and not (tmp_path / "configs" / "Temp.json").exists()
    assert not s.undo()


def test_files_in_configs_are_stored_relative(tmp_path):
    s = Store.load(tmp_path)
    folder = tmp_path / "configs" / "Stick"
    folder.mkdir()
    (folder / "stick.mesh").write_bytes(b"x")
    s.profiles["Default"].append(Swap("stick", [1], KIND_FILE, str(folder / "stick.mesh")))
    s.save_profile("Default")
    raw = json.loads((tmp_path / "configs" / "Default.json").read_text())
    assert raw["swaps"][0]["value"] == "Stick/stick.mesh"
    assert Path(Store.load(tmp_path).profiles["Default"][0].value) == (folder / "stick.mesh").resolve()


def test_broken_profile_is_reported_not_deleted(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "broken.json").write_text("{nope")
    s = Store.load(tmp_path)
    assert "broken" not in s.profiles and any("broken.json" in w for w in s.warnings)
    assert (tmp_path / "configs" / "broken.json").exists()


@pytest.mark.parametrize("raw,clean", [("My: Sky?", "My Sky"), ("con", "con_"), (" a. ", "a")])
def test_clean_name(raw, clean):
    assert clean_name(raw) == clean


def test_clean_name_rejects_empty():
    with pytest.raises(ValueError):
        clean_name(' /:* ')
