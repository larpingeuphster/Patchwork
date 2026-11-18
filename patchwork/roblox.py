"""Find Roblox installs, trust Patchwork's certificate in them, and clear the asset cache.

Roblox does not use the Windows certificate store. Each version folder ships
its own ``ssl/cacert.pem`` bundle, so Patchwork appends its per-PC certificate
authority to that file between marker lines, and removes it again on request.
Roblox writes a fresh version folder on every update, so patching is
idempotent and safe to repeat.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "# >>> Patchwork CA: added by Patchwork, remove it from the Patchwork app >>>"
END_MARKER = "# <<< Patchwork CA <<<"

CLIENT_EXES = ("RobloxPlayerBeta.exe", "RobloxStudioBeta.exe")

# Roblox can use a certificate list built into the client instead of
# ssl/cacert.pem. This setting makes it read cacert.pem (Fleasion sets the same
# one). It is written in the string form launchers such as Bloxstrap use.
CLIENT_FLAGS = {"DFFlagHttpUseEmbeddedCaCerts": "False"}
CREATED_SUFFIX = ".patchwork-created"


@dataclass(frozen=True)
class Install:
    version_dir: Path

    @property
    def cacert(self) -> Path:
        return self.version_dir / "ssl" / "cacert.pem"

    @property
    def client_settings(self) -> Path:
        return self.version_dir / "ClientSettings" / "ClientAppSettings.json"

    @property
    def label(self) -> str:
        return f"{self.version_dir.parent.parent.name}/{self.version_dir.name}"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def version_roots() -> list[Path]:
    """Folders that may contain Roblox ``version-*`` folders.

    Covers the standard installer and launchers such as Bloxstrap that keep
    their own copy under %LOCALAPPDATA%\\<Launcher>\\Versions.
    """
    roots: list[Path] = []
    local = _env_path("LOCALAPPDATA")
    if local and local.is_dir():
        try:
            for child in local.iterdir():
                if (child / "Versions").is_dir():
                    roots.append(child / "Versions")
        except OSError:
            pass
    for name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = _env_path(name)
        if base and (base / "Roblox" / "Versions").is_dir():
            roots.append(base / "Roblox" / "Versions")
    # De-duplicate while keeping order.
    seen: set[Path] = set()
    unique = []
    for root in roots:
        key = root.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def find_installs(roots: list[Path] | None = None) -> list[Install]:
    installs = []
    for root in roots if roots is not None else version_roots():
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for version_dir in children:
            if not version_dir.is_dir():
                continue
            if any((version_dir / exe).exists() for exe in CLIENT_EXES):
                installs.append(Install(version_dir))
    return installs


def _strip_block(text: str) -> str:
    start = text.find(BEGIN_MARKER)
    while start != -1:
        end = text.find(END_MARKER, start)
        if end == -1:
            text = text[:start]
            break
        end += len(END_MARKER)
        # Drop the newline we added before the block and the one after it.
        if start > 0 and text[start - 1] == "\n":
            start -= 1
        if text[end : end + 1] == "\n":
            end += 1
        text = text[:start] + text[end:]
        start = text.find(BEGIN_MARKER)
    return text


def _block(ca_pem: str) -> str:
    return f"\n{BEGIN_MARKER}\n{ca_pem.strip()}\n{END_MARKER}\n"


def patch_cacert(path: Path, ca_pem: str) -> bool:
    """Make sure ``path`` trusts ``ca_pem``. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    clean = _strip_block(text)
    if not clean.endswith("\n"):
        clean += "\n"
    patched = clean + _block(ca_pem)
    if patched == text:
        return False
    path.write_text(patched, encoding="utf-8", errors="surrogateescape")
    return True


