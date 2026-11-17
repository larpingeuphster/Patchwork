# PyInstaller spec for the single-file Windows build.
# Build from the repo root with:  pyinstaller packaging/patchwork.spec --noconfirm
# mitmproxy and mitmproxy_rs ship their own PyInstaller hooks (including the
# Windows redirector helper), so nothing extra is listed for them here.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "patchwork", "__main__.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "patchwork", "assets"), os.path.join("patchwork", "assets"))],
    hiddenimports=["patchwork.gui", "patchwork.selfcheck"],
    excludes=["mitmproxy.tools.console", "mitmproxy.tools.web", "urwid"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Patchwork",
    icon=os.path.join(SPECPATH, "patchwork.ico"),
    console=False,
    upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
)
