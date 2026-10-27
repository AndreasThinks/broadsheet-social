import time

from crossposter import cache, feeds
from crossposter.db import DB
from crossposter.models import Account, Post
from crossposter.richtext import plain_segments


def mk(network, pid, text, handle="a", likes=0, ago=0.0, reposted_by=None, uri=None):
    p = Post(network=network, id=pid, account_id=f"{network}:me", author=Account(network, handle, handle),
             created_at=time.time() - ago * 3600, text=plain_segments(text), likes=likes,
             reposted_by=Account(network, reposted_by, reposted_by) if reposted_by else None)
    if uri:
        p.extra["uri"] = uri
    return p


def test_cache_round_trip_and_sources():
    db = DB(":memory:")
    p = mk("bluesky", "at://1", "hello world")
    cache.save_posts(db, [p], "bluesky:following")
    cache.save_posts(db, [p], "bluesky:discover")
    assert cache.get_post(db, p.key) == p
    assert len(cache.candidates(db, ["bluesky:discover"], 24)) == 1
    assert cache.candidates(db, ["mastodon:home"], 24) == []


def test_collapse_merges_cross_posts_and_boosts():
    text = "the same announcement posted on both networks today"
    a = mk("bluesky", "at://1", text, likes=3)
    b = mk("mastodon", "9", text + " https://link.example", likes=2)
    c = mk("mastodon", "10", "boosted thing that is long enough", reposted_by="friend", uri="https://x/1")
    d = mk("mastodon", "10", "boosted thing that is long enough", reposted_by="pal", uri="https://x/1")
    out = cache.collapse([a, b, c, d])
    assert len(out) == 2
    assert out[0].also_on[0]["network"] == "mastodon" and out[0].likes == 5
    assert out[1].extra["boosted_by"] == ["friend", "pal"]


def test_feed_loading_ranking_and_hot_reload(tmp_path):
    f = tmp_path / "best.py"
    f.write_text('from crossposter.feeds import Feed\n'
                 'feed = Feed(name="Best", sources=["bluesky:following"])\n'
                 'def keep(post, ctx):\n    return "skip" not in post.plain_text\n'
                 'def score(post, ctx):\n    return {"likes": post.likes, "fresh": -ctx.age_hours(post)}\n')
    (tmp_path / "broken.py").write_text("feed = 1\n")
    loaded = {lf.slug: lf for lf in feeds.all_feeds(tmp_path)}
    assert "combined" in loaded and loaded["broken"].error
    lf = loaded["best"]
    posts = [mk("bluesky", "1", "one", likes=1), mk("bluesky", "2", "two", likes=10), mk("bluesky", "3", "skip me", likes=99)]
    ranked = feeds.rank(lf, posts, feeds.Ctx(posts, set(), set()))
    assert [r.post.id for r in ranked] == ["2", "1"]
    assert set(ranked[0].terms) == {"likes", "fresh"}

    import os
    f.write_text(f.read_text().replace('"Best"', '"Better"'))
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert feeds.get_feed("best", tmp_path).feed.name == "Better"


def test_ctx_normalised_engagement_is_per_network():
    posts = [mk("bluesky", str(i), "x", likes=i * 100) for i in range(4)] + [mk("mastodon", "m", "y", likes=5)]
    ctx = feeds.Ctx(posts, {"me"}, {"muted"})
    assert ctx.normalised_engagement(posts[3]) == 0.75
    assert ctx.normalised_engagement(posts[4]) == 0.0
    assert ctx.is_muted(mk("bluesky", "z", "q", handle="muted"))


def test_starter_feeds_load():
    for lf in feeds.all_feeds():
        assert not lf.error, (lf.slug, lf.error)
