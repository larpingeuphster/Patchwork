import shutil
import socket
import time
from pathlib import Path

import pytest

from patchwork.controller import Controller, UserError

EXAMPLE = Path(__file__).with_name("fleasion_example.json")


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    c = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs")
    yield c
    c.shutdown()


def test_snapshot_shape(ctl):
    snap = ctl.snapshot()
    assert snap["editing"] == "Default" and snap["profiles"][0]["enabled"]
    assert snap["status"] == {"tone": "idle", "short": "Stopped", "text": "Stopped"}
    assert snap["rivals"]["place_id"] == 17625359962
    assert not snap["notice_accepted"]
    ctl.accept_notice()
    assert ctl.snapshot()["notice_accepted"]


def test_swap_lifecycle_and_undo(ctl):
    assert ctl.save_swap("Hit", "1, 2", "https://example.com/hit.ogg") == {"index": 0}
    # First asset-ID swap asks for confirmation, then goes through.
    assert ctl.save_swap("", "3", "rbxassetid://4") == {"confirm": "id_swap"}
    assert ctl.save_swap("", "3", "rbxassetid://4", confirmed=True) == {"index": 1}
    assert ctl.save_swap("", "5", "6") == {"index": 2}  # only asked once per session
    swaps = ctl.snapshot()["swaps"]
    assert [s["mode"] for s in swaps] == ["Link", "ID", "ID"]
    assert ctl.snapshot()["active_count"] == 3

    ctl.save_swap("Hit 2", "1", "https://example.com/hit2.ogg", index=0)
    assert ctl.snapshot()["swaps"][0]["name"] == "Hit 2"
    ctl.set_swaps_enabled([0, 1], False)
    assert ctl.snapshot()["active_count"] == 1
    assert ctl.remove_swaps([0, 2, 99]) == 2
    assert len(ctl.snapshot()["swaps"]) == 1
    assert ctl.undo() and len(ctl.snapshot()["swaps"]) == 3
    with pytest.raises(UserError):
        ctl.save_swap("", "", "5")
    with pytest.raises(UserError):
        ctl.save_swap("", "1", "5", index=42, confirmed=True)


def test_profiles(ctl):
    assert ctl.new_profile("Skins") == "Skins"
    assert ctl.snapshot()["editing"] == "Skins"
    ctl.set_profile_enabled("Skins", True)
    assert ctl.rename_profile("Weapon skins") == "Weapon skins"
    assert ctl.duplicate_profile() == "Weapon skins copy"
    ctl.select_profile("Weapon skins")
    ctl.delete_profile()
    names = [p["name"] for p in ctl.snapshot()["profiles"]]
    assert "Weapon skins" not in names and "Weapon skins copy" in names
    with pytest.raises(UserError):
        ctl.select_profile("nope")
    with pytest.raises(UserError):
        ctl.new_profile("  ")


def test_import_asks_about_links_then_imports(ctl, tmp_path):
    path = tmp_path / "euph.json"
    shutil.copy(EXAMPLE, path)
    first = ctl.import_preset(str(path))
    assert first["confirm"] == "links" and first["count"] == 8
    assert first["hosts"] == ["files.example.com", "raw.githubusercontent.com"]
    done = ctl.import_preset(str(path), confirmed=True)
    assert done["name"] == "euph" and done["count"] == 8
    snap = ctl.snapshot()
    assert snap["editing"] == "euph" and next(p for p in snap["profiles"] if p["name"] == "euph")["enabled"]
    out = tmp_path / "out.json"
    assert ctl.export_preset("fleasion", str(out)) == {"skipped": []}
    assert out.read_text().startswith('{\n  "replacement_rules"')
    with pytest.raises(UserError):
        ctl.import_preset(str(tmp_path / "missing.json"))


def test_detect(ctl):
    assert ctl.detect("") == {"kind": None}
    assert ctl.detect("rbxassetid://5")["mode"] == "ID"
    assert ctl.detect("https://x.example/a.png")["mode"] == "Link"
    assert "error" in ctl.detect("C:/nope/missing.png")


def test_events_long_poll(ctl):
    start = ctl.snapshot()["seq"]
    ctl.log("hello", "warn")
    events = ctl.events_after(start, timeout=1)
    assert any(e["kind"] == "log" and e["data"]["text"] == "hello" for e in events)
    time.sleep(0.3)  # let any background start-up messages land first
    t = time.monotonic()
    assert ctl.events_after(ctl._seq, timeout=0.2) == []
    assert time.monotonic() - t >= 0.15


