"""Posts that share a link, ranked by how much discussion they got. Good for a reading list.

Replies are compared within each network (Mastodon has no quotes and only counts replies your
server has seen), so links from Bluesky and Mastodon get a fair share of the top spots.
"""
from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="Links worth reading",
    description="Posts with a link card from your timelines, most discussed first.",
    sources=["bluesky:following", "mastodon:home"],
    window_hours=36,
)


def keep(post: Post, ctx: Ctx) -> bool:
    return post.card is not None


def score(post: Post, ctx: Ctx) -> dict:
    return {
        "discussion": 2 * ctx.normalised(post, lambda p: p.replies + p.quotes),
        "engagement": ctx.normalised_engagement(post),
        "freshness": 1 / (ctx.age_hours(post) + 3),
    }
