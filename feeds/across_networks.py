"""What's being talked about on more than one network at once.

A post ranks higher when the link it shares (or the post itself, cross-posted) turns up on Bluesky
and Mastodon, and when more different people shared that link. Hashtags in use on both networks
add a smaller boost. Everything else is scored by engagement within its own network and decayed
with age, so a lively single-network post can still appear lower down.

Discover and Trending are included as sources so a link spreading outside the people you follow
still counts towards "talked about elsewhere".
"""
from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="Across networks",
    description="Links and topics turning up on Bluesky and Mastodon at the same time.",
    sources=["bluesky:following", "mastodon:home", "bluesky:discover", "mastodon:trending"],
    window_hours=24,
    include_replies=False,
)


DISCOVERY = {"bluesky:discover", "mastodon:trending"}


def keep(post: Post, ctx: Ctx) -> bool:
    if ctx.is_mine(post):
        return False
    # Discover and Trending are there for context: show their posts only when they reach another network too.
    from_people_you_follow = not set(ctx.seen_in(post)) <= DISCOVERY
    return from_people_you_follow or len(ctx.networks_discussing(post)) > 1


def score(post: Post, ctx: Ctx) -> dict:
    decay = 1 / (ctx.age_hours(post) + 2) ** 0.8
    nets = len(ctx.networks_discussing(post))
    shared_tags = sum(1 for t in ctx.tags(post) if len(ctx.tag_networks(t)) > 1)
    return {
        "on other networks": 4 * (nets - 1) * decay,
        "people sharing the link": 1.5 * ctx.log1p(ctx.people_sharing_link(post) - 1) * decay,
        "tags used elsewhere": 0.5 * min(shared_tags, 3) * decay,
        "engagement": 2 * ctx.normalised_engagement(post) * decay,
    }
