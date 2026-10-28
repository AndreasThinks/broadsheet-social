"""The community directory and the app's side of it, wired together in-process."""
import time

import httpx
import pytest
from starlette.testclient import TestClient

from crossposter import community, feedcheck
from crossposter.config import settings
from directory import store as dstore
from directory.store import Rejected, Store

GOOD = '''from crossposter.feeds import Feed

feed = Feed(name="Morning reading", description="Long reads.", sources=["bluesky:following", "mastodon:home"])


def score(post, ctx):
    return ctx.log1p(ctx.people_sharing_link(post))
'''


# ---------- the checks ----------

def test_checks_pass_the_starter_feeds():
    for path in (settings.feeds_dir).glob("*.py"):
        assert feedcheck.check(path.read_text()).ok, path


@pytest.mark.parametrize("bad", [
    "import os\n", "import urllib.request\n", "x = open('f')\n", "eval('1')\n", "().__class__.__bases__\n",
    "from subprocess import run\n", "getattr(object, 'x')\n",
])
def test_checks_block_risky_code(bad):
    r = feedcheck.check(GOOD + bad)
    assert not r.ok


def test_checks_warn_about_personal_things():
    src = GOOD.replace('"mastodon:home"', '"bluesky:list:at://did:plc:abc123/app.bsky.graph.list/3kq"')
    r = feedcheck.check(src + '\nFRIEND = "alice.bsky.social"\n')
    assert r.ok
    texts = " ".join(i.text for i in r.warnings)
    assert "lists" in texts and "handle" in texts


def test_checks_need_feed_and_score():
    assert not feedcheck.check("x = 1\n").ok
    assert not feedcheck.check("def score(:\n").ok
    r = feedcheck.check(GOOD)
    assert r.ok and r.name == "Morning reading" and r.sources == ["bluesky:following", "mastodon:home"]


# ---------- the directory's store ----------

@pytest.fixture
def st(tmp_path):
    s = Store(tmp_path / "d.db")
    s.seed(settings.feeds_dir, "AndreasThinks")
    return s


def test_seed_lists_starter_feeds_once(st, tmp_path):
    n = len(st.feeds())
    assert n >= 4 and all(f["credit"] == "AndreasThinks" for f in st.feeds())
    st.seed(settings.feeds_dir)
    assert len(st.feeds()) == n


def test_submit_review_and_update(st):
    receipt = "r" * 40
    s = st.submit(receipt, "Morning reading", "", "", GOOD)
    assert s["status"] == "pending" and st.queue()[0]["id"] == s["id"]
    assert st.mine([receipt])[0]["status"] == "pending"
    assert st.mine(["x" * 40]) == []
    with pytest.raises(Rejected):
        st.review(s["id"], "changes", "")                  # asking for changes needs a note
    done = st.review(s["id"], "approve")
    assert done["status"] == "approved" and done["slug"] == "morning-reading"
    f = st.feed("morning-reading")
    assert f["version"] == 1 and f["credit"] == ""
    # only the same receipt can update it
    with pytest.raises(Rejected):
        st.submit("y" * 40, "Morning reading", "", "", GOOD, slug="morning-reading")
    up = st.submit(receipt, "Morning reading", "", "Andreas", GOOD + "\n# tweak\n", slug="morning-reading")
    st.review(up["id"], "approve")
    f = st.feed("morning-reading")
    assert f["version"] == 2 and f["credit"] == "Andreas" and [v["version"] for v in st.versions("morning-reading")] == [2, 1]


def test_submit_rejects_failing_code_and_short_receipts(st):
    with pytest.raises(Rejected):
        st.submit("r" * 40, "Bad", "", "", GOOD + "import os\n")
    with pytest.raises(Rejected):
        st.submit("short", "Good", "", "", GOOD)


def test_resend_replaces_the_open_submission(st):
    a = st.submit("r" * 40, "Morning reading", "", "", GOOD)
    st.review(a["id"], "changes", "Drop X please")
    b = st.submit("r" * 40, "Morning reading", "", "", GOOD + "\n")
    assert st.submission(a["id"])["status"] == "withdrawn"
    assert [s["id"] for s in st.queue()] == [b["id"]]


def test_downloads_count(st):
    slug = st.feeds()[0]["slug"]
    for _ in range(3):
        st.count_download(slug)
    assert st.feed(slug)["downloads"] == 3 and st.total_downloads() == 3


def _report(slug, **kw):
    item = {"feed": slug, "on_home": True, "installed_for": "30d+", "visits": 4, "posts_read": 100,
            "posts_liked_or_replied": 3, "tab_position": 2}
    item.update(kw)
    return {"week": dstore.week_of(), "feeds": [item]}


