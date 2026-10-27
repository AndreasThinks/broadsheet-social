"""People who rarely post, so their posts don't get buried under the busiest accounts.

Each author's posts in the window are counted; the fewer they've posted, the higher they rank.
"""
from collections import Counter

from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="Quiet posters",
    description="Posts from people who post least often, so you don't miss them.",
    sources=["bluesky:following", "mastodon:home"],
    window_hours=48,
    include_replies=False,
)

_counts: dict[int, Counter] = {}


def _per_author(ctx: Ctx) -> Counter:
    key = id(ctx)
    if key not in _counts:
        _counts.clear()
        _counts[key] = Counter(p.author.handle for p in ctx.posts if not p.reposted_by)
    return _counts[key]


def keep(post: Post, ctx: Ctx) -> bool:
    return post.reposted_by is None and not ctx.is_mine(post)


def score(post: Post, ctx: Ctx) -> dict:
    n = _per_author(ctx)[post.author.handle]
    return {"rarity": 1 / n, "recency": 0.5 / (ctx.age_hours(post) + 1)}
