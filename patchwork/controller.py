"""Everything the window does, with no UI code: profiles, swaps, start/stop, safety.

The web interface (server.py + webui/) calls these methods and redraws from
``snapshot()``. Events (state changes, activity lines) are queued with a
sequence number so the page can long-poll for them.
"""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, games, presets, roblox, startup
from .config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap, app_dir, swap_from_inputs
from .profiles import Store
from .rules import Hit
from .swapper import CacheRunner

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
        self.log(f"Patchwork swaps {games.RIVALS_NAME} assets in Roblox's cache on this PC. It works on its own: add "
                 "swaps, switch a profile on, and play. A swap shows up once Roblox has downloaded that asset and "
                 "loads it again (usually your next match).")
        for warning in self.store.warnings:
            self.log(warning, "warn")
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
                "cache": {"path": str(self.runner.store.directory), "found": self.runner.store.exists()},
                "asset_states": {str(k): v for k, v in self.runner.swapper.state.items()},
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
        """Delete Roblox's asset cache.

        Windows locks the cache while Roblox is open, so if it's running this
        returns {"confirm": "roblox_open"} first; call again with
        close_roblox=True to close Roblox and clear in one go.
        """
        running = roblox.running_clients()
        if running and not close_roblox:
            return {"confirm": "roblox_open", "running": running}
        if running:
            roblox.close_clients()
            self.log("Closed Roblox to clear its cache.")
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
