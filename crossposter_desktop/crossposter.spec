# PyInstaller spec for the desktop app.  Build from the repo root:
#     python -m crossposter_desktop.release info --out build/app-info/build.json
#     pyinstaller crossposter_desktop/crossposter.spec --noconfirm
#
# The app code (crossposter/, static/, feeds/) is shipped as plain files under app/ rather than compiled
# into the executable, so the launcher can swap in newer code downloaded from GitHub (see updater.py).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
NAME = "Broadsheet"

datas, binaries, hidden = [], [], []
for pkg in ("fasthtml", "fastcore", "uvicorn", "starlette", "httpx", "joserfc", "pywebpush", "py_vapid",
            "bs4", "PIL", "cryptography", "webview"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hidden += h

# Feeds you write in the app, and future app code, may import any standard-library module.
SKIP_STDLIB = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv", "pydoc_data",
               "antigravity", "this", "_tkinter"}
hidden += [m for m in sys.stdlib_module_names if m not in SKIP_STDLIB and not m.startswith("_")]
hidden += collect_submodules("crossposter")  # analysed for their imports, then dropped from the archive below

datas += [(str(ROOT / part), f"app/{part}") for part in ("crossposter", "static", "feeds")]
datas += [(str(ROOT / "build" / "app-info" / "build.json"), "app")]

a = Analysis([str(ROOT / "crossposter_desktop" / "entry.py")], pathex=[str(ROOT)], datas=datas,
             binaries=binaries, hiddenimports=hidden, excludes=["tkinter", "test"], noarchive=False)
a.pure = [m for m in a.pure if not (m[0] == "crossposter" or m[0].startswith("crossposter."))]
pyz = PYZ(a.pure)

icon = str(ROOT / "static" / "icons" / "icon-512.png")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(coll, name=f"{NAME}.app", icon=icon, bundle_identifier="com.andreasthinks.crossposter",
                 info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