def test_settings(ctl):
    ctl.set_setting("restore_on_stop", False)
    assert ctl.snapshot()["settings"]["restore_on_stop"] is False
    with pytest.raises(UserError):
        ctl.set_setting("evil", True)


def test_start_and_stop_with_regular_proxy(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ctl = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs", proxy_mode=f"regular@{port}")
    try:
        ctl.start()
        for _ in range(100):
            if ctl.snapshot()["running"]:
                break
            time.sleep(0.1)
        snap = ctl.snapshot()
        assert snap["running"] and snap["status"]["short"] == "Waiting"
        ctl.stop()
        for _ in range(100):
            if not ctl.snapshot()["running"] and not ctl.busy:
                break
            time.sleep(0.1)
        assert not ctl.snapshot()["running"]
        texts = [e["text"] for e in ctl.activity_log()]
        assert any("Patchwork is running" in t for t in texts) and "Patchwork stopped." in texts
    finally:
        ctl.shutdown()


def test_start_asks_when_roblox_is_open(ctl, monkeypatch):
    from patchwork import roblox
    closed = []
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: ["RobloxPlayerBeta.exe"])
    monkeypatch.setattr(roblox, "close_clients", lambda *a, **k: closed.append(True) or ["RobloxPlayerBeta.exe"])
    assert ctl.start() == {"confirm": "roblox_open", "running": ["RobloxPlayerBeta.exe"]}
    assert not ctl.busy and not closed


def test_clear_cache_names_the_program_holding_it(ctl, monkeypatch):
    from patchwork import roblox
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: [])
    monkeypatch.setattr(roblox, "clear_cache", lambda *a, **k: ([], [("C:/x/rbx-storage.db", "in use")]))
    monkeypatch.setattr(roblox, "lockers", lambda paths: [(1234, "Fishstrap")])
    with pytest.raises(UserError, match=r"Fishstrap \(PID 1234\)"):
        ctl.clear_cache()


def test_discovers_install_from_running_roblox(ctl, monkeypatch, tmp_path):
    from patchwork import roblox
    exe = tmp_path / "Froststrap" / "Versions" / "version-abc" / "RobloxPlayerBeta.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(roblox, "_extra_roots", [])
    monkeypatch.setattr(roblox, "client_paths", lambda *a, **k: [exe])
    # On Windows the controller's own background finder may get there first.
    ctl.discover_installs()
    assert ctl.store.install_roots == [str(exe.parent.parent)]
    assert exe.parent.parent in roblox.version_roots()
    assert ctl.discover_installs() == []  # remembered
    assert "Froststrap/version-abc" in ctl.snapshot()["installs"]


def test_clear_cache_offers_to_close_roblox(ctl, monkeypatch):
    from patchwork import roblox
    state = {"running": ["RobloxPlayerBeta.exe"], "closed": 0}
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: list(state["running"]))

    def close(*a, **k):
        state["closed"] += 1
        state["running"] = []
    monkeypatch.setattr(roblox, "close_clients", close)
    monkeypatch.setattr(roblox, "clear_cache", lambda *a, **k: (["rbx-storage.db"], []))
    assert ctl.clear_cache() == {"confirm": "roblox_open", "running": ["RobloxPlayerBeta.exe"]}
    assert state["closed"] == 0
    assert ctl.clear_cache(close_roblox=True) is None
    assert state["closed"] == 1


def test_accepting_the_notice_starts_patchwork(tmp_path, monkeypatch):
    import socket
    from patchwork import roblox
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.setattr(roblox, "running_clients", lambda *a, **k: [])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = Controller(home=tmp_path / "home", log_dir=tmp_path / "logs", proxy_mode=f"regular@{port}")
    try:
        c.auto_start()  # nothing happens before the notice is accepted
        assert not c.busy and not c.runner.running
        c.accept_notice()
        for _ in range(100):
            if c.runner.running:
                break
            time.sleep(0.1)
        assert c.runner.running
    finally:
        c.shutdown()


def test_launch_at_login_setting(ctl, monkeypatch):
    from patchwork import startup
    calls = []
    monkeypatch.setattr(startup, "supported", lambda: True)
    monkeypatch.setattr(startup, "enable", lambda exe=None: calls.append("on"))
    monkeypatch.setattr(startup, "disable", lambda: calls.append("off"))
    ctl.set_setting("launch_at_login", False)
    ctl.set_setting("launch_at_login", True)
    assert calls == ["off", "on"] and ctl.snapshot()["settings"]["launch_at_login"] is True
