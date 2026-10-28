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


def _linked(network, pid, url, handle, ago=1.0):
    from crossposter.models import LinkCard
    p = mk(network, pid, f"read this {pid}", handle=handle, ago=ago)
    p.card = LinkCard(url=url)
    return p


def test_ctx_cross_network_helpers():
    a = _linked("bluesky", "1", "https://www.example.com/story/?utm_source=bsky", "alice")
    b = _linked("mastodon", "2", "http://example.com/story", "bob@m.social")
    c = _linked("bluesky", "3", "https://other.example/x", "carol")
    ctx = feeds.Ctx([a, b, c], set(), set())
    assert ctx.links(a) == ["example.com/story"]
    assert ctx.networks_discussing(a) == {"bluesky", "mastodon"}
    assert ctx.networks_discussing(c) == {"bluesky"}
    assert ctx.people_sharing_link(a) == 2


def test_across_networks_feed_ranks_shared_links_first():
    lf = feeds.get_feed("across_networks")
    assert lf and not lf.error
    shared = [_linked("bluesky", "1", "https://example.com/a", "alice"),
              _linked("mastodon", "2", "https://example.com/a", "bob")]
    solo = mk("bluesky", "3", "a popular post on one network only", handle="dan", likes=500, ago=0.1)
    discover_only = mk("bluesky", "4", "something from discover", handle="eve", likes=900)
    for p in shared + [solo]:
        p.extra["sources"] = ["bluesky:following"]
    discover_only.extra["sources"] = ["bluesky:discover"]
    posts = shared + [solo, discover_only]
    ranked = feeds.rank(lf, posts, feeds.Ctx(posts, set(), set()))
    ids = [r.post.id for r in ranked]
    assert set(ids[:2]) == {"1", "2"} and "4" not in ids


def test_starter_gallery_install_remove_and_arrange(tmp_path):
    starters = tmp_path / "starters"
    mine = tmp_path / "mine"
    starters.mkdir()
    for name in ("one", "two"):
        (starters / f"{name}.py").write_text(f'from crossposter.feeds import Feed\nfeed = Feed(name="{name}", sources=[])\n'
                                             'def score(post, ctx):\n    return 0\n')
    assert [f.slug for f in feeds.starter_feeds(mine, starters)] == ["one", "two"]
    assert feeds.install_starter("one", mine, starters)
    assert [f.slug for f in feeds.starter_feeds(mine, starters)] == ["two"]
    assert feeds.remove_feed("one", mine) and (mine / "_removed_one.py").exists()
    # A removed starter isn't offered again, and isn't loaded.
    assert [f.slug for f in feeds.starter_feeds(mine, starters)] == ["two"]
    assert [f.slug for f in feeds.all_feeds(mine)] == ["combined"]

    loaded = feeds.all_feeds()
    slugs = [lf.slug for lf in loaded]
    ordered, hidden = feeds.arrange(loaded, {"order": [slugs[-1]], "hidden": ["combined"]})
    assert [lf.slug for lf in ordered] == [slugs[-1]] + slugs[:-1] and hidden == {"combined"}


def test_save_arrangement_keeps_absent_tabs():
    prefs = {"order": ["a", "gone", "b"], "hidden": ["gone"], "shown": ["gone-native"], "other": 1}
    tabs = [("b", True, True), ("n", True, False), ("a", False, True)]
    new = feeds.save_arrangement(prefs, tabs, {"a", "b", "n"})
    assert new["order"] == ["b", "n", "a", "gone"]
    assert new["hidden"] == ["a", "gone"] and new["shown"] == ["gone-native", "n"] and new["other"] == 1
