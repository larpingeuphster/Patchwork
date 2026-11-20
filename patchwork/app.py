"""Open Patchwork's window.

The interface is a local web page (patchwork/webui) shown in a native window
through pywebview, which uses Microsoft Edge WebView2 on Windows (built into
Windows 10 and 11). If no web view is available, Patchwork opens the page in
the default browser instead.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from pathlib import Path
import threading
import webbrowser

from . import __version__, games, startup, tray
from .config import app_dir
from .controller import Controller
from .server import UiServer

TITLE = f"Patchwork for {games.RIVALS_NAME}"


def _instance_file():
    return app_dir() / "instance.json"


def _write_instance(server: UiServer) -> None:
    try:
        path = _instance_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"port": server.port, "token": server.token, "pid": os.getpid()}),
                        encoding="utf-8")
    except OSError:
        pass


def _clear_instance() -> None:
    try:
        data = json.loads(_instance_file().read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            _instance_file().unlink()
    except (OSError, ValueError):
        pass


def _show_running_instance() -> bool:
    """If another Patchwork is running, ask it to show its window. True if it answered."""
    try:
        data = json.loads(_instance_file().read_text(encoding="utf-8"))
        port, token = int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/api/show", body=b"{}", headers={
            "Host": f"127.0.0.1:{port}", "X-Patchwork-Token": token, "Content-Type": "application/json"})
        return conn.getresponse().status == 200
    except OSError:
        return False


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
    if kind == "export":
        return [("All files", "*.*")]
    return [("Roblox-ready files", "*.png *.jpg *.jpeg *.webp *.ktx2 *.ogg *.mp3 *.wav *.mesh *.obj *.rbxm *.rbxmx"),
            ("All files", "*.*")]


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    # Only one Patchwork at a time: if one is already running (for example it
    # started with Windows), bring its window forward and quit.
    if _show_running_instance():
        return

    # PATCHWORK_CACHE_DIR points at a Roblox cache folder elsewhere (for development).
    cache_dir = os.environ.get("PATCHWORK_CACHE_DIR")
    controller = Controller(cache_dir=Path(cache_dir) if cache_dir else None)
    server = UiServer(controller, port=int(os.environ.get("PATCHWORK_PORT", "0")))
    server.start()
    _write_instance(server)
    controller.apply_launch_at_login()
    # No Start button: start swapping right away (after the first-run notice is accepted).
    threading.Thread(target=controller.auto_start, daemon=True).start()

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
        _clear_instance()
        return

    background = startup.BACKGROUND_FLAG in argv
    use_tray = tray.available()
    window = webview.create_window(
        TITLE, server.url, width=1240, height=820, min_size=(980, 640), background_color="#07070b",
        text_select=False,
        # Started with Windows: go straight to the tray (or the taskbar if there's no tray).
        hidden=background and use_tray, minimized=background and not use_tray,
    )

    def show() -> None:
        try:
            window.show()
            window.restore()
            window.on_top = True
            window.on_top = False
        except Exception:
            pass

    server.on_show = show
    tray_icon: tray.Tray | None = None

    def quit_app() -> None:
        closer.quitting = True
        if tray_icon:
            tray_icon.stop()
        window.destroy()

    def clear_cache_from_tray() -> None:
        try:
            controller.clear_cache(close_roblox=True)
            if tray_icon:
                tray_icon.notify("Roblox's cache is clear. Your swaps were kept, so they load straight away.")
        except Exception as exc:
            if tray_icon:
                tray_icon.notify(f"Couldn't clear Roblox's cache: {exc}")

    def first_hide() -> None:
        controller.log("Patchwork is still running in the system tray, so swaps keep working. Right-click "
                       "its tray icon to open it again or quit.")
        if tray_icon:
            tray_icon.notify("Patchwork is still running here, so your swaps keep working. "
                             "Right-click the icon to quit.")

    closer = tray.CloseToTray(
        hide=window.hide,
        enabled=lambda: use_tray and controller.store.close_to_tray,
        on_first_hide=first_hide,
    )
    window.events.closing += closer.on_closing
    if use_tray:
        try:
            tray_icon = tray.Tray(TITLE, on_open=show, on_clear_cache=clear_cache_from_tray, on_quit=quit_app)
            tray_icon.start()
        except Exception:
            tray_icon = None
            use_tray = False
            if background:
                window.minimized = True
    controller.tray_available = use_tray

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
    server.on_quit = quit_app
    window.events.closed += lambda: (controller.shutdown(), server.stop(), _clear_instance(),
                                     tray_icon.stop() if tray_icon else None)
    webview.start(private_mode=True)