def test_stats_hidden_until_enough_reports(st):
    slug = st.feeds()[0]["slug"]
    for _ in range(dstore.MIN_REPORTS - 1):
        st.record_report(_report(slug))
    assert st.stats(slug) is None
    st.record_report(_report(slug, installed_for="0-7d", tab_position=1))
    s = st.stats(slug)
    assert s["reports"] == dstore.MIN_REPORTS and s["users"] == dstore.MIN_REPORTS
    assert s["long_term_share"] == pytest.approx(0.9)
    assert s["read_per_visit"] == pytest.approx(25)
    assert s["positions"][0] == pytest.approx(0.1) and s["positions"][1] == pytest.approx(0.9)


@pytest.mark.parametrize("bad", [
    {"week": "2001-W01", "feeds": []},
    {"week": dstore.week_of(), "feeds": [], "device": "abc"},
    {"week": dstore.week_of(), "feeds": [{"feed": "x", "handle": "me"}]},
])
def test_reports_are_strict(st, bad):
    with pytest.raises(Rejected):
        st.record_report(bad)


def test_reports_are_not_stored(st):
    slug = st.feeds()[0]["slug"]
    st.record_report(_report(slug))
    tables = {r["name"] for r in st.q("select name from sqlite_master where type='table'")}
    assert tables - {"sqlite_sequence"} == {"feeds", "versions", "submissions", "downloads", "weekly", "kv"}
    assert st.one("select reports from weekly")["reports"] == 1


