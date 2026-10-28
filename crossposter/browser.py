"""Running Broadsheet inside a web page, with no server holding anyone's data.

The browser build loads this package into Pyodide (Python compiled to WebAssembly) in a Web Worker. The page's
service worker hands every request from the app's frame to `handle`, which calls the ASGI app directly. Accounts,
tokens, cached posts and feeds live in /data, which the worker keeps in the browser's IndexedDB. Network calls go out
with the browser's own fetch, so only services that allow browsers (Bluesky, Mastodon) work; X doesn't.

Nothing here runs on a server: the home page only hands out these files.
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

# Headers the browser sets itself or refuses to let a page set; sending them would also fail CORS preflights.
_SKIP_REQUEST = {"host", "user-agent", "content-length", "connection", "accept-encoding", "transfer-encoding"}
# fetch has already undone any compression, so these would make httpx decode twice.
_SKIP_RESPONSE = {"content-encoding", "content-length", "transfer-encoding"}


class FetchTransport(httpx.AsyncBaseTransport):
    """httpx over the browser's fetch."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from js import AbortSignal, Object, fetch
        from pyodide.ffi import to_js

        body = await request.aread()
        init = {"method": request.method, "redirect": "follow",
                "headers": [[k, v] for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST]}
        if body:
            init["body"] = to_js(body)
        timeout = (request.extensions.get("timeout") or {}).get("read")
        if timeout:
            init["signal"] = AbortSignal.timeout(int(timeout * 1000))
        try:
            resp = await fetch(str(request.url), to_js(init, dict_converter=Object.fromEntries))
        except Exception as e:  # a CORS refusal or no connection both arrive as a TypeError
            raise httpx.ConnectError(f"Couldn't reach {request.url.host} from the browser ({e}).", request=request)
        data = (await resp.arrayBuffer()).to_bytes()
        headers = [(k, v) for k, v in (list(p) for p in resp.headers.entries()) if k.lower() not in _SKIP_RESPONSE]
        return httpx.Response(resp.status, headers=headers, content=data, request=request)


def _patch_for_pyodide() -> None:
    """Pyodide has no threads: run 'background thread' work inline, and send httpx through fetch."""
    import anyio.to_thread

    async def run_sync(func, *args, **_):
        return func(*args)

    async def to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    anyio.to_thread.run_sync = run_sync
    asyncio.to_thread = to_thread

    original = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("transport", FetchTransport())
        original(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = init


_app = None


def boot(base_url: str, data_dir: str = "/data") -> None:
    """Configure for the browser and import the app. Called once by the worker after /data is mounted."""
    global _app
    os.environ.update(BROWSER="1", BASE_URL=base_url, DATA_DIR=data_dir, FEEDS_DIR=f"{data_dir}/feeds",
                      X_ENABLED="false", DIRECTORY_URL=base_url.rsplit("/web", 1)[0])
    _patch_for_pyodide()
    from . import app as appmod
    appmod.seed_feeds()
    _app = appmod.app


async def handle(method: str, url: str, headers: list, body: bytes | None) -> tuple[int, list, bytes]:
    """One HTTP request from the page, answered by the ASGI app."""
    u = urlsplit(url)
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
        "method": method.upper(), "scheme": u.scheme or "https", "path": unquote(u.path or "/"),
        "raw_path": (u.path or "/").encode(), "query_string": u.query.encode(), "root_path": "",
        # The service worker can't see Host or Content-Length, and form parsing needs the length.
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers
                    if k.lower() not in ("host", "content-length")]
                   + [(b"host", u.netloc.encode()), (b"content-length", str(len(body or b"")).encode())],
        "server": (u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)), "client": ("127.0.0.1", 0),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body or b"", "more_body": False}
        await asyncio.Future()  # the page never disconnects mid-response; wait to be cancelled

    status, out_headers, chunks = 500, [], []

    async def send(message):
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await _app(scope, receive, send)
    return status, out_headers, b"".join(chunks)


# ---------- moving to another browser ----------

def export_zip(db, data_dir: Path) -> bytes:
    """Everything this browser holds, as one file: the database, the key its tokens are encrypted with, and feeds."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        tmp = data_dir / ".export.db"
        disk = sqlite3.connect(str(tmp))
        with db.lock:
            db.conn.backup(disk)
        disk.close()
        z.write(tmp, "crossposter.db")
        tmp.unlink()
        secret = data_dir / ".secret"
        if secret.exists():
            z.write(secret, ".secret")
        for f in sorted((data_dir / "feeds").glob("*.py")):
            z.write(f, f"feeds/{f.name}")
    return buf.getvalue()


def import_zip(db, data_dir: Path, blob: bytes) -> None:
    """Replace this browser's data with a backup made by export_zip."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        if "crossposter.db" not in names:
            raise ValueError("That isn't a Broadsheet backup.")
        with db.lock:
            db.conn.close()
            for name in names:
                if name == "crossposter.db" or name == ".secret" or (name.startswith("feeds/") and name.endswith(".py")
                                                                     and "/" not in name[6:] and ".." not in name):
                    target = data_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(name))
            db.__init__(data_dir / "crossposter.db", wal=False)


def erase(db, data_dir: Path) -> None:
    """Forget everything: accounts, tokens, cached posts and feeds. The app starts fresh."""
    with db.lock:
        db.conn.close()
        for p in data_dir.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        db.__init__(data_dir / "crossposter.db", wal=False)
