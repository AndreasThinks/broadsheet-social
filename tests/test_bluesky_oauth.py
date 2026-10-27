import asyncio
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from joserfc import jwt
from joserfc.jwk import ECKey

from crossposter.adapters import bluesky_oauth as oauth
from crossposter.adapters.base import AccountRecord
from crossposter.adapters.bluesky import BlueskyAdapter
from crossposter.crypto import b64url
from crossposter.db import DB

PDS = "https://pds.example"
AS = "https://auth.example"


def test_dpop_proof_claims():
    key = ECKey.generate_key("P-256", private=True)
    proof = oauth.dpop_proof(key, "post", "https://pds.example/xrpc/a.b?x=1", nonce="n1", access_token="tok")
    tok = jwt.decode(proof, key)
    assert tok.header["typ"] == "dpop+jwt" and "d" not in tok.header["jwk"]
    assert tok.claims["htm"] == "POST" and tok.claims["htu"] == "https://pds.example/xrpc/a.b"
    assert tok.claims["nonce"] == "n1"
    assert tok.claims["ath"] == b64url(hashlib.sha256(b"tok").digest())


def test_client_metadata_is_confidential_on_https():
    meta = oauth.client_metadata()
    assert meta["client_id"] == "https://cross.example.com/oauth/bluesky/client-metadata.json"
    assert meta["dpop_bound_access_tokens"] is True
    assert meta["redirect_uris"] == ["https://cross.example.com/auth/bluesky/callback"]
    db = DB(":memory:")
    keys = oauth.jwks(db)["keys"]
    assert keys[0]["kty"] == "EC" and "d" not in keys[0]


def _mock_discovery(router):
    router.get("https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle").respond(json={"did": "did:plc:abc"})
    router.get("https://plc.directory/did:plc:abc").respond(json={
        "alsoKnownAs": ["at://ada.test"], "service": [{"id": "#atproto_pds", "serviceEndpoint": PDS}]})
    router.get(f"{PDS}/.well-known/oauth-protected-resource").respond(json={"authorization_servers": [AS]})
    router.get(f"{AS}/.well-known/oauth-authorization-server").respond(json={
        "issuer": AS, "pushed_authorization_request_endpoint": f"{AS}/par",
        "authorization_endpoint": f"{AS}/authorize", "token_endpoint": f"{AS}/token"})


def test_full_login_flow_with_nonce_retry():
    db = DB(":memory:")
    calls = []

    def par(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "use_dpop_nonce"}, headers={"DPoP-Nonce": "as-nonce"})
        proof = request.headers["DPoP"]
        assert jwt.decode(proof, ECKey.import_key(json.loads(
            __import__("base64").urlsafe_b64decode(proof.split(".")[0] + "==").decode())["jwk"])).claims["nonce"] == "as-nonce"
        body = parse_qs(request.content.decode())
        assert body["login_hint"] == ["ada.test"]
        assert body["client_assertion_type"][0].endswith("jwt-bearer")
        return httpx.Response(201, json={"request_uri": "urn:req:1"}, headers={"DPoP-Nonce": "as-nonce"})

    with respx.mock(assert_all_called=False) as router:
        _mock_discovery(router)
        router.post(f"{AS}/par").mock(side_effect=par)
        url = asyncio.run(oauth.start_login(db, "@ada.test"))
        q = parse_qs(urlparse(url).query)
        assert url.startswith(f"{AS}/authorize") and q["request_uri"] == ["urn:req:1"]
        state = db.q("select state from oauth_states")[0]["state"]

        router.post(f"{AS}/token").respond(json={"access_token": "at1", "refresh_token": "rt1", "sub": "did:plc:abc",
                                                 "expires_in": 3600, "token_type": "DPoP"})
        data = asyncio.run(oauth.finish_login(db, "code1", state, AS))
    assert data["did"] == "did:plc:abc" and data["pds"] == PDS and data["handle"] == "ada.test"
    assert data["tokens"]["access_token"] == "at1"


def test_wrong_issuer_is_rejected():
    db = DB(":memory:")
    db.put_state("s1", "bluesky", {"issuer": AS})
    try:
        asyncio.run(oauth.finish_login(db, "c", "s1", "https://evil.example"))
    except Exception as e:
        assert "unexpected server" in str(e)
    else:
        raise AssertionError("expected failure")


def test_pds_request_retries_with_resource_nonce():
    db = DB(":memory:")
    key = ECKey.generate_key("P-256", private=True)
    import time
    rec = AccountRecord(id="bluesky:did:plc:abc", network="bluesky", native_id="did:plc:abc", handle="ada.test",
                        display_name="", avatar="", server=PDS, tokens={
                            "access_token": "at1", "refresh_token": "rt1", "expires_at": time.time() + 3600,
                            "dpop_jwk": key.as_dict(private=True), "issuer": AS, "token_endpoint": f"{AS}/token"})
    from crossposter.adapters.base import save_account
    save_account(db, rec)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "DPoP at1"
        if len(seen) == 1:
            return httpx.Response(401, headers={"WWW-Authenticate": 'DPoP error="use_dpop_nonce"', "DPoP-Nonce": "pds-n"},
                                  json={"error": "use_dpop_nonce"})
        return httpx.Response(200, json={"did": "did:plc:abc", "handle": "ada.test", "displayName": "Ada"})

    with respx.mock as router:
        router.get(f"{PDS}/xrpc/app.bsky.actor.getProfile").mock(side_effect=handler)
        prof = asyncio.run(BlueskyAdapter(rec, db).profile("ada.test"))
    assert prof.display_name == "Ada" and len(seen) == 2
    assert rec.tokens["pds_nonce"] == "pds-n"
