"""Custom feeds: plain Python files in the feeds/ folder.

A feed file defines `feed = Feed(...)`, an optional `keep(post, ctx) -> bool` and a
`score(post, ctx)` that returns a number, or a dict of named numbers (shown as the
"why am I seeing this" breakdown). Files reload automatically when you save them.

Starter feeds ship in the repo's feeds/ folder; any your feeds folder doesn't have yet are offered
under "Add more feeds" on the Feeds page. Tab order and which feeds show on Home (set on the Arrange tabs page) are
saved in the database (feed_prefs), not in the files.

Sources a feed can draw from:
    bluesky:following      bluesky:discover        bluesky:feed:<at:// uri>   bluesky:list:<at:// uri>
    mastodon:home          mastodon:local          mastodon:public            mastodon:trending
    mastodon:list:<id>     mastodon:tag:<name>     x:home (costs money)
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
from urllib.parse import urlsplit
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
        self._spread: dict[str, dict[str, set[str]]] | None = None

    def age_hours(self, post: Post) -> float:
        """How many hours ago the post was made."""
        return max(0.0, (self.now - post.created_at) / 3600)

    def normalised_engagement(self, post: Post) -> float:
        """0..1: the share of posts from the same network in this feed with less engagement."""
        values = self._eng.get(post.network) or [0]
        return bisect.bisect_left(values, post.engagement) / max(1, len(values))

    def follows_who_engaged(self, post: Post) -> list[str]:
        """Handles of people you follow who reposted or boosted this post, as seen in your timelines."""
        return list(dict.fromkeys(post.extra.get("boosted_by") or []))

    @staticmethod
    def seen_in(post: Post) -> list[str]:
        """The sources this post was fetched from, e.g. ["bluesky:following", "bluesky:discover"]."""
        return list(post.extra.get("sources") or [])

    def is_mine(self, post: Post) -> bool:
        """True for posts from your own connected accounts."""
        return post.author.handle in self.my_handles

    def is_muted(self, post: Post) -> bool:
        """True for authors on your muted list (they are already left out before keep() runs)."""
        return post.author.handle in self.muted

    @staticmethod
    def log1p(x: float) -> float:
        """log(1 + x), clamped at 0: tames big counts like likes so one viral post doesn't swamp the rest."""
        return math.log1p(max(0.0, x))

    @staticmethod
    def links(post: Post) -> list[str]:
        """The web links in a post (its link card and any links in the text), normalised so the same page matches."""
        urls = ([post.card.url] if post.card else []) + [s.href for s in post.text if s.kind == "link" and s.href]
        return list(dict.fromkeys(u for u in map(normalise_url, urls) if u))

    @staticmethod
    def tags(post: Post) -> list[str]:
        """The hashtags in a post, lower case and without the #."""
        return list(dict.fromkeys((s.target or s.text).lstrip("#").lower() for s in post.text if s.kind == "tag"))

    def _topics(self) -> dict[str, dict[str, set[str]]]:
        if self._spread is None:
            spread: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
            for p in self.posts:
                who = (p.reposted_by or p.author).handle
                for topic in [f"link:{u}" for u in self.links(p)] + [f"tag:{t}" for t in self.tags(p)]:
                    spread[topic][p.network].add(who)
                    for o in p.also_on:
                        spread[topic][o["network"]].add(p.author.handle)
            self._spread = spread
        return self._spread

    def networks_discussing(self, post: Post) -> set[str]:
        """Every network where this post, or a post sharing one of its links, shows up in this feed's candidates."""
        nets = {post.network} | {o["network"] for o in post.also_on}
        for u in self.links(post):
            nets |= set(self._topics().get(f"link:{u}", {}))
        return nets

    def people_sharing_link(self, post: Post) -> int:
        """How many different people (across every network) shared any of this post's links."""
        who: set[str] = set()
        for u in self.links(post):
            for handles in self._topics().get(f"link:{u}", {}).values():
                who |= handles
        return len(who)

    def tag_networks(self, tag: str) -> set[str]:
        """The networks where a hashtag is being used in this feed's candidates."""
        return set(self._topics().get(f"tag:{tag.lstrip('#').lower()}", {}))


