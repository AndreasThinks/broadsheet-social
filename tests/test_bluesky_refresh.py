"""Bluesky refresh tokens are single use: concurrent adapters must not spend one twice or save a stale copy."""
import asyncio
import time

import httpx
import respx
from joserfc.jwk import ECKey

from crossposter.adapters.base import AccountRecord, SignInExpired, save_account
from crossposter.adapters.bluesky import BlueskyAdapter
from crossposter.db import DB

PDS = "https://pds.example"
AS = "https://auth.example"
ID = "bluesky:did:plc:abc"


def _setup(expires_at):
    db = DB(":memory:")
    key = ECKey.generate_key("P-256", private=True)
    rec = AccountRecord(id=ID, network="bluesky", native_id="did:plc:abc", handle="ada.test", display_name="",
                        avatar="", server=PDS, tokens={
                            "access_token": "at1", "refresh_token": "rt1", "expires_at": expires_at,
                            "dpop_jwk": key.as_dict(private=True), "issuer": AS, "token_endpoint": f"{AS}/token"})
    save_account(db, rec)
    return db


def _fresh_adapter(db):
    from crossposter.adapters import accounts
    return next(a for a in accounts(db) if a.account_id == ID)


def _profile(request):
    return httpx.Response(200, json={"did": "did:plc:abc", "handle": "ada.test"})


def test_two_adapters_refresh_once_and_reuse_the_new_token():
    db = _setup(time.time() - 10)
    a, b = _fresh_adapter(db), _fresh_adapter(db)  # both loaded rt1 before either refreshed
    used = []

    def token(request):
        rt = dict(x.split("=", 1) for x in request.content.decode().split("&"))["refresh_token"]
        used.append(rt)
        if rt != "rt1":
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"})
        return httpx.Response(200, json={"access_token": "at2", "refresh_token": "rt2", "expires_in": 3600})

    async def both():
        return await asyncio.gather(a.profile("ada.test"), b.profile("ada.test"))

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{AS}/token").mock(side_effect=token)
        router.get(f"{PDS}/xrpc/app.bsky.actor.getProfile").mock(side_effect=_profile)
        asyncio.run(both())
    assert used == ["rt1"]
    assert _fresh_adapter(db).rec.tokens["refresh_token"] == "rt2"


def test_nonce_update_from_a_stale_adapter_keeps_the_new_refresh_token():
    db = _setup(time.time() + 3600)
    stale = _fresh_adapter(db)
    other = _fresh_adapter(db)
    other.save_tokens({**other.rec.tokens, "access_token": "at2", "refresh_token": "rt2"})  # someone refreshed

    with respx.mock as router:
        router.get(f"{PDS}/xrpc/app.bsky.actor.getProfile").respond(
            json={"did": "did:plc:abc", "handle": "ada.test"}, headers={"DPoP-Nonce": "n2"})
        asyncio.run(stale.profile("ada.test"))
    saved = _fresh_adapter(db).rec.tokens
    assert saved["refresh_token"] == "rt2" and saved["pds_nonce"] == "n2"


def test_dead_refresh_token_asks_for_reconnect_without_retrying():
    db = _setup(time.time() - 10)
    calls = []

    def token(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid refresh token"})

    with respx.mock as router:
        router.post(f"{AS}/token").mock(side_effect=token)
        for _ in range(2):
            try:
                asyncio.run(_fresh_adapter(db).profile("ada.test"))
            except SignInExpired as e:
                assert "Reconnect Bluesky" in str(e)
            else:
                raise AssertionError("expected SignInExpired")
    assert len(calls) == 1  # the second attempt didn't hit the server again
    assert _fresh_adapter(db).needs_reconnect


def test_server_error_on_refresh_is_not_treated_as_expired():
    db = _setup(time.time() - 10)
    with respx.mock as router:
        router.post(f"{AS}/token").respond(500, json={"error": "server_error"})
        try:
            asyncio.run(_fresh_adapter(db).profile("ada.test"))
        except SignInExpired:
            raise AssertionError("a server hiccup shouldn't ask for a reconnect")
        except Exception:
            pass
    assert not _fresh_adapter(db).needs_reconnect
