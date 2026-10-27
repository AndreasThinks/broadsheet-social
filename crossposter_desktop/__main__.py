"""Broadsheet as a desktop app: the same FastHTML app, served on 127.0.0.1 and shown in a native window.

    python -m crossposter_desktop              # open the app
    python -m crossposter_desktop --headless   # just the server, for a browser or testing
    python -m crossposter_desktop --smoke      # start, check it answers, exit (used by CI)

Data lives in your user data folder (see data_dir), separate from any server deployment.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import updater

log = logging.getLogger("crossposter.desktop")
APP_NAME = "Broadsheet"
DATA_DIR_NAME = "Cross-poster"  # the app's old name; kept so existing installs find their data
DEFAULT_PORT = 47615
FROZEN = getattr(sys, "frozen", False)


def data_dir() -> Path:
    if override := os.environ.get("CROSSPOSTER_DESKTOP_DATA"):
        return Path(override)
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / DATA_DIR_NAME
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / DATA_DIR_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "cross-poster"


def bundled_dir() -> Path:
    """Where the app code that shipped with this build lives."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "app"
    return Path(__file__).resolve().parent.parent


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(data: Path, wanted: int | None) -> int:
    """Keep the same port between launches: sign-in callbacks registered with Mastodon include it."""
    prefs_path = data / "desktop.json"
    try:
        prefs = json.loads(prefs_path.read_text())
    except Exception:
        prefs = {}
    port = wanted or prefs.get("port", DEFAULT_PORT)
    if not _free(port):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    if prefs.get("port") != port:
        prefs["port"] = port
        prefs_path.write_text(json.dumps(prefs, indent=2))
    return port


def configure(data: Path, port: int) -> str:
    """Point the app at the desktop data folder. Must run before crossposter is imported."""
    token = secrets.token_urlsafe(24)
    os.environ.update({
        "DATA_DIR": str(data), "FEEDS_DIR": str(data / "feeds"), "PORT": str(port),
        "BASE_URL": f"http://127.0.0.1:{port}", "DESKTOP": "1",
        # A fresh password every launch, so other programs on this computer can't use the app;
        # the window signs itself in with it at /login/desktop.
        "APP_PASSWORD": token,
    })
    return token


def _forget_crossposter() -> None:
    for name in [n for n in sys.modules if n == "crossposter" or n.startswith("crossposter.")]:
        del sys.modules[name]


def load_app(code_dir: Path):
    sys.path.insert(0, str(code_dir))
    importlib.invalidate_caches()
    try:
        return importlib.import_module("crossposter.app").app
    except Exception:
        sys.path.remove(str(code_dir))
        _forget_crossposter()
        raise


def serve(app, port: int):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the app server didn't start")
        time.sleep(0.05)
    return server, thread


def start(data: Path, bundled: Path):
    """Load the newest good code and start it, falling back to the built-in code if an update breaks."""
    updates = data / "updates"
    while True:
        code_dir = updater.choose_code_dir(bundled, updates)
        try:
            app = load_app(code_dir)
            server, thread = serve(app, int(os.environ["PORT"]))
            return code_dir, server, thread
        except Exception:
            if code_dir == bundled:
                raise
            log.exception("downloaded code failed to start")
            updater.mark_bad(updates, code_dir)
            if str(code_dir) in sys.path:
                sys.path.remove(str(code_dir))
            _forget_crossposter()


def smoke(port: int, token: str) -> None:
    import httpx
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=True, timeout=30) as c:
        assert c.get("/healthz").json() == {"ok": True}
        home = c.get("/login/desktop", params={"token": token})
        assert home.status_code == 200 and "Broadsheet" in home.text, home.status_code
    print("smoke test passed")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Broadsheet desktop app")
    ap.add_argument("--headless", action="store_true", help="run the server without a window")
    ap.add_argument("--smoke", action="store_true", help="start, check the app answers, and exit")
    ap.add_argument("--port", type=int, help="port on 127.0.0.1 (default: the one used last time)")
    ap.add_argument("--no-update", action="store_true", help="don't check for newer app code")
    args = ap.parse_args(argv)
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        # A windowed build has no console; keep a log next to the data instead.
        sys.stdout = sys.stderr = open(data / "desktop.log", "a", buffering=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = pick_port(data, args.port)
    token = configure(data, port)
    bundled = bundled_dir()
    code_dir, server, thread = start(data, bundled)
    info = updater.read_info(code_dir)
    log.info("Broadsheet %s (build %s) on http://127.0.0.1:%s, data in %s",
             info.get("commit"), info.get("build"), port, data)

    if args.smoke:
        smoke(port, token)
        server.should_exit = True
        thread.join(10)
        return

    # Only installed builds update themselves; a source checkout updates with git pull.
    auto_update = FROZEN and not args.no_update and os.environ.get("CROSSPOSTER_NO_UPDATE") != "1"
    url = f"http://127.0.0.1:{port}/login/desktop?token={token}"

    if args.headless:
        print(f"Open {url}", flush=True)
        if auto_update:
            threading.Thread(target=updater.check, args=(bundled, data / "updates", code_dir), daemon=True).start()
        try:
            thread.join()
        except KeyboardInterrupt:
            server.should_exit = True
        return

    import webview  # pywebview; imported late so --headless works without a GUI toolkit

    def after_load(window):
        if not auto_update:
            return
        result = updater.check(bundled, data / "updates", code_dir)
        if result.status == "shell" and result.page:
            ask = getattr(window, "create_confirmation_dialog", None)
            if ask and ask("Update available", "A new version of Broadsheet is available to download. "
                                                "Open the download page?"):
                webbrowser.open(result.page)

    window = webview.create_window(APP_NAME, url, width=1180, height=820, min_size=(380, 520),
                                   text_select=True, zoomable=True)
    webview.start(after_load, (window,), private_mode=False, storage_path=str(data / "webview"))
    server.should_exit = True
    thread.join(10)


if __name__ == "__main__":
    main()
