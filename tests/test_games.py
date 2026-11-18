import os
import time

from patchwork import games
from patchwork.games import RIVALS_PLACE_ID, GameState, GameWatcher

JOIN_RIVALS = (f"2026-11-18T10:00:00.000Z,1.0,abc,6 [FLog::Output] ! Joining game "
               f"'0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0' place {RIVALS_PLACE_ID} at 10.0.0.1")
LOADTIME = ("2026-11-18T10:00:05.000Z,5.0,abc,6 [FLog::GameJoinLoadTime] Report game_join_loadtime: "
            f"placeid:{RIVALS_PLACE_ID}, visitid:0, universeid:6035872082, userid:1")
JOIN_OTHER = ("2026-11-18T10:10:00.000Z,1.0,abc,6 [FLog::Output] ! Joining game "
              "'11111111-2222-3333-4444-555555555555' place 920587237 at 10.0.0.2")
JOIN_SUBPLACE = ("[FLog::Output] ! Joining game '11111111-2222-3333-4444-666666666666' place 555 at 10.0.0.3")
SUBPLACE_LOADTIME = "[FLog::GameJoinLoadTime] Report game_join_loadtime: placeid:555, universeid:6035872082, userid:1"
LEAVE = "[FLog::SingleSurfaceApp] leaveUGCGameInternal"


def test_state_machine():
    s = GameState()
    assert not s.in_game and not s.in_rivals and s.describe() == "not in a game"
    assert s.feed(JOIN_RIVALS)
    assert s.in_rivals and s.describe() == "RIVALS"
    assert s.feed(LOADTIME)
    assert s.rivals_universes == {6035872082}
    assert s.feed(LEAVE) and not s.in_game
    s.feed(JOIN_OTHER)
    assert s.in_game and not s.in_rivals and "920587237" in s.describe()
    # A different place in the same experience (a teleport) counts once the universe is known.
    s.feed(JOIN_SUBPLACE)
    assert not s.in_rivals
    s.feed(SUBPLACE_LOADTIME)
    assert s.in_rivals
    assert not s.feed("unrelated line")


def test_watcher_follows_newest_log(tmp_path):
    changes = []
    w = GameWatcher(tmp_path, changes.append)
    w.poll()  # no logs yet
    old = tmp_path / "0.700_20261118T100000Z_Player_A_last.log"
    old.write_text(JOIN_RIVALS + "\n")
    w.poll()
    assert w.state.in_rivals and len(changes) == 1

    with open(old, "a") as fh:
        fh.write(LEAVE[:20])  # a half-written line is held back
    w.poll()
    assert w.state.in_rivals
    with open(old, "a") as fh:
        fh.write(LEAVE[20:] + "\n")
    w.poll()
    assert not w.state.in_game

    new = tmp_path / "0.700_20261118T110000Z_Player_B_last.log"
    new.write_text(JOIN_OTHER + "\n")
    t = time.time() + 5
    os.utime(new, (t, t))
    w.poll()
    assert w.state.in_game and not w.state.in_rivals


def test_watcher_ignores_studio_logs(tmp_path):
    (tmp_path / "x_Studio_last.log").write_text(JOIN_RIVALS + "\n")
    w = GameWatcher(tmp_path, lambda s: None)
    w.poll()
    assert not w.state.in_game


def test_watcher_without_log_dir():
    w = GameWatcher(None, lambda s: None)
    w.poll()
    assert not w.state.in_game
    assert games.log_dir() is None or isinstance(games.log_dir(), os.PathLike)


def test_stale_log_is_ignored_when_roblox_is_closed(tmp_path):
    (tmp_path / "x_Player_last.log").write_text(JOIN_RIVALS + "\n")
    alive = [True]
    changes = []
    w = GameWatcher(tmp_path, changes.append, is_running=lambda: alive[0])
    w.poll()
    assert w.state.in_rivals
    alive[0] = False
    w._last_alive_check = 0
    w.poll()
    assert not w.state.in_game and len(changes) == 2
