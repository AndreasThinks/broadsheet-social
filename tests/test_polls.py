import time

import pytest
from fasthtml.common import to_xml
from starlette.testclient import TestClient

from crossposter.adapters.mastodon import to_post as masto_post
from crossposter.adapters.x import _parse as x_parse
from crossposter.models import Account, Poll, PollOption, Post
from crossposter.ui import poll_block

STATUS = {
    "id": "1", "created_at": "2026-10-31T10:00:00Z", "content": "<p>Tabs or spaces?</p>", "url": "https://m.example/@a/1",
    "account": {"id": "9", "acct": "a", "username": "a", "display_name": "A"},
    "poll": {"id": "34", "expires_at": "2099-01-01T00:00:00Z", "expired": False, "multiple": False,
             "votes_count": 10, "voters_count": 10, "voted": True, "own_votes": [1],
             "options": [{"title": "Tabs", "votes_count": 3}, {"title": "Spaces", "votes_count": 7}]},
}


def _post(poll: Poll, network="mastodon") -> Post:
    author = Account(network=network, id="9", handle="a@m.example", display_name="A")
    return Post(network=network, id="1", account_id="acct", author=author, created_at=0, poll=poll, url="https://x.example/1")


def test_mastodon_poll_is_parsed_and_survives_the_cache():
    p = masto_post(STATUS, "acct", "m.example")
    assert p.poll.id == "34" and [o.title for o in p.poll.options] == ["Tabs", "Spaces"]
    assert p.poll.voted and p.poll.own_votes == [1] and p.poll.can_vote and not p.poll.closed
    again = Post.from_dict(p.to_dict())
    assert again.poll == p.poll and isinstance(again.poll.options[0], PollOption)


def test_results_show_shares_and_your_vote():
    html = to_xml(poll_block(_post(masto_post(STATUS, "acct", "m.example").poll)))
    assert "70%" in html and "30%" in html and "poll-mine" in html and "10 people" in html and "left" in html
    assert "<form" not in html


def test_open_poll_you_have_not_voted_in_offers_choices():
    poll = Poll(id="1", options=[PollOption("a", 1), PollOption("b", 2)], votes=3, multiple=True,
                expires_at=time.time() + 7200, can_vote=True)
    html = to_xml(poll_block(_post(poll)))
    assert html.count('type="checkbox"') == 2 and "/act/vote" in html and "See results" in html
    assert "choose any" in html


def test_hidden_counts_and_finished_polls():
    poll = Poll(id="1", options=[PollOption("a", None), PollOption("b", None)], votes=4, voters=4,
                expires_at=time.time() - 10, can_vote=True)
    html = to_xml(poll_block(_post(poll)))
    assert "Final results" in html and "<form" not in html and "%" not in html.replace("width:0%", "")


def test_x_poll_is_read_only_with_a_link_out():
    data = {"data": [{"id": "5", "author_id": "u", "text": "Which?", "attachments": {"poll_ids": ["p1"]}}],
            "includes": {"users": [{"id": "u", "username": "bob", "name": "Bob"}],
                         "polls": [{"id": "p1", "voting_status": "open", "end_datetime": "2099-01-01T00:00:00Z",
                                    "options": [{"position": 2, "label": "No", "votes": 1},
                                                {"position": 1, "label": "Yes", "votes": 3}]}]}}
    post = x_parse(data, "x:1")[0]
    assert [o.title for o in post.poll.options] == ["Yes", "No"] and post.poll.votes == 4 and not post.poll.can_vote
    html = to_xml(poll_block(post))
    assert "75%" in html and "Vote on X" in html


@pytest.fixture(scope="module")
def client():
    from crossposter.app import app
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_voting_updates_the_cached_post(client):
    from crossposter import cache
    from crossposter.app import db
    author = Account(network="mastodon", id="9", handle="a@m.example")
    poll = Poll(id="7", options=[PollOption("a", 0), PollOption("b", 0)], expires_at=time.time() + 3600, can_vote=True)
    post = Post(network="mastodon", id="vote-1", account_id="demo-mastodon", author=author, created_at=time.time(), poll=poll)
    cache.save_posts(db, [post])
    assert "Pick an option" in client.post(f"/act/vote?key={post.key}", data={}).text
    r = client.post(f"/act/vote?key={post.key}", data={"choice": "1"})
    assert r.status_code == 200 and "100%" in r.text and "poll-mine" in r.text
    saved = cache.get_post(db, post.key)
    assert saved.poll.voted and saved.poll.own_votes == [1]
    assert "Back to voting" not in client.get(f"/act/poll?key={post.key}&results=1").text
