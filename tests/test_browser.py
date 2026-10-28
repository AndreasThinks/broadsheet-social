"""The browser version: the request bridge, backups, Bluesky's public client, Mastodon push, and the files /web serves."""
import asyncio
import io
import json
import zipfile

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from crossposter import browser
from crossposter.adapters import bluesky_oauth as oauth
from crossposter.adapters import mastodon
from crossposter.adapters.base import AccountRecord
from crossposter.config import settings
from crossposter.db import DB


def test_handle_answers_like_a_server(monkeypatch):
    from crossposter import app as appmod
    monkeypatch.setattr(browser, "_app", appmod.app)
    status, headers, body = asyncio.run(browser.handle("GET", "https://cross.example.com/settings", [], None))
    assert status == 200 and b"Settings" in body
    # Forms arrive without Content-Length (the service worker can't see it); the bridge adds it so parsing works.
    boundary = "----b"
    form = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"muted\"\r\n\r\nsomeone.bsky.social\r\n"
            f"--{boundary}--\r\n").encode()
    status, headers, _ = asyncio.run(browser.handle(
        "POST", "https://cross.example.com/settings/muted",
        [("content-type", f"multipart/form-data; boundary={boundary}")], form))
    assert status == 303 and ("location", "/settings?ok=Saved") in headers
    assert appmod.db.get("muted") == ["someone.bsky.social"]
    appmod.db.set("muted", [])


def test_backup_round_trip_and_erase(tmp_path):
    data = tmp_path / "data"
    (data / "feeds").mkdir(parents=True)
    (data / ".secret").write_text("s3cret")
    (data / "feeds" / "mine.py").write_text("# my feed")
    db = DB(data / "crossposter.db", wal=False)
    db.set("muted", ["a"])
    blob = browser.export_zip(db, data)
    assert set(zipfile.ZipFile(io.BytesIO(blob)).namelist()) == {"crossposter.db", ".secret", "feeds/mine.py"}

    browser.erase(db, data)
    assert db.get("muted") is None and not (data / ".secret").exists()

    browser.import_zip(db, data, blob)
    assert db.get("muted") == ["a"]
    assert (data / ".secret").read_text() == "s3cret" and (data / "feeds" / "mine.py").exists()


def test_restore_refuses_other_files(tmp_path):
    db = DB(tmp_path / "crossposter.db", wal=False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "hello")
    with pytest.raises(ValueError):
        browser.import_zip(db, tmp_path, buf.getvalue())


def test_bluesky_signs_in_as_a_public_client_in_the_browser(monkeypatch):
    monkeypatch.setattr(settings, "browser", True)
    monkeypatch.setattr(settings, "base_url", "https://site.example/web")
    assert not oauth.is_confidential()
    assert oauth.client_id() == "https://site.example/web/oauth/bluesky/client-metadata.json"
    assert oauth.client_assertion(None, "https://bsky.social") == {}
    meta = oauth.client_metadata()
    assert meta["token_endpoint_auth_method"] == "none" and "jwks_uri" not in meta
    assert meta["redirect_uris"] == ["https://site.example/web/auth/bluesky/callback"]


@respx.mock
def test_mastodon_pushes_straight_to_the_browser():
    route = respx.post("https://social.example/api/v1/push/subscription").mock(return_value=httpx.Response(200, json={}))
    rec = AccountRecord(id="mastodon:1@social.example", network="mastodon", native_id="1", handle="me@social.example",
                        display_name="", avatar="", server="https://social.example", tokens={"access_token": "t"})
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    asyncio.run(mastodon.subscribe_push(rec, sub, ["reply", "like"]))
    sent = json.loads(route.calls[0].request.content)
    assert sent["subscription"]["endpoint"] == sub["endpoint"] and sent["subscription"]["standard"] is True
    assert sent["data"]["alerts"] == {"mention": True, "favourite": True}
    assert route.calls[0].request.headers["authorization"] == "Bearer t"


@respx.mock
def test_mastodon_push_key_falls_back_to_instance_info(tmp_path):
    db = DB(tmp_path / "db.sqlite")
    respx.get("https://social.example/api/v2/instance").mock(
        return_value=httpx.Response(200, json={"configuration": {"vapid": {"public_key": "BKEY"}}}))
    assert asyncio.run(mastodon.vapid_key(db, "https://social.example")) == "BKEY"
    assert db.get("mastodon_vapid|social.example") == "BKEY"


@pytest.fixture
def site():
    import directory.app as dapp
    with TestClient(dapp.app, base_url="https://site.example") as c:
        yield c, dapp


def test_site_serves_the_browser_version(site):
    c, dapp = site
    for path in ("/web", "/web/settings", "/web/auth/bluesky/callback?code=x&state=y"):
        r = c.get(path)
        assert r.status_code == 200 and "/web-files/shell.js" in r.text
    sw = c.get("/web-files/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert c.get("/web-files/worker.js").status_code == 200
    assert c.get("/web-files/build.py").status_code == 404           # source isn't served
    assert c.get("/web-files/../directory/app.py").status_code == 404
    assert "/web" in c.get("/").text


def test_site_and_app_agree_on_the_bluesky_client(site, monkeypatch):
    c, dapp = site
    served = c.get("/web/oauth/bluesky/client-metadata.json").json()
    assert served == oauth.browser_client_metadata("https://site.example/web")
    assert served["client_id"] == "https://site.example/web/oauth/bluesky/client-metadata.json"
