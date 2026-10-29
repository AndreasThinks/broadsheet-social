from crossposter.adapters.bluesky import to_post as bsky_post
from crossposter.adapters.mastodon import normalise_instance, to_post as masto_post
from crossposter.models import Post

BSKY_VIEW = {
    "uri": "at://did:plc:a/app.bsky.feed.post/3k1", "cid": "bafy1",
    "author": {"did": "did:plc:a", "handle": "ada.bsky.social", "displayName": "Ada", "avatar": "https://cdn/a.jpg"},
    "record": {"text": "Look at this", "createdAt": "2026-10-27T10:00:00Z",
               "reply": {"root": {"uri": "at://r", "cid": "c"}, "parent": {"uri": "at://p", "cid": "c2"}}},
    "embed": {"$type": "app.bsky.embed.recordWithMedia#view",
              "media": {"$type": "app.bsky.embed.images#view",
                        "images": [{"thumb": "https://t", "fullsize": "https://f", "alt": "a cat",
                                    "aspectRatio": {"width": 4, "height": 3}}]},
              "record": {"record": {"$type": "app.bsky.embed.record#viewRecord", "uri": "at://did:plc:b/app.bsky.feed.post/9",
                                    "cid": "bafy2", "author": {"did": "did:plc:b", "handle": "bob.test"},
                                    "value": {"text": "quoted", "createdAt": "2026-10-26T10:00:00Z"}}}},
    "likeCount": 5, "repostCount": 2, "replyCount": 1, "quoteCount": 0,
    "viewer": {"like": "at://did:plc:me/app.bsky.feed.like/1"},
    "labels": [{"val": "graphic-media"}],
}


def test_bluesky_post_view():
    p = bsky_post(BSKY_VIEW, "bluesky:did:plc:me",
                  reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": "did:plc:c", "handle": "cat.test"}},
                  reply={"parent": {"author": {"handle": "parent.test"}}})
    assert p.url == "https://bsky.app/profile/ada.bsky.social/post/3k1"
    assert p.media[0].alt == "a cat" and p.media[0].width == 4
    assert p.quote and p.quote.plain_text == "quoted"
    assert p.reposted_by.handle == "cat.test"
    assert p.reply_to_handle == "parent.test" and p.is_reply
    assert p.liked and p.extra["like_uri"].endswith("/1")
    assert p.sensitive and p.content_warning == "graphic-media"
    assert p.extra["root"] == {"uri": "at://r", "cid": "c"}
    assert Post.from_dict(p.to_dict()) == p


MASTO = {
    "id": "200", "account": {"id": "9", "acct": "booster", "username": "booster", "display_name": "B"},
    "reblog": {
        "id": "100", "uri": "https://other.social/statuses/1", "url": "https://other.social/@g/1",
        "created_at": "2026-10-27T09:00:00.000Z", "content": "<p>Hello <a href=\"https://x.y\">x.y</a></p>",
        "account": {"id": "5", "acct": "grace@other.social", "username": "grace", "display_name": "Grace",
                    "avatar": "https://a", "url": "https://other.social/@grace"},
        "media_attachments": [{"type": "gifv", "url": "https://v.mp4", "preview_url": "https://p.jpg",
                               "description": "dancing", "meta": {"original": {"width": 10, "height": 5}}}],
        "card": {"url": "https://x.y", "title": "XY", "description": "d", "image": None},
        "favourites_count": 3, "reblogs_count": 4, "replies_count": 1, "favourited": True,
        "spoiler_text": "spoilers", "sensitive": True, "mentions": [], "in_reply_to_id": None,
    },
}


def test_mastodon_status():
    p = masto_post(MASTO, "mastodon:1@m.social", "m.social")
    assert p.id == "100" and p.reposted_by.handle == "booster@m.social"
    assert p.author.handle == "grace@other.social"
    assert p.media[0].kind == "gif" and p.media[0].alt == "dancing"
    assert p.card.title == "XY"
    assert p.liked and p.content_warning == "spoilers"
    assert p.plain_text == "Hello x.y"


def test_normalise_instance():
    assert normalise_instance("https://Mastodon.Social/") == "mastodon.social"
    assert normalise_instance("@me@fosstodon.org") == "fosstodon.org"
    assert normalise_instance("hachyderm.io") == "hachyderm.io"


def test_reply_parents_are_recorded():
    from crossposter.adapters import bluesky, mastodon, x
    author = {"did": "did:plc:a", "handle": "a.bsky.social"}
    parent = {"uri": "at://did:plc:b/app.bsky.feed.post/1", "author": {"did": "did:plc:b", "handle": "b.bsky.social"},
              "record": {"text": "the question", "createdAt": "2026-10-28T10:00:00Z"}}
    pv = {"uri": "at://did:plc:a/app.bsky.feed.post/2", "author": author,
          "record": {"text": "the answer", "createdAt": "2026-10-28T10:05:00Z",
                     "reply": {"parent": {"uri": parent["uri"]}, "root": {"uri": parent["uri"]}}}}
    p = bluesky.to_post(pv, "bluesky:me", reply={"parent": parent, "root": parent})
    assert p.reply_to_id == parent["uri"] and p.parent.plain_text == "the question"
    assert type(p).from_dict(p.to_dict()).parent.plain_text == "the question"
    s = {"id": "9", "in_reply_to_id": "8", "in_reply_to_account_id": "2", "content": "<p>hi</p>",
         "created_at": "2026-10-28T10:00:00Z", "account": {"id": "1", "acct": "a"}, "mentions": []}
    assert mastodon.to_post(s, "mastodon:me", "m.social").reply_to_id == "8"
    t = {"data": [{"id": "5", "text": "yes", "author_id": "1", "in_reply_to_user_id": "2",
                   "referenced_tweets": [{"type": "replied_to", "id": "4"}]}]}
    assert x._parse(t, "x:me")[0].reply_to_id == "4"
