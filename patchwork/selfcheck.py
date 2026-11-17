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

    def gui() -> str:
        import tkinter

        from . import gui as _gui  # noqa: F401

        icon = Path(__file__).with_name("assets") / "patchwork.png"
        if not icon.exists():
            raise RuntimeError(f"missing {icon}")
        return f"Tk {tkinter.TkVersion}"

    check("mitmproxy local mode", local_mode)
    check("Windows redirector", redirector)
    check("certificate authority", certificate)
    check("swap addon", addons)
    check("GUI", gui)

    lines.append("PASS" if ok else "FAIL")
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
