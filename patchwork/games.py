"""Know which Roblox experience the player is in, so swaps apply only in RIVALS.

Roblox writes a log for every session to %LOCALAPPDATA%\\Roblox\\logs. When you
join a game it logs a line like::

    [FLog::Output] ! Joining game '<job id>' place 17625359962 at 1.2.3.4

and, a moment later, the experience (universe) the place belongs to::

    [FLog::GameJoinLoadTime] Report game_join_loadtime: placeid:..., universeid:..., userid:...

Patchwork tails the newest log the same way launchers such as Bloxstrap do.
RIVALS' universe ID is learned from the first RIVALS join, so places inside the
RIVALS experience (teleports, match servers) count as RIVALS too.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

RIVALS_NAME = "RIVALS"
RIVALS_PLACE_ID = 17625359962

_JOINING = re.compile(r"! Joining game '([0-9a-f\-]{36})' place ([0-9]+)")
_UNIVERSE = re.compile(r"game_join_loadtime:.*?placeid:([0-9]+).*?universeid:([0-9]+)")
_UNIVERSE_ONLY = re.compile(r"game_join_loadtime:.*?universeid:([0-9]+)")
_LEAVING = ("[FLog::SingleSurfaceApp] leaveUGCGameInternal", "[FLog::Network] Time to disconnect replication data:")


@dataclass
class GameState:
    place_id: int | None = None
    universe_id: int | None = None
    rivals_universes: set[int] = field(default_factory=set)

    @property
    def in_game(self) -> bool:
        return self.place_id is not None

    @property
    def in_rivals(self) -> bool:
        if self.place_id == RIVALS_PLACE_ID:
            return True
        return self.universe_id is not None and self.universe_id in self.rivals_universes

    def feed(self, line: str) -> bool:
        """Update from one log line. Returns True if the state changed."""
        before = (self.place_id, self.universe_id, frozenset(self.rivals_universes))
        if match := _JOINING.search(line):
            self.place_id = int(match.group(2))
            self.universe_id = None
        elif match := _UNIVERSE.search(line):
            place, universe = int(match.group(1)), int(match.group(2))
            if self.place_id is None:
                self.place_id = place
            self.universe_id = universe
            if place == RIVALS_PLACE_ID:
                self.rivals_universes.add(universe)
        elif match := _UNIVERSE_ONLY.search(line):
            self.universe_id = int(match.group(1))
            if self.place_id == RIVALS_PLACE_ID:
                self.rivals_universes.add(self.universe_id)
        elif any(marker in line for marker in _LEAVING):
            self.place_id = None
            self.universe_id = None
        return before != (self.place_id, self.universe_id, frozenset(self.rivals_universes))

    def describe(self) -> str:
        if not self.in_game:
            return "not in a game"
        if self.in_rivals:
            return RIVALS_NAME
        return f"another game (place {self.place_id})"


def log_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "Roblox" / "logs" if local else None


def newest_player_log(directory: Path) -> Path | None:
    try:
        logs = [p for p in directory.glob("*.log") if "studio" not in p.name.lower()]
    except OSError:
        return None
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


class GameWatcher:
    """Follows Roblox's newest log on a background thread."""

    def __init__(
        self,
        directory: Path | None,
        on_change: Callable[[GameState], None],
        rivals_universes: set[int] | None = None,
        interval: float = 0.25,
    ):
        self.directory = directory
        self.on_change = on_change
        self.interval = interval
        self.state = GameState(rivals_universes=set(rivals_universes or ()))
        self._file: Path | None = None
        self._pos = 0
        self._partial = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="patchwork-games", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                pass
            self._stop.wait(self.interval)

    def poll(self) -> None:
        """Read whatever Roblox has written since the last poll (also used by tests)."""
        if self.directory is None or not self.directory.is_dir():
            return
        newest = newest_player_log(self.directory)
        if newest is None:
            return
        if newest != self._file:
            # A new Roblox session: start over from the top of its log.
            self._file, self._pos, self._partial = newest, 0, ""
            if self.state.in_game:
                self.state.place_id = self.state.universe_id = None
                self.on_change(self.state)
        try:
            with open(self._file, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        lines = (self._partial + chunk).split("\n")
        self._partial = lines.pop()
        changed = False
        for line in lines:
            changed |= self.state.feed(line)
        if changed:
            self.on_change(self.state)
