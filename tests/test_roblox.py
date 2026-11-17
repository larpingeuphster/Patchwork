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

    report = roblox.patch_all(CA, installs)
    assert len(report.patched) == 2 and report.failed[0][0].endswith("version-c")
    assert roblox.patch_all(CA, installs).trusted_count == 2
    assert roblox.unpatch_all(installs).patched
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
