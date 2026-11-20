# PyInstaller spec for the single-file Windows build.
# Build from the repo root with:  pyinstaller packaging/patchwork.spec --noconfirm
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(ROOT, "patchwork", "__main__.py")],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, "patchwork", "assets"), os.path.join("patchwork", "assets")),
        (os.path.join(ROOT, "patchwork", "webui"), os.path.join("patchwork", "webui")),
    ],
    hiddenimports=["patchwork.app", "patchwork.selfcheck", "webview.platforms.edgechromium", "pystray._win32", "PIL.DdsImagePlugin", "PIL.PngImagePlugin", "zstandard"],
    excludes=["webview.platforms.qt", "webview.platforms.gtk", "tkinter.test"],
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
    # Always run as administrator (needed to capture Roblox's traffic). When Patchwork starts with
    # Windows it runs from a Task Scheduler task with highest privileges, so there is no UAC prompt.
    uac_admin=True,
    upx=False,
    version=os.path.join(SPECPATH, "version_info.txt"),
)
