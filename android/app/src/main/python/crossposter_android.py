"""Runs Broadsheet inside the Android app: the same FastHTML server, on 127.0.0.1, shown in a WebView.

Called from Kotlin through Chaquopy:
- start(files_dir, port, notifier) when the app opens, returns the per-launch sign-in token.
- poll(files_dir, port, notifier) from the background worker, about every 15 minutes, to check for
  notifications while the app is closed.

`notifier` is a Kotlin object with notify(title, body, path, tag); new notifications go to it instead
of (as well as) Web Push, which a WebView can't receive.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import threading
import time

log = logging.getLogger("crossposter.android")
_server = None
_notifier = None
_lock = threading.Lock()


def _configure(files_dir: str, port: int) -> None:
    """Point the app at the phone's private storage. Must run before crossposter is imported."""
    if "crossposter.config" in sys.modules:
        return
    data = os.path.join(files_dir, "data")
    os.makedirs(data, exist_ok=True)
    os.environ.update({
        "DATA_DIR": data, "FEEDS_DIR": os.path.join(data, "feeds"), "PORT": str(port),
        "BASE_URL": f"http://127.0.0.1:{port}", "DESKTOP": "1",
        # Other apps on the phone can reach 127.0.0.1 too, so the app needs a password; the WebView
        # signs itself in with it at /login/desktop. A new one each time the process starts.
        "APP_PASSWORD": secrets.token_urlsafe(24),
    })
    sys.path.insert(0, os.path.join(files_dir, "app"))


def _hook_push() -> None:
    from crossposter import push
    if getattr(push.send, "_android", False):
        return
    web_send = push.send

    async def send(db, title, body, url, tag="", icon=""):
        sent = 0
        if _notifier is not None:
            try:
                _notifier.notify(title, body[:300], url, tag or url)
                sent = 1
            except Exception:
                log.exception("posting an Android notification failed")
        return sent + await web_send(db, title, body, url, tag=tag, icon=icon)

    send._android = True
    push.send = send


def start(files_dir: str, port: int, notifier=None) -> str:
    global _server, _notifier
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with _lock:
        _notifier = notifier or _notifier
        _configure(files_dir, port)
        if _server is None:
            import uvicorn
            from crossposter.app import app
            _hook_push()
            _server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
            threading.Thread(target=_server.run, name="server", daemon=True).start()
        deadline = time.monotonic() + 60
        while not _server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the app server didn't start")
            time.sleep(0.05)
        return os.environ["APP_PASSWORD"]


def poll(files_dir: str, port: int, notifier=None) -> int:
    """One notification check, for the background worker. If the server is running in this process its
    own poller is already doing this every minute, so there's nothing to add."""
    global _notifier
    with _lock:
        _notifier = notifier or _notifier
        if _server is not None:
            return 0
        _configure(files_dir, port)
    from crossposter import push
    from crossposter.app import db
    _hook_push()
    return asyncio.run(push.poll_once(db))
