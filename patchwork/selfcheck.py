"""``Patchwork.exe --self-check <report.txt>``: confirm a build has everything it needs.

Used by CI on the packaged .exe, and handy when someone reports a broken build.
It writes a report file because the windowed .exe has no console.
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path


def run(report_path: str) -> int:
    lines: list[str] = []
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"ok    {name}" + (f": {detail}" if detail else ""))
        except Exception:
            ok = False
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    def redirector() -> str:
        if sys.platform != "win32":
            return "skipped (not Windows)"
        import mitmproxy_windows

        pkg = Path(mitmproxy_windows.__file__).parent
        exes = sorted(p.name for p in pkg.rglob("*.exe"))
        if not exes:
            raise RuntimeError(f"no redirector executable in {pkg}")
        return ", ".join(exes)

    def local_mode() -> str:
        from mitmproxy.proxy.mode_specs import ProxyMode

        return ProxyMode.parse("local:RobloxPlayerBeta.exe").description

    def certificate() -> str:
        from .proxy import ensure_ca

        with tempfile.TemporaryDirectory() as tmp:
            pem = ensure_ca(Path(tmp))
        if "BEGIN CERTIFICATE" not in pem:
            raise RuntimeError("certificate authority PEM looks wrong")
        return "generated"

    def addons() -> str:
        from mitmproxy import addons as mitm_addons

        from .config import Config
        from .proxy import SwapAddon

        SwapAddon(Config())
        return f"{len(mitm_addons.default_addons())} mitmproxy addons"

    def ui() -> str:
        from . import app as _app  # noqa: F401
        from .server import API_METHODS, WEBUI_DIR

        for name in ("index.html", "style.css", "app.js"):
            if not (WEBUI_DIR / name).is_file():
                raise RuntimeError(f"missing {WEBUI_DIR / name}")
        from .controller import Controller

        missing = [m for m in API_METHODS if not callable(getattr(Controller, m, None))]
        if missing:
            raise RuntimeError(f"API methods missing from Controller: {missing}")
        return f"{len(API_METHODS)} API methods"

    def window() -> str:
        if sys.platform != "win32":
            return "skipped (not Windows)"
        import webview
        from webview.platforms import edgechromium  # noqa: F401  (needs pythonnet + WebView2 loader)

        return f"pywebview {getattr(webview, '__version__', '')}".strip()

    check("mitmproxy local mode", local_mode)
    check("Windows redirector", redirector)
    check("certificate authority", certificate)
    check("swap addon", addons)
    check("interface", ui)
    check("native window", window)

    lines.append("PASS" if ok else "FAIL")
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
