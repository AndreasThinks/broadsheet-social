import os
import re

import pytest
from starlette.testclient import TestClient

from crossposter.adapters import NetworkError
from crossposter.adapters import x as xapi
from crossposter.config import settings
from crossposter.db import DB


def test_x_usage_and_cap():
    db = DB(":memory:")
    xapi.record_usage(db, post_reads=100, link_posts=1)
    u = xapi.usage(db)
    assert u["cost"] == pytest.approx(100 * 0.005 + 0.2)
    old = settings.x_monthly_cap_usd
    settings.x_monthly_cap_usd = 1.0
    try:
        xapi.check_budget(db, post_reads=10)
        with pytest.raises(NetworkError):
            xapi.check_budget(db, post_reads=200)
    finally:
        settings.x_monthly_cap_usd = old


@pytest.fixture(scope="module")
def client():
    from crossposter.app import app
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_pages_render_in_demo_mode(client):
    for path in ["/", "/timeline?custom=combined", "/timeline?custom=popular_in_my_network",
                 "/timeline?custom=across_networks", "/feeds/new/edit?start=across_networks", "/compose",
                 "/notifications", "/feeds", "/settings", "/feeds/new/edit",
                 "/timeline?feed=bluesky:following&acct=demo-bluesky", "/thread?key=demo-mastodon%7C1",
                 "/profile/bluesky/ada.bsky.social?acct=demo-bluesky"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Something went wrong" not in r.text, path


def test_pwa_and_oauth_documents(client):
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    sw = client.get("/sw.js")
    assert sw.headers["service-worker-allowed"] == "/" and "showNotification" in sw.text
    assert client.get("/oauth/bluesky/client-metadata.json").json()["token_endpoint_auth_method"] == "private_key_jwt"
    assert client.get("/oauth/bluesky/jwks.json").json()["keys"]


def test_working_directory_files_are_not_served(client):
    assert client.get("/requirements.txt").status_code == 404
    assert client.get("/static/../requirements.txt").status_code == 404


def test_compose_posts_to_demo_accounts(client):
    r = client.post("/compose", data={"text": "hello both", "accounts": ["demo-bluesky", "demo-mastodon"], "thread": "on"})
    assert r.status_code == 200 and "Posted." in r.text


def test_feed_preview(client):
    code = ('from crossposter.feeds import Feed\nfeed = Feed(name="t", sources=["bluesky:following"])\n'
            'def score(post, ctx):\n    return post.likes\n')
    r = client.post("/feeds/preview", data={"code": code})
    assert r.status_code == 200 and "candidates" in r.text
    r = client.post("/feeds/preview", data={"code": "feed = 1"})
    assert "must define" in r.text


def test_feed_order_and_visibility(client):
    from crossposter.app import db
    try:
        r = client.post("/feeds/quiet_posters/visibility")
        assert r.status_code == 200 and "Show on Home" in r.text
        assert 'href="/?custom=quiet_posters"' not in client.get("/").text
        assert 'href="/?custom=quiet_posters"' in client.get("/?custom=quiet_posters").text
        client.post("/feeds/quiet_posters/visibility")
        assert 'href="/?custom=quiet_posters"' in client.get("/").text

        page = client.get("/feeds/arrange").text
        keys = re.findall(r'data-key="([^"]+)"', page)
        assert "across_networks" in keys and any(k.startswith("native|") for k in keys)
        # Put a network's own feed first, Across networks before Combined, and show an unpinned native feed.
        native = next(k for k in keys if k.startswith("native|") and k.endswith("bluesky:following"))
        discover = next(k for k in keys if k.endswith("bluesky:discover"))
        order = [native, "across_networks", "combined"] + [k for k in keys if k not in (native, "across_networks", "combined")]
        r = client.post("/feeds/arrange", json={"tabs": [{"key": k, "visible": k != "quiet_posters" and
                                                          (k == discover or not k.startswith("native|") or k == native)}
                                                         for k in order]})
        assert r.json() == {"ok": True}
        home = client.get("/").text
        assert home.index("bluesky%3Afollowing") < home.index("?custom=across_networks") < home.index("?custom=combined")
        assert "bluesky%3Adiscover" in home and "mastodon%3Ahome" not in home
        assert 'href="/?custom=quiet_posters"' not in home
        assert 'data-refresh="/timeline?custom=combined&amp;fresh=1"' in home
        assert "feed-bar" in home
    finally:
        db.set("feed_prefs", {})


def test_password_protects_app(client):
    settings.app_password = "pw"
    try:
        client.cookies.clear()
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert client.get("/oauth/bluesky/client-metadata.json").status_code == 200
        r = client.post("/login", data={"password": "pw"}, follow_redirects=False)
        assert r.headers["location"] == "/"
        assert client.get("/", follow_redirects=False).status_code == 200
    finally:
        settings.app_password = ""


def test_desktop_token_login(client):
    settings.app_password = "launch-token"
    try:
        client.cookies.clear()
        # Off unless the desktop launcher started the app.
        r = client.get("/login/desktop?token=launch-token", follow_redirects=False)
        assert r.headers["location"] == "/login"
        settings.desktop = True
        assert client.get("/login/desktop?token=wrong", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/", follow_redirects=False).status_code == 303
        r = client.get("/login/desktop?token=launch-token", follow_redirects=False)
        assert r.headers["location"] == "/"
        assert client.get("/", follow_redirects=False).status_code == 200
    finally:
        settings.app_password = ""
        settings.desktop = False
        client.cookies.clear()
