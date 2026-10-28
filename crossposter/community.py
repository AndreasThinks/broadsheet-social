"""The community directory, from the app's side: browse and add shared feeds, share your own, and the opt-in
anonymous weekly stats.

What leaves this device, and when:
- Adding or updating a community feed fetches its file from the directory (that counts one download).
- Sharing a feed sends that file, its name and description, an optional credit line and a random receipt.
- Only if you said yes to anonymous stats: once a week, counts for the community feeds you have (see build_report).
  Nothing is counted at all while stats are off.
No handles, account details, posts or feeds you wrote yourself are ever sent.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import httpx

from . import feedcheck
from .config import settings

log = logging.getLogger(__name__)

INSTALLED = "community_installed"     # {local slug: {"remote", "version", "installed_at"}}
CHOICE = "stats_choice"               # None until asked, then "yes" or "no"
COUNTS = "stats_counts"               # {week: {remote slug: {"visits", "posts_read", "acted"}}}
RECEIPTS = "community_receipts"       # [{"receipt", "local", "name", "sent_at"}]
SENT = "stats_last_sent"              # the report most recently sent, shown in Settings

_client: httpx.AsyncClient | None = None
_list_cache: tuple[float, list] | None = None


class DirectoryError(Exception):
    """The directory couldn't be reached or refused the request; the message is safe to show."""


def http() -> httpx.AsyncClient:
    # A client of its own: no cookies, and a user agent that says nothing about you.
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "Broadsheet"})
    return _client


async def _call(method: str, path: str, json=None) -> dict:
    try:
        r = await http().request(method, settings.directory_url + path, json=json)
    except httpx.HTTPError as e:
        raise DirectoryError(f"Couldn't reach the community directory ({type(e).__name__}).")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 404:
        raise DirectoryError("That feed isn't in the community directory any more.")
    if r.status_code >= 400:
        raise DirectoryError(body.get("error") or f"The community directory said {r.status_code}.")
    return body


def page_url(slug: str) -> str:
    return f"{settings.directory_url}/feeds/{slug}"


# ---------- browsing and adding ----------

async def list_feeds(fresh: bool = False) -> list[dict]:
    global _list_cache
    if _list_cache and not fresh and time.time() - _list_cache[0] < 600:
        return _list_cache[1]
    feeds = (await _call("GET", "/api/feeds")).get("feeds") or []
    _list_cache = (time.time(), feeds)
    return feeds


async def get_feed(slug: str) -> dict:
    return await _call("GET", f"/api/feeds/{slug}")


def installed(db) -> dict:
    return db.get(INSTALLED, {})


def local_for(db, remote: str) -> str | None:
    """The file name a community feed was added under, if it's still in your feeds folder."""
    for local, rec in installed(db).items():
        if rec["remote"] == remote and (settings.feeds_dir / f"{local}.py").is_file():
            return local
    return None


def _local_name(remote: str) -> str:
    base = "".join(c if c.isalnum() else "_" for c in remote.lower()).strip("_")[:50] or "community_feed"
    name, n = base, 2
    while (settings.feeds_dir / f"{name}.py").exists():
        name, n = f"{base}_{n}", n + 1
    return name


def _same_file(source: str) -> str | None:
    """A feed file you already have with exactly this code (say a starter feed), so adding it doesn't make a second tab."""
    d = settings.feeds_dir
    for path in sorted(d.glob("*.py")) if d.exists() else []:
        if not path.name.startswith("_") and path.read_text() == source:
            return path.stem
    return None


def same_file_named(f: dict) -> str | None:
    """For the Community list: a feed file you have with the same name and code as a listed feed (a starter, say)."""
    path = settings.feeds_dir / f"{f['slug'].replace('-', '_')}.py"
    candidates = [path] + sorted(settings.feeds_dir.glob("*.py")) if settings.feeds_dir.exists() else []
    for p in candidates:
        if p.is_file() and not p.name.startswith("_") and f'name="{f["name"]}"' in p.read_text():
            return p.stem
    return None


async def add(db, remote: str) -> str:
    """Download a community feed into your feeds folder (or replace the copy you have) and return its file name."""
    data = await _call("POST", f"/api/feeds/{remote}/download")
    source = data.get("source") or ""
    if not feedcheck.check(source).ok:
        raise DirectoryError("That feed didn't pass the checks on this device, so it wasn't added.")
    local = local_for(db, remote) or _same_file(source) or _local_name(remote)
    settings.feeds_dir.mkdir(parents=True, exist_ok=True)
    (settings.feeds_dir / f"{local}.py").write_text(source)
    (settings.feeds_dir / f"_removed_{local}.py").unlink(missing_ok=True)
    recs = installed(db)
    prev = recs.get(local) or {}
    recs[local] = {"remote": remote, "version": data.get("version", 1),
                   "installed_at": prev.get("installed_at") or time.time()}
    db.set(INSTALLED, recs)
    global _list_cache
    _list_cache = None
    return local


# ---------- anonymous weekly stats ----------

def choice(db) -> str | None:
    return db.get(CHOICE)


def set_choice(db, value: str) -> None:
    db.set(CHOICE, "yes" if value == "yes" else "no")
    if value != "yes":
        db.set(COUNTS, {})          # stopping means nothing counted so far is sent either


