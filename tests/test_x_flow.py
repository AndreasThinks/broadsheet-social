"""X sign-in, token refresh and posting against a fake X API."""
import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from crossposter.adapters import AccountRecord, Upload, base
from crossposter.adapters import x as xapi
from crossposter.db import DB


class FakeX:
    def __init__(self):
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if path == "/2/oauth2/token":
            form = parse_qs(req.content.decode())
            n = len([c for c in self.calls if c.url.path == path])
            body = {"access_token": f"at{n}", "expires_in": 7200}
            if form["grant_type"] == ["authorization_code"]:
                body["refresh_token"] = "rt1"
            return httpx.Response(200, json=body)
        if path == "/2/users/me":
            return httpx.Response(200, json={"data": {"id": "42", "username": "ada", "name": "Ada"}})
        if path == "/2/media/upload":
            return httpx.Response(200, json={"data": {"id": "m1", "media_key": "3_m1"}})
        if path == "/2/media/metadata":
            return httpx.Response(200, json={"data": {}})
        if path == "/2/tweets":
            n = len([c for c in self.calls if c.url.path == path])
            return httpx.Response(201, json={"data": {"id": f"t{n}", "text": ""}})
        return httpx.Response(404, json={})

    def find(self, path):
        return [c for c in self.calls if c.url.path == path]


@pytest.fixture
def fake(monkeypatch):
    f = FakeX()
    monkeypatch.setattr(base, "_client", httpx.AsyncClient(transport=httpx.MockTransport(f)))
    return f


def test_credentials_saved_encrypted():
    db = DB(":memory:")
    assert xapi.client_id(db) == "" and xapi.client_secret(db) == ""
    xapi.save_credentials(db, " cid ", "shh")
    assert xapi.client_id(db) == "cid" and xapi.client_secret(db) == "shh"
    assert "shh" not in json.dumps(db.get("x_client_secret"))
    xapi.save_credentials(db, "cid", "")
    assert xapi.client_secret(db) == ""


def test_public_client_sign_in_refresh_and_post(fake):
    db = DB(":memory:")
    xapi.save_credentials(db, "native-id", "")
    url = urlparse(asyncio.run(xapi.start_login(db)))
    q = parse_qs(url.query)
    assert url.netloc == "x.com" and q["client_id"] == ["native-id"]
    assert q["redirect_uri"] == ["https://cross.example.com/auth/x/callback"]
    assert "media.write" in q["scope"][0] and "offline.access" in q["scope"][0]

    rec = asyncio.run(xapi.finish_login(db, "code", q["state"][0]))
    assert rec.id == "x:42" and rec.handle == "ada"
    token_req = fake.find("/2/oauth2/token")[0]
    assert "authorization" not in token_req.headers  # public client: no secret
    assert parse_qs(token_req.content.decode())["client_id"] == ["native-id"]

    row = db.one("select * from accounts where id='x:42'")
    adapter = xapi.XAdapter(AccountRecord.from_row(row), db)
    adapter.rec.tokens["expires_at"] = time.time() - 10
    results = asyncio.run(adapter.post("hello " * 60, [Upload(b"png", "image/png", alt="a cat")], thread=True))
    assert len(results) == 2 and results[0].url == "https://x.com/ada/status/t1"
    # Refreshed once, and kept the refresh token X didn't send back.
    assert len(fake.find("/2/oauth2/token")) == 2 and adapter.rec.tokens["refresh_token"] == "rt1"
    tweets = [json.loads(c.content) for c in fake.find("/2/tweets")]
    assert tweets[0]["media"] == {"media_ids": ["m1"]} and tweets[1]["reply"] == {"in_reply_to_tweet_id": "t1"}
    assert json.loads(fake.find("/2/media/metadata")[0].content)["metadata"]["alt_text"]["text"] == "a cat"
    assert xapi.usage(db)["posts"] == 2


def test_confidential_client_uses_basic_auth(fake):
    db = DB(":memory:")
    xapi.save_credentials(db, "web-id", "web-secret")
    q = parse_qs(urlparse(asyncio.run(xapi.start_login(db))).query)
    asyncio.run(xapi.finish_login(db, "code", q["state"][0]))
    assert fake.find("/2/oauth2/token")[0].headers["authorization"].startswith("Basic ")


def test_settings_page_takes_x_credentials():
    from crossposter.app import app, db
    with TestClient(app, base_url="https://testserver") as c:
        page = c.get("/settings").text
        assert "/auth/x/callback" in page and 'action="/settings/x/app"' in page
        r = c.post("/settings/x/app", data={"client_id": "abc", "client_secret": "s3"}, follow_redirects=False)
        assert r.status_code == 303
        assert xapi.client_id(db) == "abc" and xapi.client_secret(db) == "s3"
        c.post("/settings/x/app", data={"client_id": "abc2", "client_secret": ""})
        assert xapi.client_id(db) == "abc2" and xapi.client_secret(db) == "s3"  # empty keeps the saved secret
        assert 'href="/auth/x/start"' in c.get("/settings").text
        c.post("/settings/x/post", data={})
        assert not xapi.post_by_default(db)
        db.set("x_client_id", "")
        db.set("x_post_by_default", True)