# ---------- the app talking to the directory ----------

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The app pointed at an in-process directory, with its own feeds folder and a fresh directory database."""
    from crossposter.app import db
    import directory.app as dapp
    fresh = Store(tmp_path / "dir.db")
    fresh.seed(settings.feeds_dir, "AndreasThinks")
    monkeypatch.setattr(dapp, "store", fresh)
    feeds_dir = tmp_path / "feeds"
    feeds_dir.mkdir()
    monkeypatch.setattr(settings, "feeds_dir", feeds_dir)
    monkeypatch.setattr(settings, "directory_url", "http://directory")
    monkeypatch.setattr(community, "_client", httpx.AsyncClient(transport=httpx.ASGITransport(app=dapp.app),
                                                                base_url="http://directory"))
    monkeypatch.setattr(community, "_list_cache", None)
    for k in (community.INSTALLED, community.CHOICE, community.COUNTS, community.RECEIPTS, community.SENT, "stats_sent_week"):
        db.x("delete from kv where key=?", (k,))
    from crossposter.app import app
    with TestClient(app, base_url="https://testserver") as c:
        yield c, db, fresh, feeds_dir


def test_browse_add_and_the_stats_question(wired):
    c, db, st, feeds_dir = wired
    r = c.get("/feeds/community")
    assert r.status_code == 200 and "Quiet posters" in r.text and "You have this" in r.text   # the starter copy
    r = c.get("/feeds/community/quiet-posters")
    assert "Add to Home" in r.text and "Runs on this device" in r.text
    r = c.post("/feeds/community/quiet-posters/add", follow_redirects=False)
    assert r.headers["location"].startswith("/feeds/stats-question")
    assert (feeds_dir / "quiet_posters.py").is_file() and st.feed("quiet-posters")["downloads"] == 1
    page = c.get(r.headers["location"]).text
    assert 'type="radio"' in page and "checked" not in page          # neither answer is picked
    r = c.post("/feeds/stats-question", data={"next": "/?custom=quiet_posters"}, follow_redirects=False)
    assert "error" in r.headers["location"] and community.choice(db) is None
    r = c.post("/feeds/stats-question", data={"choice": "yes", "next": "/?custom=quiet_posters"}, follow_redirects=False)
    assert r.headers["location"] == "/?custom=quiet_posters" and community.choice(db) == "yes"
    # asked once: adding another goes straight to it
    r = c.post("/feeds/community/links-worth-reading/add", follow_redirects=False)
    assert r.headers["location"] == "/?custom=links_and_reading"     # the starter copy you already have


def test_counts_only_when_on_and_report_shape(wired):
    c, db, st, feeds_dir = wired
    c.post("/feeds/community/quiet-posters/add")
    community.record_shown(db, "quiet_posters", ["a|1", "a|2"], True)
    assert db.get(community.COUNTS, {}) == {}                       # not chosen: nothing counted
    community.set_choice(db, "yes")
    community.record_shown(db, "quiet_posters", ["a|1", "a|2"], True)
    community.record_shown(db, "quiet_posters", ["a|3"], False)
    community.record_action(db, "a|2")
    community.record_shown(db, "my_own_feed", ["b|1"], True)        # not from the directory: never counted
    rep = community.build_report(db, community.week_of(), {"quiet_posters": 3})
    assert rep == {"week": community.week_of(), "feeds": [{
        "feed": "quiet-posters", "on_home": True, "installed_for": "0-7d", "visits": 1, "posts_read": 3,
        "posts_liked_or_replied": 1, "tab_position": 3}]}
    st.record_report(rep)                                           # the directory accepts exactly this shape
    community.set_choice(db, "no")
    assert db.get(community.COUNTS) == {}


def test_send_due_sends_last_week_once(wired):
    import asyncio
    c, db, st, feeds_dir = wired
    c.post("/feeds/community/quiet-posters/add")
    community.set_choice(db, "yes")
    last = community.week_of(time.time() - 7 * 86400)
    db.set(community.COUNTS, {last: {"quiet-posters": {"visits": 2, "posts_read": 40, "acted": 1}}})
    assert asyncio.run(community.send_due(db, {"quiet_posters": 1})) is True
    assert st.one("select reports, visits from weekly where week=?", (last,))["visits"] == 2
    assert asyncio.run(community.send_due(db, {"quiet_posters": 1})) is False
    assert db.get(community.SENT)["report"]["week"] == last


def test_share_and_follow_a_submission(wired):
    c, db, st, feeds_dir = wired
    (feeds_dir / "morning.py").write_text(GOOD)
    page = c.get("/feeds/morning/share").text
    assert "Send for review" in page and "Passed" in page
    r = c.post("/feeds/morning/share", data={"name": "Morning reading", "description": "Long reads.", "credit": ""},
               follow_redirects=False)
    assert "error" in r.headers["location"]                         # licence box not ticked
    r = c.post("/feeds/morning/share", data={"name": "Morning reading", "description": "Long reads.", "licence": "on"},
               follow_redirects=False)
    assert r.headers["location"].startswith("/feeds/submissions")
    assert "In review" in c.get("/feeds/submissions").text
    sid = st.queue()[0]["id"]
    st.review(sid, "changes", "Please add a description")
    page = c.get("/feeds/submissions").text
    assert "Changes asked" in page and "Please add a description" in page
    c.post("/feeds/morning/share", data={"name": "Morning reading", "description": "Long reads.", "licence": "on"})
    st.review(st.queue()[0]["id"], "approve")
    assert "Listed" in c.get("/feeds/submissions").text
    assert "update to your listed feed" in c.get("/feeds/morning/share").text


def test_privacy_page_shows_the_report(wired):
    c, db, st, feeds_dir = wired
    c.post("/feeds/community/quiet-posters/add")
    page = c.get("/settings/privacy").text
    assert "quiet-posters" in page and "Turn on" in page
    c.post("/settings/privacy", data={"choice": "yes"})
    assert community.choice(db) == "yes" and "Turn off" in c.get("/settings/privacy").text


def test_directory_pages_and_review_queue(wired, monkeypatch):
    import directory.app as dapp
    c, db, st, feeds_dir = wired
    monkeypatch.setattr(dapp, "ADMIN_PASSWORD", "pw")
    with TestClient(dapp.app, base_url="https://testserver") as d:
        for path in ("/", "/download", "/feeds", "/feeds/quiet-posters", "/privacy", "/feeds/share",
                     "/feeds?sort=kept&net=across&q=quiet", "/healthz"):
            assert d.get(path).status_code == 200, path
        for old, new in (("/data", "/privacy"), ("/write", "/feeds/share")):    # old addresses still work
            r = d.get(old, follow_redirects=False)
            assert r.status_code == 301 and r.headers["location"] == new
        page = d.get("/feeds/quiet-posters").text
        assert "/web/feeds/community/quiet-posters" in page                     # opens the feed in the browser version
        assert "Rarity" in page and "People who rarely post" in page             # score parts and docstring, in words
        assert d.get("/review", follow_redirects=False).status_code == 303
        st.submit("r" * 40, "Morning reading", "", "", GOOD)
        d.post("/login", data={"password": "pw"})
        page = d.get("/review").text
        assert "Morning reading" in page and "Approve and list" in page
        sid = st.queue()[0]["id"]
        d.post(f"/review/{sid}", data={"decision": "approve", "note": ""})
        assert st.feed("morning-reading") and "Morning reading" in d.get("/feeds").text
        r = d.post("/api/report", json={"week": "2001-W01", "feeds": []})
        assert r.status_code == 400