def week_of(t: float | None = None) -> str:
    y, w, _ = datetime.fromtimestamp(t or time.time(), timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


_shown: OrderedDict[str, str] = OrderedDict()   # post key -> remote slug of the community feed it was shown in


def _remote(db, local: str) -> str | None:
    rec = installed(db).get(local)
    return rec["remote"] if rec else None


def _bump(db, remote: str, field: str, by: int = 1) -> None:
    counts = db.get(COUNTS, {})
    wk = counts.setdefault(week_of(), {}).setdefault(remote, {"visits": 0, "posts_read": 0, "acted": 0})
    wk[field] = wk.get(field, 0) + by
    db.set(COUNTS, counts)


def record_shown(db, local: str, post_keys: list[str], first_page: bool) -> None:
    """Called when a page of a feed is shown. Counts only community feeds, and only if stats are on."""
    if choice(db) != "yes":
        return
    remote = _remote(db, local)
    if not remote:
        return
    if first_page:
        _bump(db, remote, "visits")
    if post_keys:
        _bump(db, remote, "posts_read", len(post_keys))
    for k in post_keys:
        _shown[k] = remote
        _shown.move_to_end(k)
    while len(_shown) > 3000:
        _shown.popitem(last=False)


def record_action(db, post_key: str) -> None:
    """A like, repost or reply on a post that was shown in a community feed."""
    remote = _shown.get(post_key)
    if remote and choice(db) == "yes":
        _bump(db, remote, "acted")


def _installed_for(installed_at: float, at: float) -> str:
    days = (at - installed_at) / 86400
    return "0-7d" if days < 8 else "8-30d" if days < 30 else "30d+"


def build_report(db, week: str, positions: dict[str, int], at: float | None = None) -> dict:
    """The exact report for a week. positions maps each local feed file name to its place among visible Home tabs."""
    at = at or time.time()
    counts = (db.get(COUNTS, {}) or {}).get(week, {})
    items, seen = [], set()
    for local, rec in sorted(installed(db).items()):
        remote = rec["remote"]
        if remote in seen or not (settings.feeds_dir / f"{local}.py").is_file():
            continue
        seen.add(remote)
        c = counts.get(remote, {})
        items.append({"feed": remote, "on_home": local in positions,
                      "installed_for": _installed_for(rec.get("installed_at") or at, at),
                      "visits": c.get("visits", 0), "posts_read": c.get("posts_read", 0),
                      "posts_liked_or_replied": min(c.get("acted", 0), c.get("posts_read", 0)),
                      "tab_position": positions.get(local, 0)})
    return {"week": week, "feeds": items}


async def send_due(db, positions: dict[str, int]) -> bool:
    """Send last week's report if stats are on and it hasn't gone yet. Returns True if one was sent."""
    if choice(db) != "yes":
        return False
    this_week = week_of()
    counts = db.get(COUNTS, {})
    last_week = week_of(time.time() - 7 * 86400)
    if db.get("stats_sent_week") == last_week:
        return False
    report = build_report(db, last_week, positions)
    if report["feeds"]:
        try:
            await _call("POST", "/api/report", report)
        except DirectoryError as e:
            log.info("stats report not sent: %s", e)
            return False
        db.set(SENT, {"at": time.time(), "report": report})
    db.set("stats_sent_week", last_week)
    db.set(COUNTS, {w: v for w, v in counts.items() if w == this_week})
    return bool(report["feeds"])


async def stats_loop(db, positions_fn) -> None:
    """Background task: once an hour, send last week's report if it's due. A random start spreads apps out."""
    await asyncio.sleep(60 + secrets.randbelow(600))
    while True:
        try:
            if choice(db) == "yes":
                await send_due(db, await positions_fn())
        except Exception:
            log.exception("stats report failed")
        await asyncio.sleep(3600)


# ---------- sharing ----------

def receipts(db) -> list[dict]:
    return db.get(RECEIPTS, [])


def receipt_for(db, local: str) -> dict | None:
    return next((r for r in receipts(db) if r["local"] == local), None)


async def share(db, local: str, name: str, description: str, credit: str, source: str, note: str = "",
                remote: str = "") -> dict:
    """Send a feed for review. One receipt per feed file, so later updates are recognised as yours."""
    rec = receipt_for(db, local)
    if rec is None:
        rec = {"receipt": secrets.token_urlsafe(32), "local": local, "name": name, "sent_at": time.time()}
        db.set(RECEIPTS, receipts(db) + [rec])
    body = {"receipt": rec["receipt"], "name": name, "description": description, "credit": credit, "source": source,
            "note": note}
    if remote:
        body["slug"] = remote
    out = await _call("POST", "/api/submissions", body)
    rec.update(name=name, sent_at=time.time())
    db.set(RECEIPTS, [rec if r["local"] == local else r for r in receipts(db)])
    return out


async def my_submissions(db) -> list[dict]:
    rs = receipts(db)
    if not rs:
        return []
    return (await _call("POST", "/api/submissions/status", {"receipts": [r["receipt"] for r in rs]})).get("submissions") or []


async def listed_slug_for(db, local: str) -> str:
    """If a feed you shared has been listed, its slug in the directory (so a new share is an update)."""
    rec = receipt_for(db, local)
    if not rec:
        return ""
    try:
        subs = (await _call("POST", "/api/submissions/status", {"receipts": [rec["receipt"]]})).get("submissions") or []
    except DirectoryError:
        return ""
    return next((s["slug"] for s in subs if s["status"] == "approved" and s["slug"]), "")


async def withdraw(db, sid: int) -> bool:
    for r in receipts(db):
        try:
            if (await _call("POST", f"/api/submissions/{sid}/withdraw", {"receipt": r["receipt"]})).get("ok"):
                return True
        except DirectoryError:
            continue
    return False
