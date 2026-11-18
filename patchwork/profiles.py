"""Swap profiles: one JSON file per profile in ``<app dir>/configs``.

* Each file is a preset (Patchwork or Fleasion format, see presets.py), so a
  Fleasion preset can simply be dropped into the folder.
* Replacement files can live in subfolders of ``configs`` and are stored
  relative to it, which keeps a profile and its files portable.
* App settings (which profiles are on, which one is being edited, advanced
  options) live in ``settings.json``.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import presets
from .config import DEFAULT_INTERCEPT_HOSTS, DEFAULT_PROCESSES, KIND_FILE, Config, Swap, app_dir

DEFAULT_PROFILE = "Default"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def clean_name(name: str) -> str:
    """A profile name that is also a safe Windows file name. Raises ValueError if empty."""
    cleaned = _BAD_CHARS.sub("", name).strip().rstrip(".").strip()
    if not cleaned:
        raise ValueError("Give the profile a name (letters, numbers, spaces).")
    if cleaned.lower() in _RESERVED:
        cleaned += "_"
    return cleaned[:80]


@dataclass
class Snapshot:
    profiles: dict[str, list[Swap]]
    enabled: list[str]
    editing: str


@dataclass
class Store:
    home: Path
    profiles: dict[str, list[Swap]] = field(default_factory=dict)
    enabled: list[str] = field(default_factory=list)
    editing: str = DEFAULT_PROFILE
    intercept_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_INTERCEPT_HOSTS))
    processes: list[str] = field(default_factory=lambda: list(DEFAULT_PROCESSES))
    notice_accepted: bool = False  # the first-run risk notice
    restore_on_stop: bool = True  # Stop also clears the cache and undoes Roblox changes
    warn_id_swaps: bool = True  # explain that ID swaps are visible to Roblox's servers
    rivals_universes: list[int] = field(default_factory=list)  # learned from Roblox's log
    warnings: list[str] = field(default_factory=list)
    _undo: list[Snapshot] = field(default_factory=list)

    # ----- paths ---------------------------------------------------------------

    @property
    def configs_dir(self) -> Path:
        return self.home / "configs"

    @property
    def settings_path(self) -> Path:
        return self.home / "settings.json"

    def profile_path(self, name: str) -> Path:
        return self.configs_dir / f"{name}.json"

    # ----- loading -------------------------------------------------------------

    @classmethod
    def load(cls, home: Path | None = None) -> "Store":
        store = cls(home or app_dir())
        store.configs_dir.mkdir(parents=True, exist_ok=True)
        store._migrate_old_config()
        store.reload_profiles()
        settings = {}
        if store.settings_path.exists():
            try:
                settings = json.loads(store.settings_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                store.warnings.append(f"settings.json couldn't be read ({exc}); using defaults.")
        store.enabled = [n for n in settings.get("enabled", [DEFAULT_PROFILE]) if n in store.profiles]
        store.editing = settings.get("editing", DEFAULT_PROFILE)
        store.intercept_hosts = list(settings.get("intercept_hosts") or DEFAULT_INTERCEPT_HOSTS)
        store.processes = list(settings.get("processes") or DEFAULT_PROCESSES)
        store.notice_accepted = bool(settings.get("notice_accepted", False))
        store.restore_on_stop = bool(settings.get("restore_on_stop", True))
        store.warn_id_swaps = bool(settings.get("warn_id_swaps", True))
        store.rivals_universes = [int(u) for u in settings.get("rivals_universes", []) if str(u).isdigit()]
        if not store.profiles:
            store.profiles[DEFAULT_PROFILE] = []
            store.save_profile(DEFAULT_PROFILE)
            store.enabled = [DEFAULT_PROFILE]
        if store.editing not in store.profiles:
            store.editing = next(iter(store.profiles))
        return store

    def _migrate_old_config(self) -> None:
        """0.1.0 kept one swap list in config.json; turn it into the Default profile."""
        old = self.home / "config.json"
        if not old.exists() or any(self.configs_dir.glob("*.json")):
            return
        try:
            cfg = Config.load(old)
        except (ValueError, TypeError, OSError) as exc:
            self.warnings.append(f"The old config.json couldn't be read ({exc}).")
            return
        self.profiles[DEFAULT_PROFILE] = cfg.swaps
        self.save_profile(DEFAULT_PROFILE)
        self.intercept_hosts, self.processes = cfg.intercept_hosts, cfg.processes
        self.enabled = [DEFAULT_PROFILE]
        self.save_settings()
        os.replace(old, old.with_name("config.migrated.json"))

    def reload_profiles(self) -> None:
        """Read every profile file. Unreadable files are reported, never deleted."""
        profiles: dict[str, list[Swap]] = {}
        for path in sorted(self.configs_dir.glob("*.json"), key=lambda p: p.stem.lower()):
            try:
                result = presets.load_preset(path)
            except (ValueError, OSError) as exc:
                self.warnings.append(f"Skipped {path.name}: {exc}")
                continue
            for reason in result.skipped:
                self.warnings.append(f"{path.stem}: skipped {reason}")
            profiles[path.stem] = result.swaps
        self.profiles = profiles

    # ----- saving --------------------------------------------------------------

    def save_settings(self) -> None:
        data = {
            "editing": self.editing,
            "enabled": self.enabled,
            "intercept_hosts": self.intercept_hosts,
            "processes": self.processes,
            "notice_accepted": self.notice_accepted,
            "restore_on_stop": self.restore_on_stop,
            "warn_id_swaps": self.warn_id_swaps,
            "rivals_universes": self.rivals_universes,
        }
        _atomic_write(self.settings_path, json.dumps(data, indent=2))

    def save_profile(self, name: str) -> None:
        swaps = []
        for swap in self.profiles[name]:
            swap = copy.copy(swap)
            if swap.kind == KIND_FILE:
                swap.value = self.portable_path(swap.value)
            swaps.append(swap)
        _atomic_write(self.profile_path(name), presets.export_patchwork(swaps))

    def portable_path(self, value: str) -> str:
        """Files inside the configs folder are stored relative to it."""
        try:
            return Path(value).resolve().relative_to(self.configs_dir.resolve()).as_posix()
        except ValueError:
            return value

    def save_all(self) -> None:
        for name in self.profiles:
            self.save_profile(name)
        self.save_settings()

    # ----- undo ----------------------------------------------------------------

    def checkpoint(self) -> None:
        """Remember the current state so the next change can be undone."""
        self._undo.append(Snapshot(copy.deepcopy(self.profiles), list(self.enabled), self.editing))
        del self._undo[:-50]

    def undo(self) -> bool:
        if not self._undo:
            return False
        snap = self._undo.pop()
        for name in set(self.profiles) - set(snap.profiles):
            self.profile_path(name).unlink(missing_ok=True)
        self.profiles, self.enabled, self.editing = snap.profiles, snap.enabled, snap.editing
        self.save_all()
        return True

    # ----- profile operations ----------------------------------------------------

    def _unused(self, name: str) -> str:
        name = clean_name(name)
        if name.lower() not in {n.lower() for n in self.profiles}:
            return name
        n = 2
        while f"{name} {n}".lower() in {p.lower() for p in self.profiles}:
            n += 1
        return f"{name} {n}"

    def create(self, name: str, swaps: list[Swap] | None = None) -> str:
        name = self._unused(name)
        self.profiles[name] = list(swaps or [])
        self.save_profile(name)
        self.editing = name
        self.save_settings()
        return name

    def duplicate(self, name: str) -> str:
        return self.create(f"{name} copy", copy.deepcopy(self.profiles[name]))

    def rename(self, old: str, new: str) -> str:
        new = clean_name(new)
        if new == old:
            return old
        if new.lower() != old.lower() and new.lower() in {n.lower() for n in self.profiles}:
            raise ValueError(f'A profile called "{new}" already exists.')
        swaps = self.profiles.pop(old)
        self.profiles = {**self.profiles, new: swaps}
        self.profile_path(old).unlink(missing_ok=True)
        self.save_profile(new)
        self.enabled = [new if n == old else n for n in self.enabled]
        if self.editing == old:
            self.editing = new
        self.save_settings()
        return new

    def delete(self, name: str) -> None:
        del self.profiles[name]
        self.profile_path(name).unlink(missing_ok=True)
        self.enabled = [n for n in self.enabled if n != name]
        if not self.profiles:
            self.profiles[DEFAULT_PROFILE] = []
            self.save_profile(DEFAULT_PROFILE)
        if self.editing not in self.profiles:
            self.editing = next(iter(self.profiles))
        self.save_settings()

    def set_enabled(self, name: str, on: bool) -> None:
        if on and name not in self.enabled:
            self.enabled.append(name)
        elif not on and name in self.enabled:
            self.enabled.remove(name)
        self.save_settings()

    # ----- what the proxy uses ---------------------------------------------------

    def active_swaps(self) -> list[Swap]:
        """Swaps from every enabled profile, in profile order (earlier profiles win)."""
        swaps = []
        for name in self.profiles:
            if name in self.enabled:
                swaps.extend(self.profiles[name])
        return swaps

    def to_config(self) -> Config:
        return Config(swaps=self.active_swaps(), intercept_hosts=self.intercept_hosts, processes=self.processes)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
