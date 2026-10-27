"""A rolling local cache of posts. Custom feeds rank what is in here, and actions look posts up by key."""
from __future__ import annotations

import json
import logging
import re
import time

from .config import settings
from .models import Post

log = logging.getLogger(__name__)
FETCH_EVERY = 300  # seconds between background refreshes of one source


def save_posts(db, posts: list[Post], source: str | None = None) -> None:
    now = time.time()
    rows = []
    for p in posts:
        existing = db.one("select sources from posts where key=?", (p.key,))
        sources = set(json.loads(existing["sources"])) if existing else set()
        if source:
            sources.add(source)
        rows.append((p.key, p.account_id, p.network, p.id, p.created_at, now, json.dumps(sorted(sources)),
                     json.dumps(p.to_dict())))
    db.xmany("""insert into posts(key, account_id, network, post_id, created_at, fetched_at, sources, data)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(key) do update set fetched_at=excluded.fetched_at, sources=excluded.sources,
                data=excluded.data""", rows)


def get_post(db, key: str) -> Post | None:
    row = db.one("select data from posts where key=?", (key,))
    return Post.from_dict(json.loads(row["data"])) if row else None


def update_post(db, post: Post) -> None:
    db.x("update posts set data=? where key=?", (json.dumps(post.to_dict()), post.key))


def candidates(db, sources: list[str], window_hours: float) -> list[Post]:
    """Posts from any of the given sources, newest first, within the window."""
    since = time.time() - window_hours * 3600
    wanted = set(sources)
    out = []
    for row in db.q("select sources, data from posts where created_at > ? order by created_at desc", (since,)):
        if wanted & set(json.loads(row["sources"])):
            out.append(Post.from_dict(json.loads(row["data"])))
    return out


def prune(db) -> None:
    db.x("delete from posts where fetched_at < ?", (time.time() - settings.cache_hours * 3600,))
    db.x("delete from notifications where created_at < ?", (time.time() - 30 * 86400,))


async def refresh(db, adapters, sources: list[str], force: bool = False, pages: int = 2) -> list[str]:
    """Fetch each source for every account on that network. Returns any errors."""
    errors = []
    for src in sources:
        network = src.split(":", 1)[0]
        for a in adapters:
            if a.network != network:
                continue
            if a.network == "x":
                from .adapters.x import reading_enabled
                if not reading_enabled(db):
                    continue
            stamp_key = f"fetched:{a.account_id}:{src}"
            if not force and time.time() - (db.get(stamp_key) or 0) < FETCH_EVERY:
                continue
            cursor = None
            try:
                for _ in range(1 if a.network == "x" else pages):
                    page = await a.timeline(src, cursor, limit=50 if a.network != "x" else 20)
                    save_posts(db, page.posts, src)
                    cursor = page.cursor
                    if not cursor:
                        break
                db.set(stamp_key, time.time())
            except Exception as e:  # one broken source shouldn't stop the others
                log.warning("refresh %s %s failed: %s", a.account_id, src, e)
                errors.append(f"{a.rec.handle} {src}: {e}")
    return errors


_ws = re.compile(r"\s+")
_url = re.compile(r"https?://\S+")


def _fingerprint(p: Post) -> str:
    text = _url.sub("", p.plain_text).lower()
    return _ws.sub(" ", re.sub(r"[^\w\s]", "", text)).strip()


def collapse(posts: list[Post]) -> list[Post]:
    """Merge duplicates: the same post fetched twice, or the same text cross-posted to two networks.

    Reposts of a post are folded into one entry; who reposted it is kept in extra["boosted_by"]."""
    by_id: dict[str, Post] = {}
    order: list[str] = []
    for p in posts:
        ident = f"{p.network}:{p.extra.get('uri') or p.id}"
        if ident in by_id:
            keep = by_id[ident]
            if p.reposted_by:
                keep.extra.setdefault("boosted_by", []).append(p.reposted_by.handle)
            continue
        if p.reposted_by:
            p.extra.setdefault("boosted_by", []).append(p.reposted_by.handle)
        by_id[ident] = p
        order.append(ident)
    out: list[Post] = []
    seen: dict[str, Post] = {}
    for ident in order:
        p = by_id[ident]
        fp = _fingerprint(p)
        twin = seen.get(fp) if len(fp) >= 20 else None
        if twin and twin.network != p.network and abs(twin.created_at - p.created_at) < 900:
            twin.also_on.append({"network": p.network, "url": p.url, "key": p.key})
            twin.likes += p.likes
            twin.reposts += p.reposts
            twin.replies += p.replies
            continue
        if len(fp) >= 20:
            seen[fp] = p
        out.append(p)
    return out
