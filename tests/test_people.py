import asyncio

import pytest

from crossposter import people
from crossposter.db import DB
from crossposter.models import Account
from crossposter.richtext import length_for


@pytest.fixture
def db():
    d = DB(":memory:")
    people.save(d, "Alice", {"bluesky": ["alice.bsky.social"], "mastodon": ["alice@hachyderm.io"], "x": ["AliceX"]})
    people.save(d, "Bob", {"bluesky": "bob.bsky.social"})
    return d


def rewrite(db, text, network, bridges=None):
    return people.apply(text, people.plan(db, text, network, bridges))


def test_finds_mentions_of_every_shape_but_not_emails_or_urls():
    text = "hi @alice.bsky.social, @alice@hachyderm.io and @AliceX! mail a@b.com or see https://x.com/@nope."
    assert [m[2] for m in people.find_mentions(text)] == ["@alice.bsky.social", "@alice@hachyderm.io", "@AliceX"]


def test_linked_person_gets_their_handle_on_each_network(db):
    text = "Great talk by @alice.bsky.social."
    assert rewrite(db, text, "bluesky") == text
    assert rewrite(db, text, "mastodon") == "Great talk by @alice@hachyderm.io."
    assert rewrite(db, text, "x") == "Great talk by @alicex."
    # Typing any of her handles works, case-insensitively.
    assert rewrite(db, "@ALICE@hachyderm.io", "bluesky") == "@alice.bsky.social"
    assert rewrite(db, "@AliceX", "mastodon") == "@alice@hachyderm.io"


def test_linked_person_missing_on_a_network_gets_their_name(db):
    assert rewrite(db, "with @bob.bsky.social", "x") == "with Bob"
    assert rewrite(db, "with @bob.bsky.social", "mastodon") == "with Bob"
    # ...or their bridged handle, when Bridgy Fed bridges them.
    assert rewrite(db, "with @bob.bsky.social", "mastodon", {"bob.bsky.social@bsky.brid.gy"}) == \
        "with @bob.bsky.social@bsky.brid.gy"


def test_unknown_foreign_mentions_never_tag_a_stranger(db):
    # On X, "@carol.bsky.social" would tag @carol, and "@dan@mastodon.social" would tag @dan.
    assert rewrite(db, "cc @carol.bsky.social @dan@mastodon.social", "x") == "cc carol.bsky.social dan@mastodon.social"
    assert rewrite(db, "cc @dan@mastodon.social", "bluesky") == "cc dan@mastodon.social"
    # Native shapes are left alone.
    assert rewrite(db, "cc @carol.bsky.social", "bluesky") == "cc @carol.bsky.social"
    assert rewrite(db, "cc @dan@mastodon.social", "mastodon") == "cc @dan@mastodon.social"
    # Bridged when the bridge account exists.
    assert rewrite(db, "cc @dan@mastodon.social", "bluesky", {"dan.mastodon.social.ap.brid.gy"}) == \
        "cc @dan.mastodon.social.ap.brid.gy"


def test_bridged_handles_map_back_to_native(db):
    assert people.unbridge("@carol.bsky.social@bsky.brid.gy") == ("bluesky", "carol.bsky.social")
    assert people.unbridge("dan.mastodon.social.ap.brid.gy") == ("mastodon", "dan@mastodon.social")
    assert rewrite(db, "@carol.bsky.social@bsky.brid.gy", "bluesky") == "@carol.bsky.social"
    assert people.bridge_candidates(db, "@carol.bsky.social and @bob.bsky.social", "mastodon") == \
        ["carol.bsky.social@bsky.brid.gy", "bob.bsky.social@bsky.brid.gy"]


def test_link_merges_people_and_unlink_cleans_up(db):
    pid = people.link(db, "bluesky", "bob.bsky.social", "mastodon", "bob@fosstodon.org")
    bob = people.get(db, pid)
    assert bob.name == "Bob" and bob.handle_on("mastodon") == "bob@fosstodon.org"
    # Linking two people's accounts merges them.
    people.save(db, "Robert", {"x": "robert"})
    people.link(db, "x", "robert", "bluesky", "bob.bsky.social")
    assert people.find(db, "x", "robert").id == people.find(db, "mastodon", "bob@fosstodon.org").id
    assert len(people.all_people(db)) == 2
    for n, h in people.find(db, "x", "robert").all_handles():
        people.unlink(db, n, h)
    assert [p.name for p in people.all_people(db)] == ["Alice"]


