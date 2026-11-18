"""Open Patchwork's window.

The interface is a local web page (patchwork/webui) shown in a native window
through pywebview, which uses Microsoft Edge WebView2 on Windows (built into
Windows 10 and 11). If no web view is available, Patchwork opens the page in
the default browser instead.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

from . import __version__, games
from .controller import Controller
from .server import UiServer

TITLE = f"Patchwork for {games.RIVALS_NAME}"


def _tk_file_dialog(kind: str, save: bool, default_name: str) -> str | None:
    """Fallback file picker when there is no native window."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    types = _dialog_types(kind)
    try:
        if save:
            return filedialog.asksaveasfilename(defaultextension=".json", initialfile=default_name,
                                                filetypes=types) or None
        return filedialog.askopenfilename(filetypes=types) or None
    finally:
        root.destroy()


def _dialog_types(kind: str) -> list[tuple[str, str]]:
    if kind == "preset":
        return [("Preset (JSON)", "*.json"), ("All files", "*.*")]
    return [("Roblox-ready files", "*.png *.jpg *.jpeg *.webp *.ogg *.mp3 *.wav *.mesh *.rbxm *.rbxmx"),
            ("All files", "*.*")]


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    # PATCHWORK_PROXY_MODE (e.g. "regular@8080") is for development on non-Windows machines.
    controller = Controller(proxy_mode=os.environ.get("PATCHWORK_PROXY_MODE") or None)
    server = UiServer(controller, port=int(os.environ.get("PATCHWORK_PORT", "0")))
    server.start()

    use_browser = "--browser" in argv
    webview = None
    if not use_browser:
        try:
            import webview  # pywebview
        except ImportError:
            use_browser = True

    if use_browser:
        done = threading.Event()
        server.on_quit = done.set
        controller.file_dialog = _tk_file_dialog
        print(f"Patchwork is open in your browser: {server.url}")
        webbrowser.open(server.url)
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
        controller.shutdown()
        server.stop()
        return

    window = webview.create_window(
        TITLE, server.url, width=1240, height=820, min_size=(980, 640), background_color="#07070b",
        text_select=False,
    )

    def native_dialog(kind: str, save: bool, default_name: str) -> str | None:
        types = tuple(f"{label} ({pattern.replace(' ', ';')})" for label, pattern in _dialog_types(kind))
        if save:
            result = window.create_file_dialog(webview.SAVE_DIALOG, save_filename=default_name, file_types=types)
        else:
            result = window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    controller.file_dialog = native_dialog
    server.on_quit = window.destroy
    window.events.closed += lambda: (controller.shutdown(), server.stop())
    webview.start(private_mode=True)