_TRACKING = ("utm_", "fbclid", "gclid", "ref_src", "mc_")


def normalise_url(url: str) -> str:
    """Strip scheme, www., tracking parameters and trailing slashes so two shares of one page compare equal."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return ""
    if not u.netloc:
        return ""
    host = u.netloc.lower().removeprefix("www.")
    query = "&".join(q for q in u.query.split("&") if q and not q.lower().startswith(_TRACKING))
    return host + u.path.rstrip("/") + ("?" + query if query else "")


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
    out += [_cached_load(path) for path in paths if not path.name.startswith("_")]
    return out


def get_feed(slug: str, feeds_dir: Path | None = None) -> LoadedFeed | None:
    return next((f for f in all_feeds(feeds_dir) if f.slug == slug), None)


STARTER_DIR = Path(__file__).resolve().parent.parent / "feeds"


def _cached_load(path: Path) -> LoadedFeed:
    cached = _loaded.get(str(path))
    if cached is None or cached.mtime != path.stat().st_mtime:
        cached = _loaded[str(path)] = _load(path)
    return cached


def starter_feeds(feeds_dir: Path | None = None, starter_dir: Path | None = None) -> list[LoadedFeed]:
    """Bundled feeds that aren't in your feeds folder yet (new ones arrive with app updates)."""
    d = feeds_dir or settings.feeds_dir
    src = starter_dir or STARTER_DIR
    if not src.exists() or (d.exists() and d.resolve() == src.resolve()):
        return []
    have = {p.stem for p in d.glob("*.py")} if d.exists() else set()
    removed = {p.stem.removeprefix("_removed_") for p in d.glob("_removed_*.py")} if d.exists() else set()
    return [_cached_load(p) for p in sorted(src.glob("*.py"))
            if not p.name.startswith("_") and p.stem not in have and p.stem not in removed]


def install_starter(slug: str, feeds_dir: Path | None = None, starter_dir: Path | None = None) -> bool:
    d = feeds_dir or settings.feeds_dir
    src = (starter_dir or STARTER_DIR) / f"{slug}.py"
    if not src.is_file() or (d / src.name).exists():
        return False
    d.mkdir(parents=True, exist_ok=True)
    (d / src.name).write_text(src.read_text())
    (d / f"_removed_{slug}.py").unlink(missing_ok=True)
    return True


def remove_feed(slug: str, feeds_dir: Path | None = None) -> bool:
    """Set a feed file aside as _removed_<slug>.py (ignored by the loader) rather than deleting it."""
    d = feeds_dir or settings.feeds_dir
    path = d / f"{slug}.py"
    if not path.is_file():
        return False
    path.replace(d / f"_removed_{slug}.py")
    _loaded.pop(str(path), None)
    _results.pop(slug, None)
    return True


def arrange(items: list, prefs: dict, key: Callable = lambda lf: lf.slug) -> tuple[list, set[str]]:
    """Order items by the saved order (unlisted ones keep their given order, after), and return the hidden keys.

    The saved order holds the keys of every Home tab: custom feed slugs and native feed keys alike."""
    order = {k: i for i, k in enumerate(prefs.get("order") or [])}
    ranked = sorted(enumerate(items), key=lambda t: (order.get(key(t[1]), len(order) + t[0]), t[0]))
    return [it for _, it in ranked], set(prefs.get("hidden") or [])


def save_arrangement(prefs: dict, tabs: list[tuple[str, bool, bool]], present: set[str]) -> dict:
    """New prefs from the Arrange page: tabs is [(key, visible, visible_by_default)] in the chosen order.

    Tabs that aren't listed right now (say an account whose feeds couldn't be fetched) keep their saved place
    and visibility, so a network hiccup doesn't scramble the order."""
    keys = [k for k, _, _ in tabs]
    order = keys + [k for k in prefs.get("order") or [] if k not in keys and k not in present]
    hidden = {k for k in prefs.get("hidden") or [] if k not in present} | {k for k, vis, dflt in tabs if dflt and not vis}
    shown = {k for k in prefs.get("shown") or [] if k not in present} | {k for k, vis, dflt in tabs if vis and not dflt}
    return {**prefs, "order": order, "hidden": sorted(hidden), "shown": sorted(shown)}


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
