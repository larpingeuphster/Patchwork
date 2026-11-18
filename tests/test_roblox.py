from patchwork import roblox

CA = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
CA2 = "-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n"
ORIGINAL = "# Roblox bundle\n-----BEGIN CERTIFICATE-----\nZZZZ\n-----END CERTIFICATE-----\n"


def make_install(root, name="version-abc", exe="RobloxPlayerBeta.exe", cacert=True):
    d = root / name
    (d / "ssl").mkdir(parents=True)
    (d / exe).write_bytes(b"")
    if cacert:
        (d / "ssl" / "cacert.pem").write_text(ORIGINAL)
    return d


def test_patch_is_idempotent_and_reversible(tmp_path):
    pem = tmp_path / "cacert.pem"
    pem.write_text(ORIGINAL)
    assert roblox.patch_cacert(pem, CA)
    assert not roblox.patch_cacert(pem, CA)
    text = pem.read_text()
    assert text.startswith(ORIGINAL) and text.count(roblox.BEGIN_MARKER) == 1 and "AAAA" in text

    # A new CA replaces the old block instead of stacking.
    assert roblox.patch_cacert(pem, CA2)
    text = pem.read_text()
    assert "AAAA" not in text and "BBBB" in text and text.count(roblox.BEGIN_MARKER) == 1

    assert roblox.unpatch_cacert(pem)
    assert pem.read_text() == ORIGINAL
    assert not roblox.unpatch_cacert(pem)


def test_patch_handles_missing_trailing_newline(tmp_path):
    pem = tmp_path / "cacert.pem"
    pem.write_text(ORIGINAL.rstrip("\n"))
    roblox.patch_cacert(pem, CA)
    assert "-----END CERTIFICATE-----\n\n" + roblox.BEGIN_MARKER in pem.read_text()


def test_find_and_patch_all(tmp_path):
    root = tmp_path / "Versions"
    a = make_install(root, "version-a")
    make_install(root, "version-b", exe="RobloxStudioBeta.exe")
    make_install(root, "version-c", cacert=False)
    (root / "version-junk").mkdir()  # no executable: not an install
    installs = roblox.find_installs([root])
    assert [i.version_dir.name for i in installs] == ["version-a", "version-b", "version-c"]

    report = roblox.patch_all(CA, installs, [])
    assert len(report.patched) == 2 and report.failed[0][0].endswith("version-c")
    assert roblox.patch_all(CA, installs, []).trusted_count == 2
    assert roblox.unpatch_all(installs, []).patched
    assert (a / "ssl" / "cacert.pem").read_text() == ORIGINAL


def test_version_roots(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    (local / "Roblox" / "Versions").mkdir(parents=True)
    (local / "Bloxstrap" / "Versions").mkdir(parents=True)
    (local / "Other").mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    roots = {p.parent.name for p in roblox.version_roots()}
    assert roots == {"Roblox", "Bloxstrap"}


def test_clear_cache(tmp_path):
    d = tmp_path / "rbx-storage"
    (d / "x").mkdir(parents=True)
    f = tmp_path / "rbx-storage.db"
    f.write_text("x")
    removed, failed = roblox.clear_cache([d, f, tmp_path / "missing"])
    assert len(removed) == 2 and not failed
    assert not d.exists() and not f.exists()


import json  # noqa: E402


def test_client_settings_patch_and_restore(tmp_path):
    path = tmp_path / "ClientSettings" / "ClientAppSettings.json"
    assert roblox.patch_client_settings(path)
    assert json.loads(path.read_text()) == {"DFFlagHttpUseEmbeddedCaCerts": "False"}
    assert not roblox.patch_client_settings(path)
    assert roblox.unpatch_client_settings(path)
    assert not path.exists()  # we created it, so it goes away again


def test_client_settings_keeps_existing_flags(tmp_path):
    path = tmp_path / "ClientAppSettings.json"
    path.write_text(json.dumps({"FIntSomething": "5"}))
    roblox.patch_client_settings(path)
    assert json.loads(path.read_text()) == {"FIntSomething": "5", "DFFlagHttpUseEmbeddedCaCerts": "False"}
    roblox.unpatch_client_settings(path)
    assert json.loads(path.read_text()) == {"FIntSomething": "5"}


def test_patch_all_sets_flag_and_launcher_modifications(tmp_path):
    launcher = tmp_path / "Bloxstrap"
    v = make_install(launcher / "Versions", "version-a")
    mods = launcher / "Modifications"
    mods.mkdir()
    installs = roblox.find_installs([launcher / "Versions"])
    assert roblox.modifications_dirs([launcher / "Versions"]) == [mods]

    report = roblox.patch_all(CA, installs, [mods])
    assert report.trusted_count == 2 and not report.failed
    assert json.loads((v / "ClientSettings" / "ClientAppSettings.json").read_text())
    mod_cert = (mods / "ssl" / "cacert.pem").read_text()
    assert mod_cert.startswith(ORIGINAL) and "AAAA" in mod_cert
    assert json.loads((mods / "ClientSettings" / "ClientAppSettings.json").read_text())

    roblox.unpatch_all(installs, [mods])
    assert (v / "ssl" / "cacert.pem").read_text() == ORIGINAL
    assert not (v / "ClientSettings" / "ClientAppSettings.json").exists()
    assert not (mods / "ssl" / "cacert.pem").exists()
    assert not (mods / "ClientSettings" / "ClientAppSettings.json").exists()
    assert not list(mods.rglob("*.patchwork-created"))


def test_existing_launcher_cacert_is_patched_not_replaced(tmp_path):
    mods = tmp_path / "Modifications"
    (mods / "ssl").mkdir(parents=True)
    (mods / "ssl" / "cacert.pem").write_text("# user's own bundle\n")
    roblox.patch_modifications(mods, CA, None)
    roblox.unpatch_modifications(mods)
    assert (mods / "ssl" / "cacert.pem").read_text() == "# user's own bundle\n"


def test_lockers_finds_this_process_on_windows(tmp_path):
    import os
    import sys

    import pytest

    if sys.platform != "win32":
        assert roblox.lockers([tmp_path / "x"]) == []
        pytest.skip("Restart Manager is Windows-only")
    held = tmp_path / "rbx-storage.db"
    with open(held, "w") as fh:
        fh.write("x")
        fh.flush()
        found = roblox.lockers([held])
    assert any(pid == os.getpid() for pid, _ in found), found
