"""Posts that share a link, ranked by how much discussion they got. Good for a reading list."""
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
        "discussion": ctx.log1p(post.replies + post.quotes) * 2,
        "engagement": ctx.normalised_engagement(post),
        "freshness": 1 / (ctx.age_hours(post) + 3),
    }
