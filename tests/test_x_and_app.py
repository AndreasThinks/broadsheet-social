import os

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
    for path in ["/", "/timeline?custom=combined", "/timeline?custom=popular_in_my_network", "/compose",
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
