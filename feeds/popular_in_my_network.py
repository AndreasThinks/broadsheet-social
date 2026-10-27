"""Posts doing well in your own network over the last day, from both Bluesky and Mastodon.

Engagement is compared within each network (Mastodon only reports the counts your server
has seen, so raw numbers would under-rate Mastodon posts), then boosted when people you
follow reposted it, and decayed with age.
"""
from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="Popular in my network",
    description="Well-liked posts from people you follow on every network, last 24 hours.",
    sources=["bluesky:following", "mastodon:home"],
    window_hours=24,
    include_replies=False,
)


def keep(post: Post, ctx: Ctx) -> bool:
    return not ctx.is_mine(post)


def score(post: Post, ctx: Ctx) -> dict:
    age = ctx.age_hours(post)
    decay = 1 / (age + 2) ** 1.2
    return {
        "engagement": 10 * ctx.normalised_engagement(post) * decay,
        "friends reposted": 2 * len(ctx.follows_who_engaged(post)) * decay,
    }
