"""Everything the window does, with no UI code: profiles, swaps, start/stop, safety.

The web interface (server.py + webui/) calls these methods and redraws from
``snapshot()``. Events (state changes, activity lines) are queued with a
sequence number so the page can long-poll for them.
"""

from __future__ import annotations

import collections
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, cache, capture, games, presets, roblox, startup
from .config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap, app_dir, swap_from_inputs
from .profiles import Store
from .rules import Hit
from .swapper import CacheRunner

log = logging.getLogger("patchwork")

SUMMARY_SECONDS = 15
MODE_LABELS = {KIND_ASSET: "ID", KIND_URL: "Link", KIND_FILE: "File", KIND_REMOVE: "Remove"}


class UserError(Exception):
    """A problem to show the user as-is."""


@dataclass
class Event:
    seq: int
    kind: str
    data: object


class Controller:
    def __init__(self, home: Path | None = None, log_dir: Path | None = None, cache_dir: Path | None = None):
        self.home = home or app_dir()
        self.store = Store.load(self.home)
        self._lock = threading.RLock()
        self._events: collections.deque[Event] = collections.deque(maxlen=500)
        self._seq = 0
        self._cond = threading.Condition()
        self.activity: collections.deque[dict] = collections.deque(maxlen=400)
        self.busy = False
        self.id_warning_shown = False
        self._last_game: tuple[bool, bool] | None = None
        self._summary_timer: threading.Timer | None = None
        self._started_at = 0.0
        self._last_summary: tuple | None = None
        self._warned_quiet = False
        self.file_dialog = None  # set by the window: (kind, save, default_name) -> path | None
        self.tray_available = False  # set by the window when the tray icon is running

        roblox.set_extra_roots([Path(r) for r in self.store.install_roots])
        self.watcher = games.GameWatcher(
            log_dir if log_dir is not None else games.log_dir(),
            on_change=lambda state: self._game_changed(),
            rivals_universes=set(self.store.rivals_universes),
            is_running=(lambda: bool(roblox.running_clients())) if sys.platform == "win32" else None,
        )
        self.runner = CacheRunner(
            self.store.to_config(),
            self.home,
            on_hit=self._hit,
            on_log=lambda level, msg: self.log(msg, "warn" if level in ("WARNING", "ERROR", "CRITICAL") else ""),
            cache_dir=cache_dir,
        )
        self.captures = capture.Captures(self.home)
        self._cap: dict = {"stage": None, "mark": None, "original": [], "replacement": [], "bodies": {}}
        self._push_captures()
        self.log(f"Patchwork swaps {games.RIVALS_NAME} assets in Roblox's cache on this PC. It works on its own: add "
                 "swaps, switch a profile on, and play. A swap shows up once Roblox has downloaded that asset and "
                 "loads it again (usually your next match).")
        for warning in self.store.warnings:
            self.log(warning, "warn")
        if self.store.migrated_keep_swaps:
            self.store.save_settings()
            self.log("Swaps now stay in Roblox's cache when Patchwork quits, so they load as soon as RIVALS starts "
                     "instead of a match later. Change this in Settings if you prefer.")
        self.watcher.start()
        threading.Thread(target=self._remove_old_setup, name="patchwork-cleanup", daemon=True).start()

    # ----- events ---------------------------------------------------------------

    def _emit(self, kind: str, data: object = None) -> None:
        with self._cond:
            self._seq += 1
            self._events.append(Event(self._seq, kind, data))
            self._cond.notify_all()

    def events_after(self, seq: int, timeout: float = 20.0) -> list[dict]:
        """Events newer than ``seq``; waits up to ``timeout`` seconds for one to arrive."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self._events if e.seq > seq]
                if fresh:
                    return [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in fresh]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)

    def log(self, text: str, tag: str = "") -> None:
        entry = {"t": time.time(), "text": text, "tag": tag}
        self.activity.append(entry)
        self._emit("log", entry)

    def _changed(self) -> None:
        self._emit("state")

    def _hit(self, hit: Hit) -> None:
        if hit.asset_id == 0:
            self.log(f"Applied captured skin {hit.swap_name} in Roblox's cache ({hit.replacement})", "hit")
            return
        self.log(f"Swapped {hit.asset_id} → {hit.replacement} in Roblox's cache  ({hit.swap_name})", "hit")

    # ----- snapshot ---------------------------------------------------------------

    def _display_value(self, swap: Swap) -> str:
        if swap.kind == KIND_REMOVE:
            return "remove"
        if swap.kind == KIND_FILE:
            portable = self.store.portable_path(swap.value)
            return "/" + portable if portable != swap.value else swap.value
        return swap.value

    def status(self) -> dict:
        state = self.watcher.state
        rivals = games.RIVALS_NAME
        if self.busy:
            return {"tone": "busy", "short": "Working…", "text": "Working…"}
        if not self.runner.running:
            return {"tone": "idle", "short": "Stopped", "text": "Stopped"}
        if not self.runner.store.exists():
            return {"tone": "waiting", "short": "No cache yet", "text": "Waiting for Roblox's cache (open Roblox once)"}
        swapped = self.runner.swapper.stats.get("swapped", 0)
        where = f"In {rivals} · " if state.in_rivals else ""
        return {"tone": "on", "short": f"{swapped} swapped", "text": f"{where}{swapped} swapped in Roblox's cache"}

    def snapshot(self) -> dict:
        with self._lock:
            store = self.store
            state = self.watcher.state
            swaps = store.profiles[store.editing]
            return {
                "version": __version__,
                "running": self.runner.running,
                "busy": self.busy,
                "status": self.status(),
                "game": {"in_game": state.in_game, "in_rivals": state.in_rivals, "text": state.describe()},
                "editing": store.editing,
                "profiles": [
                    {"name": n, "enabled": n in store.enabled, "count": len(s),
                     "on": sum(1 for x in s if x.enabled)}
                    for n, s in store.profiles.items()
                ],
                "swaps": [
                    {
                        "index": i, "name": s.name, "kind": s.kind, "mode": MODE_LABELS[s.kind],
                        "targets": s.targets, "value": s.value, "display": self._display_value(s),
                        "enabled": s.enabled,
                    }
                    for i, s in enumerate(swaps)
                ],
                "active_count": sum(1 for s in store.active_swaps() if s.enabled),
                "settings": {
                    "restore_on_stop": store.restore_on_stop,
                    "warn_id_swaps": store.warn_id_swaps,
                    "launch_at_login": store.launch_at_login,
                    "close_to_tray": store.close_to_tray,
                },
                "tray_available": self.tray_available,
                "startup_supported": startup.supported(),
                "notice_accepted": store.notice_accepted,
                "paths": {"home": str(self.home), "configs": str(store.configs_dir)},
                "rivals": {"name": games.RIVALS_NAME, "place_id": games.RIVALS_PLACE_ID},
                "can_undo": bool(store._undo),
                "fleasion_dir": str(presets.fleasion_configs_dir() or ""),
                "cache": {"path": str(self.runner.store.directory), "found": self.runner.store.exists()},
                "asset_states": {str(k): v for k, v in self.runner.swapper.state.items()},
                "captures": [{"name": c.name, "enabled": c.enabled, "files": len(c.pairs)}
                             for c in self.captures.items],
                "capture_stage": self._cap["stage"],
                "seq": self._seq,
            }

    def activity_log(self) -> list[dict]:
        return list(self.activity)

    def detect(self, replace_text: str) -> dict:
        """Live hint for the editor: what kind of replacement the text looks like."""
        text = replace_text.strip()
        if not text:
            return {"kind": None}
        try:
            swap = swap_from_inputs("", "1", text, self.store.configs_dir)
        except ValueError as exc:
            return {"kind": None, "error": str(exc)}
        return {"kind": swap.kind, "mode": MODE_LABELS[swap.kind], "describe": swap.describe()}

    # ----- saving -----------------------------------------------------------------

    def _save(self, profile: str | None = None) -> None:
        try:
            if profile:
                self.store.save_profile(profile)
            self.store.save_settings()
        except OSError as exc:
            raise UserError(f"Couldn't save: {exc}") from exc
        finally:
            self.runner.update_config(self.store.to_config())
            self._changed()
        if self.runner.running:
            self.log("Saved. The change applies to Roblox's cache within a few seconds; it shows the next time "
                     "Roblox loads that asset (usually your next match).")

    # ----- profiles -----------------------------------------------------------------

    def select_profile(self, name: str) -> None:
        with self._lock:
            if name not in self.store.profiles:
                raise UserError(f'No profile called "{name}".')
            self.store.editing = name
            self.store.save_settings()
        self._changed()

    def set_profile_enabled(self, name: str, on: bool) -> None:
        with self._lock:
            self.store.checkpoint()
            self.store.set_enabled(name, bool(on))
            self._save()

    def new_profile(self, name: str) -> str:
        with self._lock:
            self.store.checkpoint()
            try:
                created = self.store.create(name)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            self._save()
            return created

    def duplicate_profile(self) -> str:
        with self._lock:
            self.store.checkpoint()
            created = self.store.duplicate(self.store.editing)
            self._save()
            return created

    def rename_profile(self, new: str) -> str:
        with self._lock:
            self.store.checkpoint()
            try:
                renamed = self.store.rename(self.store.editing, new)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            self._save()
            return renamed

    def delete_profile(self) -> None:
        with self._lock:
            self.store.checkpoint()
            self.store.delete(self.store.editing)
            self._save()

    # ----- swaps ------------------------------------------------------------------

    def _form_swap(self, name: str, ids: str, replace: str, enabled: bool) -> Swap:
        try:
            return swap_from_inputs(name, ids, replace, self.store.configs_dir, enabled)
        except ValueError as exc:
            raise UserError(str(exc)) from exc

    def _needs_id_confirm(self, swap: Swap, confirmed: bool) -> bool:
        return (swap.kind == KIND_ASSET and self.store.warn_id_swaps and not self.id_warning_shown
                and not confirmed)

    def save_swap(self, name: str, ids: str, replace: str, index: int | None = None,
                  confirmed: bool = False) -> dict:
        """Add a swap (index None) or replace swap ``index``. May ask to confirm an ID swap first."""
        with self._lock:
            swaps = self.store.profiles[self.store.editing]
            enabled = swaps[index].enabled if index is not None and 0 <= index < len(swaps) else True
            swap = self._form_swap(name, ids, replace, enabled)
            if self._needs_id_confirm(swap, confirmed):
                return {"confirm": "id_swap"}
            if swap.kind == KIND_ASSET:
                self.id_warning_shown = True
            self.store.checkpoint()
            if index is None:
                swaps.append(swap)
                index = len(swaps) - 1
            else:
                if not 0 <= index < len(swaps):
                    raise UserError("That swap no longer exists.")
                swaps[index] = swap
            self._save(self.store.editing)
            return {"index": index}

    def set_swaps_enabled(self, indexes: list[int], on: bool) -> None:
        with self._lock:
            swaps = self.store.profiles[self.store.editing]
            self.store.checkpoint()
            for i in indexes:
                if 0 <= i < len(swaps):
                    swaps[i].enabled = bool(on)
            self._save(self.store.editing)

    def remove_swaps(self, indexes: list[int]) -> int:
        with self._lock:
            swaps = self.store.profiles[self.store.editing]
            valid = sorted({i for i in indexes if 0 <= i < len(swaps)}, reverse=True)
            if not valid:
                return 0
            self.store.checkpoint()
            for i in valid:
                del swaps[i]
            self._save(self.store.editing)
            self.log(f"Removed {len(valid)} swap(s). Ctrl+Z brings them back.")
            return len(valid)

    def undo(self) -> bool:
        with self._lock:
            done = self.store.undo()
            if done:
                self.runner.update_config(self.store.to_config())
                self._changed()
                self.log("Undid the last change.")
            return done

    # ----- presets ----------------------------------------------------------------

    def pick_file(self, kind: str, save: bool = False, default_name: str = "") -> str | None:
        if self.file_dialog is None:
            raise UserError("File dialogs aren't available here.")
        return self.file_dialog(kind, save, default_name)

    def import_preset(self, path: str, confirmed: bool = False) -> dict:
        try:
            result = presets.load_preset(Path(path))
        except (ValueError, OSError) as exc:
            raise UserError(str(exc)) from exc
        if not result.swaps:
            raise UserError("No usable swaps in this file." + (
                "\n\n" + "\n".join(result.skipped[:15]) if result.skipped else ""))
        hosts = result.link_hosts()
        if hosts and not confirmed:
            links = sum(1 for s in result.swaps if s.kind == KIND_URL)
            return {"confirm": "links", "format": result.format, "count": len(result.swaps),
                    "links": links, "hosts": hosts}
        with self._lock:
            self.store.checkpoint()
            name = self.store.create(Path(path).stem, result.swaps)
            self.store.set_enabled(name, True)
            self._save()
        self.log(f'Imported {len(result.swaps)} swap(s) from {Path(path).name} as profile "{name}" (on).')
        for reason in result.skipped:
            self.log(f"Skipped {reason}", "warn")
        for missing in result.missing_files:
            self.log(f"File not found on this PC (swap kept, it won't apply until the file exists): {missing}", "warn")
        return {"name": name, "count": len(result.swaps), "skipped": result.skipped,
                "missing": result.missing_files}

    def import_folder(self, path: str = "", confirmed: bool = False) -> dict:
        """Import a Fleasion configs folder (the rewrite's configs/): one profile per config file,
        switched on the way Fleasion has them. Without a path, Fleasion's own folder on this PC."""
        folder = Path(path) if path else presets.fleasion_configs_dir()
        if folder is None or not folder.is_dir():
            raise UserError("Fleasion's configs folder wasn't found on this PC. Pick the folder instead.")
        try:
            configs, fleasion_on = presets.load_folder(folder)
        except OSError as exc:
            raise UserError(f"Couldn't read {folder}: {exc}") from exc
        usable = [(n, r) for n, r in configs if r.swaps]
        if not usable:
            skipped = [s for _, r in configs for s in r.skipped]
            raise UserError(f"No usable configs in {folder}." + ("\n\n" + "\n".join(skipped[:15]) if skipped else ""))
        hosts = sorted({h for _, r in usable for h in r.link_hosts()})
        total = sum(len(r.swaps) for _, r in usable)
        if hosts and not confirmed:
            links = sum(1 for _, r in usable for s in r.swaps if s.kind == KIND_URL)
            return {"confirm": "links", "format": "Fleasion", "count": total, "configs": len(usable),
                    "links": links, "hosts": hosts}
        names, skipped, missing = [], [], []
        with self._lock:
            self.store.checkpoint()
            for config_name, result in usable:
                name = self.store.create(config_name, result.swaps)
                self.store.set_enabled(name, config_name in fleasion_on if fleasion_on else True)
                names.append(name)
                skipped += [f"{config_name}: {x}" for x in result.skipped]
                missing += [f"{config_name}: {x}" for x in result.missing_files]
            self._save()
        skipped += [f"{n}: {x}" for n, r in configs if not r.swaps for x in (r.skipped or ["no usable rules"])]
        self.log(f"Imported {len(names)} Fleasion config(s) from {folder} ({total} swaps): " + ", ".join(names) + ".")
        for reason in skipped:
            self.log(f"Skipped {reason}", "warn")
        for m in missing:
            self.log(f"File not found on this PC (swap kept, it won't apply until the file exists): {m}", "warn")
        return {"names": names, "count": total, "skipped": skipped, "missing": missing}

    def export_preset(self, fmt: str, path: str) -> dict:
        with self._lock:
            swaps = list(self.store.profiles[self.store.editing])
        skipped: list[str] = []
        if fmt == "fleasion":
            text, skipped = presets.export_fleasion(swaps)
        else:
            text = presets.export_patchwork(swaps)
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            raise UserError(f"Couldn't save: {exc}") from exc
        self.log(f"Exported {self.store.editing} to {path}.")
        return {"skipped": skipped}

    # ----- the cache engine ----------------------------------------------------------

    def _remove_old_setup(self) -> None:
        """Versions before 2.0 changed Roblox's certificate list and a client setting; undo that once."""
        try:
            report = roblox.unpatch_all()
            if report.patched:
                self.log(f"Removed the certificate setup older Patchwork versions added to {len(report.patched)} "
                         "Roblox folder(s). It isn't needed any more.")
        except Exception:
            pass

    def start(self, close_roblox: bool | None = None) -> dict | None:
        """Start watching Roblox's cache. (close_roblox is accepted for compatibility and ignored.)"""
        with self._lock:
            if self.busy or self.runner.running:
                return None
            if not any(s.enabled for s in self.store.active_swaps()):
                self.log("No swaps are switched on yet: turn on a profile first.", "warn")
            self.runner.update_config(self.store.to_config())
            self.runner.start()
            if not self.runner.store.exists():
                self.log(f"Roblox's cache isn't at {self.runner.store.directory} yet. Open Roblox once and "
                         "Patchwork picks it up.", "warn")
            self.log(f"Patchwork is running. Play {games.RIVALS_NAME}: swaps apply as Roblox downloads assets.")
            self._last_summary = None
            self._schedule_summary()
        self._changed()
        return None

    def _summarise(self) -> None:
        """Every so often, say how the swaps are doing."""
        st = self.runner.swapper.stats
        now = (st.get("swapped", 0), st.get("waiting", 0), st.get("unavailable", 0))
        if now != self._last_summary:
            self._last_summary = now
            swapped, waiting, unavailable = now
            parts = [f"{swapped} asset(s) swapped in Roblox's cache"]
            if waiting:
                parts.append(f"{waiting} waiting for Roblox to download them (they appear once you see them in game)")
            if unavailable:
                parts.append(f"{unavailable} can't be found (see the messages above)")
            self.log("; ".join(parts) + ".", "stats")
        self._changed()

    def stop(self) -> None:
        with self._lock:
            if self.busy or not self.runner.running:
                return
            self.busy = True
        self._changed()

        def work() -> None:
            self.runner.stop()
            self.busy = False
            self.log("Patchwork stopped.")
            if self.store.restore_on_stop:
                self.restore_cache()
            self._changed()

        threading.Thread(target=work, daemon=True).start()

    def _schedule_summary(self) -> None:
        def tick() -> None:
            if self.runner.running:
                try:
                    self._summarise()
                except Exception:
                    pass
                self._schedule_summary()

        self._summary_timer = threading.Timer(SUMMARY_SECONDS, tick)
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def restore_cache(self) -> int:
        """Put every original asset back in Roblox's cache."""
        try:
            restored = self.runner.restore_all()
        except Exception as exc:
            self.log(f"Couldn't put Roblox's original assets back: {exc}", "warn")
            return 0
        if restored:
            self.log(f"Put {restored} original asset(s) back in Roblox's cache.")
        if self.runner.running:
            self.runner.update_config(self.store.to_config())
            self._push_captures()
        self._changed()
        return restored

    def _game_changed(self) -> None:
        state = self.watcher.state
        learned = sorted(state.rivals_universes)
        if learned != sorted(self.store.rivals_universes):
            self.store.rivals_universes = learned
            self.store.save_settings()
        now = (state.in_game, state.in_rivals)
        if now != self._last_game:
            self._last_game = now
            suffix = ""
            self.log(f"Roblox: {state.describe()}{suffix}", "game")
        self._changed()

    # ----- maintenance --------------------------------------------------------------

    def clear_cache(self, close_roblox: bool | None = None) -> dict | None:
        """Clear Roblox's cache but keep the entries holding swaps.

        Roblox finds those again straight away, so swaps show instantly after a clear
        instead of waiting for a re-download. Works with Roblox open. ``close_roblox`` is
        only used when the cache index can't be found and the old full wipe is needed.
        """
        if self.runner.store.exists():
            try:
                removed, kept, in_use = self.runner.swapper.clear_keeping_swaps()
            except Exception as exc:
                log.exception("Clearing the cache failed")
                raise UserError(f"Couldn't clear Roblox's cache: {exc}\n\nTry again in a moment.") from exc
            roblox.clear_cache(roblox.temp_cache_paths())  # Roblox's older download folder, best effort
            msg = f"Cleared Roblox's cache ({removed} item(s))"
            msg += f" and kept your {kept} swapped asset(s), so they load straight away." if kept else "."
            if in_use:
                msg += f" {in_use} file(s) Roblox has open right now were left; they're cleared next time."
            self.log(msg)
            return {"removed": removed, "kept": kept}
        return self._wipe_cache(close_roblox)

    def _wipe_cache(self, close_roblox: bool | None) -> dict | None:
        """Delete Roblox's whole cache (only when its index isn't where Patchwork expects it)."""
        running = roblox.running_clients()
        if running and not close_roblox:
            return {"confirm": "roblox_open", "running": running}
        if running:
            roblox.close_clients()
            self.log("Closed Roblox to clear its cache.")
        # Patchwork keeps the cache index open while it watches it; let go while clearing.
        with self.runner.swapper._lock:
            self.runner.swapper.close()
            removed, failed = roblox.clear_cache()
        if failed:
            holders = roblox.lockers([Path(p) for p, _ in failed])
            who = ", ".join(f"{name} (PID {pid})" for pid, name in holders) or "another program"
            self.log(f"Couldn't clear all of Roblox's cache: it's in use by {who}. Close that program "
                     "(check the system tray and Task Manager), then press Clear Cache again.", "warn")
            raise UserError(f"Roblox's cache is in use by {who}.\n\nClose that program (check the system tray "
                            "and Task Manager), then press Clear Cache again. Until the cache is cleared, "
                            "Roblox keeps loading the assets it saved earlier.")
        self.log(f"Cleared Roblox's asset cache ({len(removed)} item(s))." if removed
                 else "Roblox's asset cache was already empty.")
        return None

    # ----- skin capture -------------------------------------------------------------

    def _push_captures(self) -> None:
        self.runner.swapper.set_captures(self.captures.active(), self.captures.body)

    def capture_start(self, which: str, clear: bool = False) -> None:
        """Start noting which cache files Roblox loads, while the player shows a skin in game."""
        if which not in ("original", "replacement"):
            raise UserError("Unknown capture step.")
        if not self.runner.store.exists():
            raise UserError("Roblox's cache wasn't found. Open RIVALS first.")
        if clear:
            # Only when asked: a skin Roblox already holds in memory isn't downloaded again after a clear,
            # so clearing can hide the very files being looked for.
            self.runner.swapper.clear_keeping_swaps()
        with self._lock:
            self._cap["mark"] = self.runner.swapper.capture_mark()
            self._cap["stage"] = which
            self._cap[which] = []
        self._changed()

    def capture_stop(self) -> list[dict]:
        """Stop noting and return the files Roblox loaded meanwhile."""
        with self._lock:
            which, mark = self._cap["stage"], self._cap["mark"]
            if which is None:
                raise UserError("Nothing is being captured.")
            self._cap["stage"] = None
        seen = self.runner.swapper.capture_seen(mark or {})
        if which == "replacement":
            # Keep the replacement files now: Roblox may drop them from its cache later.
            bodies = {}
            for s in seen:
                body = self.runner.swapper.read_entry(s.hex)
                if body is not None:
                    bodies[s.hex] = body
            seen = [s for s in seen if s.hex in bodies]
            self._cap["bodies"] = bodies
        with self._lock:
            self._cap[which] = seen
        self._changed()
        return self.capture_state()

    def capture_state(self) -> dict:
        def rows(items):
            return [{"hex": s.hex, "kind": s.kind, "group": capture.GROUPS.get(s.kind, s.kind), "size": s.size,
                     "label": s.label, "preview": s.preview} for s in items]
        with self._lock:
            return {
                "stage": self._cap["stage"],
                "original": rows(self._cap["original"]),
                "replacement": rows(self._cap["replacement"]),
                "guess": capture.auto_pairs(self._cap["original"], self._cap["replacement"]),
            }

    def capture_save(self, name: str, pairs: dict) -> None:
        """Save a captured swap: each original file (hex) is replaced by a captured replacement file."""
        with self._lock:
            original = {s.hex: s for s in self._cap["original"]}
            replacement = {s.hex: s for s in self._cap["replacement"]}
            bodies = self._cap["bodies"]
            saved = []
            for target, source in (pairs or {}).items():
                if not source:
                    continue
                o, r = original.get(target), replacement.get(source)
                if o is None or r is None or source not in bodies:
                    raise UserError("That capture is out of date. Capture both skins again.")
                if o.kind != r.kind:
                    raise UserError(f"A {capture.GROUPS.get(o.kind, o.kind)} can only be replaced by the same kind "
                                    "of file.")
                saved.append(capture.Pair(target, self.captures.keep_body(bodies[source]), o.kind))
            if not saved:
                raise UserError("Pick at least one replacement file.")
            item = capture.Capture(name, saved)
            self.captures.add(item)
            self._cap = {"stage": None, "mark": None, "original": [], "replacement": [], "bodies": {}}
        self._push_captures()
        self.log(f"Saved captured skin {item.name} ({len(saved)} file(s)). It applies within a second; rejoin "
                 "or re-equip to see it.")
        self._changed()

    def capture_toggle(self, name: str, enabled: bool) -> None:
        self.captures.set_enabled(name, enabled)
        self._push_captures()
        self._changed()

    def capture_delete(self, name: str) -> None:
        self.captures.remove(name)
        self._push_captures()  # its files are put back on the next check
        self.log(f"Deleted captured skin {name}; the original files are put back.")
        self._changed()

    # ----- cache browser ------------------------------------------------------------

    def cache_watch(self, on: bool = True) -> dict:
        """Start (or stop) watching: files Roblox downloads or reads from now on are marked."""
        with self._lock:
            self._cb_mark = self.runner.swapper.capture_mark() if on else None
        return {"watching": self._cb_mark is not None}

    def cache_browse(self) -> dict:
        """What Roblox has cached, newest first, and which files Patchwork replaces."""
        rows = self.runner.swapper.browse()
        mark = getattr(self, "_cb_mark", None)
        replaced = self.captures.targets()
        manifest = self.runner.swapper.backups.manifest
        for r in rows:
            r["group"] = r.get("label") or capture.GROUPS.get(r["kind"], r["kind"])
            r["fresh"] = mark is not None and mark.get(bytes.fromhex(r["hex"])) != (r["bytes"], r["atime"], r["hits"])
            r["replaced_by"] = replaced.get(r["hex"], "")
            rec = manifest.get(r["hex"])
            if not r["replaced_by"] and rec and rec.get("asset_id"):
                r["replaced_by"] = f"swap of {rec['asset_id']}"
        return {"found": self.runner.store.exists(), "rows": rows[:3000], "total": len(rows),
                "watching": mark is not None, "fresh": sum(1 for r in rows if r["fresh"])}

    def cache_preview(self, hex: str) -> dict:
        return self.runner.swapper.preview_entry(str(hex))

    def cache_mesh(self, hex: str) -> dict:
        return self.runner.swapper.mesh_entry(str(hex))

    def cache_replace(self, hex: str, path: str = "") -> str | None:
        """Replace one cached file with a file the player picks. Kept until restored."""
        if not path:
            path = self.pick_file("replacement")
            if not path:
                return None
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise UserError(f"Couldn't read {path}: {exc}") from exc
        from .swapper import Unavailable
        try:
            body = self.runner.swapper.replacement_for(hex, data)
            original = self.runner.swapper.read_entry(hex) or b""
            cache.check_write(original, None, 10, body)
        except (Unavailable, cache.Refused) as exc:
            raise UserError(f"Can't use that file: {exc}.") from exc
        kind = cache.sniff(original)
        name = f"{Path(path).stem} → {capture.GROUPS.get(kind, kind)} {hex[:6]}"
        self.captures.drop_target(hex)
        self.captures.add(capture.Capture(name, [capture.Pair(hex, self.captures.keep_body(body), kind)]))
        self._push_captures()
        self.log(f"Replacing a cached {capture.GROUPS.get(kind, kind).lower()} with {Path(path).name}. It shows the "
                 "next time Roblox loads it (rejoin or re-equip).")
        self._changed()
        return name

    def cache_restore(self, hex: str) -> None:
        names = self.captures.drop_target(str(hex))
        self._push_captures()
        if names:
            self.log("Putting back the original of a cached file (" + ", ".join(names) + ").")
        self._changed()

    def cache_export(self, hex: str) -> str | None:
        body = self.runner.swapper.read_entry(str(hex))
        if body is None:
            raise UserError("Roblox no longer has that file in its cache.")
        kind = cache.sniff(body)
        path = self.pick_file("export", save=True, default_name=f"{hex[:12]}{capture.EXTENSIONS.get(kind, '.bin')}")
        if not path:
            return None
        Path(path).write_bytes(body)
        self.log(f"Saved a copy of a cached {capture.GROUPS.get(kind, kind).lower()} to {path}.")
        return path

    def cache_report(self) -> str:
        """Write a list of what's in Roblox's cache (kinds, sizes, addresses; nothing personal)."""
        lines = self.runner.swapper.report()
        path = self.home / "patchwork-cache-report.txt"
        self.home.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log(f"Saved a cache report to {path}. Send that file to help add texture support.")
        try:
            self.open_folder("home")
        except Exception:
            pass
        return str(path)

    def clear_downloads(self) -> None:
        shutil.rmtree(self.home / "downloads", ignore_errors=True)
        self.log("Deleted downloaded replacement files. Patchwork downloads them again when needed.")

    def set_setting(self, key: str, value: bool) -> None:
        if key not in ("restore_on_stop", "warn_id_swaps", "launch_at_login", "close_to_tray"):
            raise UserError(f"Unknown setting {key}")
        setattr(self.store, key, bool(value))
        self.store.save_settings()
        if key == "launch_at_login":
            self.apply_launch_at_login(announce=True)
        self._changed()

    def apply_launch_at_login(self, announce: bool = False) -> None:
        """Register or remove the start-with-Windows task to match the setting."""
        if not startup.supported():
            return
        if self.store.launch_at_login:
            error = startup.enable()
            if error:
                self.log(f"Couldn't set Patchwork to start with Windows: {error}", "warn")
            elif announce:
                self.log("Patchwork will start with Windows (as administrator, without a prompt).")
        else:
            error = startup.disable()
            if error:
                self.log(f"Couldn't stop Patchwork starting with Windows: {error}", "warn")
            elif announce:
                self.log("Patchwork won't start with Windows any more.")

    def auto_start(self) -> None:
        """Start swapping as soon as the window is up (there is no Start button)."""
        if not self.store.notice_accepted or self.runner.running or self.busy:
            return
        try:
            self.start(close_roblox=False)
        except UserError as exc:
            self.log(str(exc), "warn")
            self._emit("error", {"title": "Patchwork couldn't start", "text": str(exc)})

    def accept_notice(self) -> None:
        self.store.notice_accepted = True
        self.store.save_settings()
        self._changed()
        self.apply_launch_at_login()
        self.auto_start()

    def open_folder(self, which: str) -> None:
        path = self.store.configs_dir if which == "configs" else self.home
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def shutdown(self) -> None:
        if self.runner.running:
            self.runner.stop()
        if self.store.restore_on_stop:
            try:
                self.runner.restore_all()
            except Exception:
                pass
        if self._summary_timer:
            self._summary_timer.cancel()
        self.watcher.stop()
