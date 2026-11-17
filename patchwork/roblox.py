"""Find Roblox installs, trust Patchwork's certificate in them, and clear the asset cache.

Roblox does not use the Windows certificate store. Each version folder ships
its own ``ssl/cacert.pem`` bundle, so Patchwork appends its per-PC certificate
authority to that file between marker lines, and removes it again on request.
Roblox writes a fresh version folder on every update, so patching is
idempotent and safe to repeat.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "# >>> Patchwork CA: added by Patchwork, remove it from the Patchwork app >>>"
END_MARKER = "# <<< Patchwork CA <<<"

CLIENT_EXES = ("RobloxPlayerBeta.exe", "RobloxStudioBeta.exe")


@dataclass(frozen=True)
class Install:
    version_dir: Path

    @property
    def cacert(self) -> Path:
        return self.version_dir / "ssl" / "cacert.pem"

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


@dataclass
class PatchReport:
    patched: list[str]
    already: list[str]
    failed: list[tuple[str, str]]

    @property
    def trusted_count(self) -> int:
        return len(self.patched) + len(self.already)


def patch_all(ca_pem: str, installs: list[Install] | None = None) -> PatchReport:
    report = PatchReport([], [], [])
    for install in installs if installs is not None else find_installs():
        if not install.cacert.exists():
            report.failed.append((install.label, "no ssl/cacert.pem in this version"))
            continue
        try:
            changed = patch_cacert(install.cacert, ca_pem)
        except OSError as exc:
            report.failed.append((install.label, str(exc)))
            continue
        (report.patched if changed else report.already).append(install.label)
    return report


def unpatch_all(installs: list[Install] | None = None) -> PatchReport:
    report = PatchReport([], [], [])
    for install in installs if installs is not None else find_installs():
        if not install.cacert.exists():
            continue
        try:
            changed = unpatch_cacert(install.cacert)
        except OSError as exc:
            report.failed.append((install.label, str(exc)))
            continue
        (report.patched if changed else report.already).append(install.label)
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
