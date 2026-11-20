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

    def cache_engine() -> str:
        from . import cache

        if cache.checksum(b"roblox-assettypeid: 1\r\n") != 0xAF46D420:
            raise RuntimeError("cache checksum key is wrong")
        import zstandard  # noqa: F401
        from PIL import Image  # noqa: F401

        from .swapper import CacheSwapper  # noqa: F401

        return "checksum key ok"

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

    def tray_icon() -> str:
        if sys.platform != "win32":
            return "skipped (not Windows)"
        from . import tray

        if not tray.available():
            raise RuntimeError("pystray or Pillow missing from the build")
        from PIL import Image

        Image.open(tray.ICON_PATH).load()
        import pystray._win32  # noqa: F401

        return "pystray + icon"

    def textures() -> str:
        from PIL import Image

        from . import ktx2
        img = Image.new("RGBA", (16, 16), (200, 30, 60, 255))
        for fmt in ("BC1", "BC3"):
            tex = ktx2.parse(ktx2.make(img, fmt, levels=5, zstd=True))
            if ktx2.distance(ktx2.signature(ktx2.decode(tex)), ktx2.signature(img)) > 6:
                raise RuntimeError(f"{fmt} round trip changed the picture")
        return "KTX2 BC1/BC3 + zstd"

    check("cache engine", cache_engine)
    check("textures", textures)
    check("interface", ui)
    check("native window", window)
    check("tray icon", tray_icon)

    lines.append("PASS" if ok else "FAIL")
    Path(report_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1