def unpatch_cacert(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    clean = _strip_block(text)
    if clean == text:
        return False
    path.write_text(clean, encoding="utf-8", errors="surrogateescape")
    return True


def _created_marker(path: Path) -> Path:
    return path.with_name(path.name + CREATED_SUFFIX)


def patch_client_settings(path: Path, flags: dict[str, str] = CLIENT_FLAGS) -> bool:
    """Add ``flags`` to a ClientAppSettings.json, keeping everything else in it."""
    data: dict = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{path} isn't a settings object")
    if all(data.get(k) == v for k, v in flags.items()):
        return False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _created_marker(path).write_text("Patchwork created this settings file\n", encoding="utf-8")
    data.update(flags)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def unpatch_client_settings(path: Path, flags: dict[str, str] = CLIENT_FLAGS) -> bool:
    marker = _created_marker(path)
    if not path.exists():
        marker.unlink(missing_ok=True)
        return False
    data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    if not isinstance(data, dict):
        return False
    removed = [k for k, v in flags.items() if data.get(k) == v]
    for key in removed:
        del data[key]
    if marker.exists() and not data:
        path.unlink()
        marker.unlink()
        return True
    marker.unlink(missing_ok=True)
    if removed:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return bool(removed)


def modifications_dirs(roots: list[Path] | None = None) -> list[Path]:
    """Launcher override folders (Bloxstrap, Fishstrap, ...) that sit next to their Versions folder.

    These launchers copy files from Modifications over Roblox on every launch,
    so Patchwork's changes must be there too or the launcher would undo them.
    """
    found = []
    for root in roots if roots is not None else version_roots():
        mods = root.parent / "Modifications"
        if mods.is_dir():
            found.append(mods)
    return found


def patch_modifications(mods: Path, ca_pem: str, base_cacert: Path | None) -> bool:
    changed = patch_client_settings(mods / "ClientSettings" / "ClientAppSettings.json")
    cacert = mods / "ssl" / "cacert.pem"
    if not cacert.exists():
        if base_cacert is None or not base_cacert.exists():
            return changed
        cacert.parent.mkdir(parents=True, exist_ok=True)
        text = _strip_block(base_cacert.read_text(encoding="utf-8", errors="surrogateescape"))
        cacert.write_text(text, encoding="utf-8", errors="surrogateescape")
        _created_marker(cacert).write_text("Patchwork created this file\n", encoding="utf-8")
    return patch_cacert(cacert, ca_pem) or changed


def unpatch_modifications(mods: Path) -> bool:
    changed = unpatch_client_settings(mods / "ClientSettings" / "ClientAppSettings.json")
    cacert = mods / "ssl" / "cacert.pem"
    marker = _created_marker(cacert)
    if marker.exists():
        cacert.unlink(missing_ok=True)
        marker.unlink()
        return True
    if cacert.exists():
        changed = unpatch_cacert(cacert) or changed
    return changed


@dataclass
class PatchReport:
    patched: list[str]
    already: list[str]
    failed: list[tuple[str, str]]

    @property
    def trusted_count(self) -> int:
        return len(self.patched) + len(self.already)


def patch_all(
    ca_pem: str, installs: list[Install] | None = None, mods: list[Path] | None = None
) -> PatchReport:
    """Trust ``ca_pem`` and set the client setting in every install and launcher override folder."""
    report = PatchReport([], [], [])
    installs = installs if installs is not None else find_installs()
    for install in installs:
        if not install.cacert.exists():
            report.failed.append((install.label, "no ssl/cacert.pem in this version"))
            continue
        try:
            changed = patch_cacert(install.cacert, ca_pem)
            changed = patch_client_settings(install.client_settings) or changed
        except (OSError, ValueError) as exc:
            report.failed.append((install.label, str(exc)))
            continue
        (report.patched if changed else report.already).append(install.label)
    base = next((i.cacert for i in installs if i.cacert.exists()), None)
    for folder in mods if mods is not None else modifications_dirs():
        label = f"{folder.parent.name}/Modifications"
        try:
            changed = patch_modifications(folder, ca_pem, base)
        except (OSError, ValueError) as exc:
            report.failed.append((label, str(exc)))
            continue
        (report.patched if changed else report.already).append(label)
    return report


def unpatch_all(installs: list[Install] | None = None, mods: list[Path] | None = None) -> PatchReport:
    """Undo everything patch_all did."""
    report = PatchReport([], [], [])
    for install in installs if installs is not None else find_installs():
        try:
            changed = install.cacert.exists() and unpatch_cacert(install.cacert)
            changed = unpatch_client_settings(install.client_settings) or changed
        except (OSError, ValueError) as exc:
            report.failed.append((install.label, str(exc)))
            continue
        (report.patched if changed else report.already).append(install.label)
    for folder in mods if mods is not None else modifications_dirs():
        label = f"{folder.parent.name}/Modifications"
        try:
            changed = unpatch_modifications(folder)
        except (OSError, ValueError) as exc:
            report.failed.append((label, str(exc)))
            continue
        (report.patched if changed else report.already).append(label)
    return report


def cache_paths() -> list[Path]:
    """Places Roblox keeps downloaded assets. Swaps only apply to assets it re-downloads."""
    paths = []
    local = _env_path("LOCALAPPDATA")
    if local:
        roblox = local / "Roblox"
        paths += [
            roblox / "rbx-storage",
            roblox / "rbx-storage.db",
            roblox / "rbx-storage.db-wal",
            roblox / "rbx-storage.db-shm",
        ]
    temp = _env_path("TEMP")
    if temp:
        paths.append(temp / "Roblox" / "http")
    return paths


def clear_cache(paths: list[Path] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    removed, failed = [], []
    for path in paths if paths is not None else cache_paths():
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(str(path))
        except OSError as exc:
            failed.append((str(path), str(exc)))
    return removed, failed


def running_clients(processes: tuple[str, ...] = CLIENT_EXES) -> list[str]:
    """Names of Roblox processes that are running right now (Windows only)."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    running = set()
    wanted = {p.lower(): p for p in processes}
    for line in out.splitlines():
        name = line.split(",", 1)[0].strip('"').lower()
        if name in wanted:
            running.add(wanted[name])
    return sorted(running)


def close_clients(processes: tuple[str, ...] = CLIENT_EXES, timeout: float = 8.0) -> list[str]:
    """Close running Roblox processes (Windows only). Returns the ones that were running."""
    running = running_clients(processes)
    if not running or sys.platform != "win32":
        return running
    for name in running:
        try:
            subprocess.run(["taskkill", "/IM", name, "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and running_clients(processes):
        time.sleep(0.3)
    # Give Windows a moment to release Roblox's cache files.
    time.sleep(0.5)
    return running
