"""Keep Patchwork running in the Windows system tray.

Closing the window hides it instead of quitting, so swaps keep working; the
tray icon reopens it, clears Roblox's cache, or quits. When Patchwork starts
with Windows it goes straight to the tray.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("patchwork")

ICON_PATH = Path(__file__).with_name("assets") / "patchwork.png"


def available() -> bool:
    """The tray needs Windows and the pystray package (bundled in Patchwork.exe)."""
    if sys.platform != "win32":
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


class CloseToTray:
    """Decides what closing the window does. Kept free of GUI code so it can be tested."""

    def __init__(
        self,
        hide: Callable[[], None],
        enabled: Callable[[], bool],
        on_first_hide: Callable[[], None] | None = None,
    ):
        self.hide = hide
        self.enabled = enabled
        self.on_first_hide = on_first_hide or (lambda: None)
        self.quitting = False
        self._hidden_once = False

    def on_closing(self) -> bool:
        """Window close requested. Return False to keep the app running (hidden)."""
        if self.quitting or not self.enabled():
            return True
        self.hide()
        if not self._hidden_once:
            self._hidden_once = True
            self.on_first_hide()
        return False


class Tray:
    """The tray icon and its menu (Windows only)."""

    def __init__(self, title: str, on_open: Callable[[], None], on_clear_cache: Callable[[], None],
                 on_quit: Callable[[], None]):
        import pystray
        from PIL import Image

        self._pystray = pystray
        image = Image.open(ICON_PATH)
        menu = pystray.Menu(
            pystray.MenuItem("Open Patchwork", lambda: on_open(), default=True),
            pystray.MenuItem("Clear Roblox cache", lambda: on_clear_cache()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Patchwork", lambda: on_quit()),
        )
        self.icon = pystray.Icon("patchwork", image, title, menu)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="patchwork-tray", daemon=True)
        self._thread.start()

    def notify(self, message: str, title: str = "Patchwork") -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            log.debug("tray notification failed", exc_info=True)

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