def test_suggestions_from_people_and_seen_accounts(db):
    people.remember(db, [Account("mastodon", "1", "alicia@example.social", "Alicia K"),
                         Account("bluesky", "d", "alice.bsky.social", "Alice B")])
    found, seen = people.suggest_local(db, "ali")
    assert [p.name for p in found] == ["Alice"]
    assert [a.handle for a in seen] == ["alicia@example.social"]  # Alice's own account is folded into her entry


def test_reply_mentions_and_bio_handles():
    from crossposter.models import Post, Segment
    post = Post(network="mastodon", id="1", account_id="m", author=Account("mastodon", "1", "carol@a.social"),
                created_at=0, text=[Segment("mention", "@me", target="me@b.social"),
                                    Segment("mention", "@dan", target="dan@c.social")])
    assert people.reply_mentions(post, {"me@b.social"}) == "@carol@a.social @dan@c.social "
    bio = Account("bluesky", "d", "eve.bsky.social",
                  bio="also @eve@hachyderm.io and https://x.com/eve_x, https://fosstodon.org/@eve2")
    assert people.handles_in_bio(bio) == [("x", "eve_x"), ("mastodon", "eve2@fosstodon.org"),
                                          ("mastodon", "eve@hachyderm.io")]


def test_mastodon_counts_mentions_by_username():
    assert length_for("mastodon", "hi @alice@hachyderm.io") == len("hi @alice")
    assert length_for("bluesky", "hi @alice@hachyderm.io") == len("hi @alice@hachyderm.io")


def test_rewrite_for_demo_accounts_skips_bridge_lookups(db):
    from crossposter.adapters.demo import DemoAdapter
    text, mentions = asyncio.run(people.rewrite(db, "@alice.bsky.social", DemoAdapter("mastodon", db)))
    assert text == "@alice@hachyderm.io" and mentions[0].how == "linked"


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient
    from crossposter.app import app
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_people_pages_link_and_mentions_follow_them(client):
    from crossposter.adapters.demo import DemoAdapter
    r = client.post("/people/save", data={"name": "Ada", "bluesky": "ada.bsky.social", "mastodon": "ada@hachyderm.io"},
                    follow_redirects=True)
    assert r.status_code == 200 and "@ada@hachyderm.io" in r.text
    # Profile pages show the linked accounts and offer linking.
    r = client.get("/profile/bluesky/ada.bsky.social?acct=demo-bluesky")
    assert "Also on" in r.text and "ada@hachyderm.io" in r.text
    r = client.get("/profile/mastodon/grace@hachyderm.io?acct=demo-mastodon")
    assert "Same person on other networks" in r.text
    r = client.post("/people/link", data={"network": "mastodon", "handle": "grace@hachyderm.io", "name": "Grace Hopper",
                                          "other_network": "bluesky", "other_handle": "grace.bsky.social",
                                          "back": "/profile/mastodon/grace@hachyderm.io"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/profile/mastodon/grace@hachyderm.io"

    # Autocomplete finds linked people and the networks' own search.
    items = client.get("/people/suggest?q=ad&networks=mastodon").json()
    assert items[0]["name"] == "Ada" and items[0]["insert"] == "@ada@hachyderm.io" and items[0]["linked"]
    items = client.get("/people/suggest?q=katherine&networks=mastodon").json()
    assert items and items[0]["insert"] == "@katherine@mastodon.social"

    # The preview explains, and the post uses each network's handle.
    form = {"text": "thanks @ada.bsky.social and @grace@hachyderm.io", "accounts": ["demo-bluesky", "demo-mastodon"]}
    r = client.post("/compose/preview", data=form)
    assert "How your mentions will read" in r.text and "@ada@hachyderm.io" in r.text and "@grace.bsky.social" in r.text
    assert client.post("/compose/preview", data={"text": "no mentions", "accounts": ["demo-bluesky"]}).text == ""
    r = client.post("/compose", data={**form, "thread": "on"})
    assert "Posted." in r.text
    texts = {p.network: p.plain_text for p in DemoAdapter._posted[:2]}
    assert texts == {"bluesky": "thanks @ada.bsky.social and @grace.bsky.social",
                     "mastodon": "thanks @ada@hachyderm.io and @grace@hachyderm.io"}


def test_mastodon_reply_starts_with_mentions(client):
    from crossposter import cache
    from crossposter.app import db
    from crossposter.models import Post, Segment
    post = Post(network="mastodon", id="r1", account_id="demo-mastodon",
                author=Account("mastodon", "9", "margaret@fosstodon.org"), created_at=0,
                text=[Segment("text", "hi "), Segment("mention", "@you", target="you@mastodon.social")])
    cache.save_posts(db, [post])
    r = client.get("/compose?reply=demo-mastodon%7Cr1")
    assert ">@margaret@fosstodon.org </textarea>" in r.text
