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
from .proxy import ProxyRunner, ca_dir, ensure_ca
from .rules import Hit

REPATCH_SECONDS = 15
MODE_LABELS = {KIND_ASSET: "ID", KIND_URL: "Link", KIND_FILE: "File", KIND_REMOVE: "Remove"}


class UserError(Exception):
    """A problem to show the user as-is."""


@dataclass
class Event:
    seq: int
    kind: str
    data: object


class Controller:
    def __init__(self, home: Path | None = None, log_dir: Path | None = None, proxy_mode: str | None = None):
        self.home = home or app_dir()
        self.store = Store.load(self.home)
        self._lock = threading.RLock()
        self._events: collections.deque[Event] = collections.deque(maxlen=500)
        self._seq = 0
        self._cond = threading.Condition()
        self.activity: collections.deque[dict] = collections.deque(maxlen=400)
        self.busy = False
        self.ca_pem: str | None = None
        self.id_warning_shown = False
        self._last_game: tuple[bool, bool] | None = None
        self._repatch_timer: threading.Timer | None = None
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
        self.runner = ProxyRunner(
            self.store.to_config(),
            self.home,
            on_hit=self._hit,
            on_log=lambda level, msg: self.log(msg, "warn" if level in ("WARNING", "ERROR", "CRITICAL") else ""),
            mode=proxy_mode,
            allow=self._allow_swaps,
        )
        self.log(f"Patchwork swaps {games.RIVALS_NAME} assets on this PC only, and only while you're in "
                 f"{games.RIVALS_NAME}. It starts swapping on its own: add swaps, switch a profile on, and open Roblox.")
        for warning in self.store.warnings:
            self.log(warning, "warn")
        self.watcher.start()
        if sys.platform == "win32":
            threading.Thread(target=self._discover_loop, name="patchwork-discover", daemon=True).start()

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
        self.log(f"Swapped {hit.asset_id} → {hit.replacement}  ({hit.swap_name})", "hit")

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
        if state.in_rivals:
            return {"tone": "on", "short": f"In {rivals}", "text": f"In {rivals} · swaps on"}
        if state.in_game:
            return {"tone": "paused", "short": "Paused", "text": f"Not {rivals} · swaps paused"}
        return {"tone": "waiting", "short": "Waiting", "text": f"Waiting for {rivals}"}

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
                "installs": [i.label for i in roblox.find_installs()] + [
                    f"{m.parent.name}/Modifications" for m in roblox.modifications_dirs()],
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
            self.log("Saved. Rejoin the game (Clear Cache first if the asset already loaded) to see changes.")

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

    # ----- proxy --------------------------------------------------------------------

    def _patch_roblox(self, quiet: bool) -> None:
        if self.ca_pem is None:
            self.ca_pem = ensure_ca(self.home)
        report = roblox.patch_all(self.ca_pem)
        for label in report.patched:
            self.log(f"Set up {label} to trust Patchwork.")
        for label, reason in report.failed:
            if not quiet:
                self.log(f"Couldn't update {label}: {reason}", "warn")
        if not quiet and report.trusted_count == 0:
            self.log("No Roblox install found. Install Roblox, then reopen Patchwork "
                     "(the Microsoft Store version isn't supported).", "warn")

    def start(self, close_roblox: bool | None = None) -> dict | None:
        """Start in the background; progress arrives as events.

        Roblox only picks up Patchwork's certificate when it launches, so if it's
        already open this returns {"confirm": "roblox_open"} first. Call again
        with close_roblox=True to close it, or False to start anyway.
        """
        with self._lock:
            if self.busy or self.runner.running:
                return None
            open_now = roblox.running_clients(tuple(self.store.processes))
            if open_now and close_roblox is None:
                return {"confirm": "roblox_open", "running": open_now}
            if open_now and close_roblox:
                roblox.close_clients(tuple(self.store.processes))
                self.log("Closed Roblox. Open it again after Patchwork starts.")
            if not any(s.enabled for s in self.store.active_swaps()):
                self.log("No swaps are switched on yet: turn on a profile first.", "warn")
            try:
                self._patch_roblox(quiet=False)
            except Exception as exc:
                raise UserError(f"Couldn't prepare Roblox: {exc}") from exc
            self.runner.update_config(self.store.to_config())
            running = roblox.running_clients(tuple(self.store.processes))
            if not running:
                removed, failed = roblox.clear_cache()
                if failed:
                    holders = roblox.lockers([Path(p) for p, _ in failed])
                    who = ", ".join(f"{name} (PID {pid})" for pid, name in holders) or "another program"
                    self.log(f"Couldn't clear Roblox's cache: it's in use by {who}. Close it and press Clear Cache, "
                             "or Roblox will keep loading the assets it saved earlier.", "warn")
                elif removed:
                    self.log("Cleared Roblox's asset cache so swaps load fresh.")
            self.busy = True
        self._changed()

        def work() -> None:
            try:
                self.runner.start()
            except Exception as exc:
                self.busy = False
                self.log(f"Couldn't start: {exc}", "warn")
                self._emit("error", {"title": "Patchwork couldn't start", "text": str(exc)})
                self._changed()
                return
            self.busy = False
            self.log(f"Patchwork is running. Open Roblox and join {games.RIVALS_NAME}.")
            if running:
                self.log("Roblox was already open, so it isn't using Patchwork yet. Press Clear Cache (it closes "
                         "Roblox for you), then open Roblox again.", "warn")
            self._started_at = time.monotonic()
            self._last_summary = None
            self._warned_quiet = False
            self._schedule_repatch()
            self._changed()

        threading.Thread(target=work, daemon=True).start()
        return None

    def _summarise(self) -> None:
        """Every so often, say what Patchwork has seen from Roblox, and flag likely problems."""
        st = self.runner.addon.stats
        now = (st.trusted, st.rejected, st.lookups, st.assets, st.swapped, st.paused, st.upstream_failed,
               st.downloads, st.silent_closes)
        if now != self._last_summary and (st.lookups or st.rejected or st.upstream_failed or st.silent_closes):
            self._last_summary = now
            parts = [f"{st.assets} asset(s) asked for in {st.lookups} request(s)", f"{st.swapped} swapped",
                     f"{st.downloads} download(s) from Roblox's CDN"]
            if st.paused:
                parts.append(f"{st.paused} request(s) outside {games.RIVALS_NAME}")
            if st.rejected:
                parts.append(f"{st.rejected} certificate refusal(s)")
            if st.upstream_failed:
                parts.append(f"{st.upstream_failed} failed connection(s) to Roblox's server")
            if st.silent_closes:
                parts.append(f"{st.silent_closes} connection(s) Roblox closed without a request")
            if st.protocols:
                parts.append("protocol " + "/".join(sorted(st.protocols)))
            self.log("Seen from Roblox so far: " + ", ".join(parts) + ".", "stats")
        quiet_for = time.monotonic() - getattr(self, "_started_at", time.monotonic())
        if (not self._warned_quiet and quiet_for > 90 and self.watcher.state.in_rivals
                and st.lookups == 0 and st.rejected == 0 and st.silent_closes == 0):
            self._warned_quiet = True
            self.log(f"You're in {games.RIVALS_NAME} but Patchwork hasn't seen any of Roblox's asset requests yet. "
                     "Roblox probably isn't going through Patchwork: close Roblox completely (check Task Manager "
                     "for RobloxPlayerBeta.exe), then open it again while Patchwork is running.", "warn")

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
                self.restore_roblox()
            else:
                self.log("Roblox now loads its normal assets (Clear Cache to drop swapped ones it saved).")
            self._changed()

        threading.Thread(target=work, daemon=True).start()

    def _schedule_repatch(self) -> None:
        # Roblox installs updates into a new folder, so keep trusting new versions.
        def tick() -> None:
            if self.runner.running:
                try:
                    self._patch_roblox(quiet=True)
                except Exception:
                    pass
                try:
                    self._summarise()
                except Exception:
                    pass
                self._schedule_repatch()

        self._repatch_timer = threading.Timer(REPATCH_SECONDS, tick)
        self._repatch_timer.daemon = True
        self._repatch_timer.start()

    def restore_roblox(self) -> None:
        """Undo Patchwork's changes to Roblox and drop swapped assets from its cache."""
        report = roblox.unpatch_all()
        for label, reason in report.failed:
            self.log(f"Couldn't restore {label}: {reason}", "warn")
        if report.patched:
            self.log(f"Removed Patchwork's certificate and setting from {len(report.patched)} Roblox folder(s).")
        if roblox.running_clients():
            self.log("Roblox is still open, so its cache wasn't cleared. Close Roblox and press Clear Cache "
                     "to drop the swapped assets it saved.", "warn")
        else:
            roblox.clear_cache()
            self.log("Cleared Roblox's asset cache. Roblox is back to normal.")

    def _discover_loop(self) -> None:
        while True:
            try:
                self.discover_installs()
            except Exception:
                pass
            time.sleep(8)

    def discover_installs(self) -> list[str]:
        """Remember any Roblox install that's running from a folder Patchwork didn't know about.

        Launchers such as Froststrap can keep Roblox somewhere unusual. Returns
        the folders added.
        """
        known = {r.resolve() for r in roblox.version_roots()}
        added = []
        for exe in roblox.client_paths(tuple(self.store.processes)):
            root = roblox.root_for_exe(exe)
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in known or str(root) in self.store.install_roots:
                continue
            self.store.install_roots.append(str(root))
            added.append(str(root))
            known.add(resolved)
        if added:
            self.store.save_settings()
            roblox.set_extra_roots([Path(r) for r in self.store.install_roots])
            running = self.runner.running
            for root in added:
                if running:
                    self.log(f"Found Roblox running from {root}, which Patchwork hadn't set up before (a launcher "
                             "such as Froststrap). It's set up now: close Roblox completely and open it again "
                             "for swaps to work.", "warn")
                else:
                    self.log(f"Found Roblox running from {root} (a launcher such as Froststrap). Patchwork will "
                             "set it up the next time it starts.")
            if running:
                try:
                    self._patch_roblox(quiet=True)
                except Exception:
                    pass
            self._changed()
        return added

    def _allow_swaps(self, header_place: int | None) -> bool:
        """Called by the proxy for each asset request: swap only inside RIVALS."""
        if header_place == games.RIVALS_PLACE_ID:
            return True
        return self.watcher.state.in_rivals

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
            if self.runner.running and state.in_game:
                suffix = " (swaps on)" if state.in_rivals else " (swaps paused)"
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

    def remove_certificate(self) -> None:
        if self.runner.running:
            self.runner.stop()
        report = roblox.unpatch_all()
        for label in report.patched:
            self.log(f"Removed Patchwork's certificate and setting from {label}.")
        for label, reason in report.failed:
            self.log(f"Couldn't update {label}: {reason}", "warn")
        shutil.rmtree(ca_dir(self.home), ignore_errors=True)
        self.ca_pem = None
        self.log("Deleted Patchwork's certificate from this PC.")
        self._changed()

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
                    self.restore_roblox()
                except Exception:
                    pass
        if self._repatch_timer:
            self._repatch_timer.cancel()
        self.watcher.stop()
