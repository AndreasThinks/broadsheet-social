"""Custom feeds: plain Python files in the feeds/ folder.

A feed file defines `feed = Feed(...)`, an optional `keep(post, ctx) -> bool` and a
`score(post, ctx)` that returns a number, or a dict of named numbers (shown as the
"why am I seeing this" breakdown). Files reload automatically when you save them.

Sources a feed can draw from:
    bluesky:following      bluesky:discover        bluesky:feed:<at:// uri>   bluesky:list:<at:// uri>
    mastodon:home          mastodon:local          mastodon:public            mastodon:trending
    mastodon:list:<id>     mastodon:tag:<name>     x:home (experimental, costs money)
"""
from __future__ import annotations

import asyncio
import bisect
import importlib.util
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cache
from .config import settings
from .models import Account, Post

log = logging.getLogger(__name__)

__all__ = ["Feed", "Ctx", "Post", "Account"]


@dataclass
class Feed:
    name: str
    sources: list[str]
    window_hours: float = 24
    description: str = ""
    limit: int = 300            # most posts to rank
    include_replies: bool = True


class Ctx:
    """Helpers passed to keep() and score(). Everything here works on cached data; no network calls."""

    def __init__(self, posts: list[Post], my_handles: set[str], muted: set[str], now: float | None = None):
        self.posts = posts
        self.my_handles = my_handles
        self.muted = muted
        self.now = now or time.time()
        self._eng: dict[str, list[int]] = defaultdict(list)
        for p in posts:
            self._eng[p.network].append(p.engagement)
        for v in self._eng.values():
            v.sort()

    def age_hours(self, post: Post) -> float:
        return max(0.0, (self.now - post.created_at) / 3600)

    def normalised_engagement(self, post: Post) -> float:
        """0..1: the share of posts from the same network in this feed with less engagement."""
        values = self._eng.get(post.network) or [0]
        return bisect.bisect_left(values, post.engagement) / max(1, len(values))

    def follows_who_engaged(self, post: Post) -> list[str]:
        """Handles of people you follow who reposted or boosted this post, as seen in your timelines."""
        return list(dict.fromkeys(post.extra.get("boosted_by") or []))

    def is_mine(self, post: Post) -> bool:
        return post.author.handle in self.my_handles

    def is_muted(self, post: Post) -> bool:
        return post.author.handle in self.muted

    @staticmethod
    def log1p(x: float) -> float:
        return math.log1p(max(0.0, x))


@dataclass
class LoadedFeed:
    slug: str
    path: Path | None
    feed: Feed | None
    keep: Callable | None
    score: Callable | None
    source: str
    mtime: float
    error: str = ""
    builtin: bool = False


def _combined_score(post: Post, ctx: Ctx) -> float:
    return post.created_at


COMBINED_SOURCE = '''# Built in: your Following (Bluesky), Home (Mastodon) and, if enabled, X home, newest first.
feed = Feed(name="Combined", sources=["bluesky:following", "mastodon:home", "x:home"], window_hours=48)

def score(post, ctx):
    return post.created_at
'''

COMBINED = LoadedFeed(slug="combined", path=None, builtin=True, keep=None, score=_combined_score,
                      feed=Feed(name="Combined", sources=["bluesky:following", "mastodon:home", "x:home"],
                                window_hours=48, description="Following and Home from every network, newest first."),
                      source=COMBINED_SOURCE, mtime=0)

_loaded: dict[str, LoadedFeed] = {}


def _load(path: Path) -> LoadedFeed:
    slug = path.stem
    source = path.read_text()
    mtime = path.stat().st_mtime
    modname = f"crossposter_user_feeds.{slug}"
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        spec.loader.exec_module(module)
        feed = getattr(module, "feed", None)
        if not isinstance(feed, Feed):
            raise ValueError("the file must define feed = Feed(name=..., sources=[...])")
        score = getattr(module, "score", None)
        if not callable(score):
            raise ValueError("the file must define score(post, ctx)")
        return LoadedFeed(slug, path, feed, getattr(module, "keep", None), score, source, mtime)
    except Exception as e:
        log.warning("feed %s failed to load: %s", path, e)
        return LoadedFeed(slug, path, None, None, None, source, mtime, error=f"{type(e).__name__}: {e}")


def all_feeds(feeds_dir: Path | None = None) -> list[LoadedFeed]:
    """Every feed file, reloading any that changed on disk."""
    d = feeds_dir or settings.feeds_dir
    out = [COMBINED]
    paths = sorted(d.glob("*.py")) if d.exists() else []
    for path in paths:
        if path.name.startswith("_"):
            continue
        cached = _loaded.get(str(path))
        if cached is None or cached.mtime != path.stat().st_mtime:
            cached = _loaded[str(path)] = _load(path)
        out.append(cached)
    return out


def get_feed(slug: str, feeds_dir: Path | None = None) -> LoadedFeed | None:
    return next((f for f in all_feeds(feeds_dir) if f.slug == slug), None)


@dataclass
class Ranked:
    post: Post
    score: float
    terms: dict[str, float] = field(default_factory=dict)


def rank(lf: LoadedFeed, posts: list[Post], ctx: Ctx) -> list[Ranked]:
    out = []
    for p in posts:
        if not lf.feed.include_replies and p.is_reply:
            continue
        if ctx.is_muted(p):
            continue
        if lf.keep and not lf.keep(p, ctx):
            continue
        s = lf.score(p, ctx)
        if isinstance(s, dict):
            terms = {k: float(v) for k, v in s.items()}
            out.append(Ranked(p, sum(terms.values()), terms))
        else:
            out.append(Ranked(p, float(s)))
    out.sort(key=lambda r: r.score, reverse=True)
    return out


_results: dict[str, tuple[float, list[Ranked]]] = {}


async def run(db, adapters, slug: str, fresh: bool = False) -> list[Ranked]:
    """Refresh the feed's sources if stale, then rank cached posts. Results are kept for two minutes so paging is stable."""
    lf = get_feed(slug)
    if lf is None:
        raise KeyError(slug)
    if lf.error:
        raise RuntimeError(lf.error)
    hit = _results.get(slug)
    if hit and not fresh and time.time() - hit[0] < 120 and hit[0] > lf.mtime:
        return hit[1]
    await cache.refresh(db, adapters, lf.feed.sources)
    posts = cache.collapse(cache.candidates(db, lf.feed.sources, lf.feed.window_hours))[: lf.feed.limit]
    my = {a.rec.handle for a in adapters}
    ctx = Ctx(posts, my, set(db.get("muted", [])))
    try:
        ranked = await asyncio.wait_for(asyncio.to_thread(rank, lf, posts, ctx), timeout=settings.feed_timeout_seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Feed took longer than {settings.feed_timeout_seconds:g}s to rank and was stopped.")
    _results[slug] = (time.time(), ranked)
    return ranked


def all_sources(feeds_dir: Path | None = None) -> list[str]:
    srcs = []
    for lf in all_feeds(feeds_dir):
        if lf.feed:
            srcs += lf.feed.sources
    return list(dict.fromkeys(srcs))
