"""AT Protocol OAuth: PAR + PKCE + DPoP, written against Bluesky's OAuth client spec.

Two client modes:
- Confidential web client (the normal case on a public HTTPS URL). Our client_id is the URL of the
  metadata document served at /oauth/bluesky/client-metadata.json, and we authenticate to the token
  endpoint with a private_key_jwt signed by a key we publish at /oauth/bluesky/jwks.json.
- Loopback development client, used automatically when BASE_URL is http://127.0.0.1. No metadata to
  host, no client secret; sessions are shorter.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse

from joserfc import jwt
from joserfc.jwk import ECKey

from ..config import settings
from ..crypto import b64url, pkce_pair
from .base import NetworkError, http

SCOPE = "atproto transition:generic"
PUBLIC_API = "https://public.api.bsky.app"


def callback_url() -> str:
    return f"{settings.base_url}/auth/bluesky/callback"


def is_confidential() -> bool:
    return not settings.is_loopback


def client_id() -> str:
    if is_confidential():
        return f"{settings.base_url}/oauth/bluesky/client-metadata.json"
    return "http://localhost?" + urlencode({"redirect_uri": callback_url(), "scope": SCOPE})


def client_key(db) -> ECKey:
    """The confidential client's signing key, generated once and kept in the database."""
    data = db.get("bsky_client_jwk")
    if not data:
        key = ECKey.generate_key("P-256", private=True)
        data = key.as_dict(private=True)
        data["kid"] = f"cp-{int(time.time())}"
        db.set("bsky_client_jwk", data)
    return ECKey.import_key(data)


def jwks(db) -> dict:
    key = client_key(db)
    pub = key.as_dict(private=False)
    pub.update({"kid": key.kid, "use": "sig", "alg": "ES256"})
    return {"keys": [pub]}


def client_metadata() -> dict:
    return {
        "client_id": client_id(),
        "client_name": "Cross-poster",
        "client_uri": settings.base_url,
        "application_type": "web",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": [callback_url()],
        "scope": SCOPE,
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_signing_alg": "ES256",
        "jwks_uri": f"{settings.base_url}/oauth/bluesky/jwks.json",
        "dpop_bound_access_tokens": True,
    }


# ---------- identity ----------

async def resolve_handle(handle: str) -> str | None:
    handle = handle.lstrip("@").strip().lower()
    try:
        r = await http().get(f"{PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle", params={"handle": handle})
        if r.status_code == 200:
            return r.json()["did"]
    except Exception:
        pass
    try:
        r = await http().get(f"https://{handle}/.well-known/atproto-did")
        if r.status_code == 200 and r.text.strip().startswith("did:"):
            return r.text.strip()
    except Exception:
        pass
    return None


async def resolve_did(did: str) -> dict:
    if did.startswith("did:plc:"):
        r = await http().get(f"https://plc.directory/{did}")
    elif did.startswith("did:web:"):
        r = await http().get(f"https://{did.removeprefix('did:web:')}/.well-known/did.json")
    else:
        raise NetworkError(f"Unsupported DID method: {did}")
    if r.status_code != 200:
        raise NetworkError(f"Couldn't resolve {did}.")
    return r.json()


def pds_from_doc(doc: dict) -> str:
    for svc in doc.get("service") or []:
        if svc.get("id", "").endswith("#atproto_pds"):
            return svc["serviceEndpoint"].rstrip("/")
    raise NetworkError("That account has no PDS listed in its DID document.")


def handle_from_doc(doc: dict) -> str:
    for aka in doc.get("alsoKnownAs") or []:
        if aka.startswith("at://"):
            return aka.removeprefix("at://")
    return ""


async def auth_server_for(pds: str) -> dict:
    r = await http().get(f"{pds}/.well-known/oauth-protected-resource")
    if r.status_code != 200:
        raise NetworkError(f"{pds} doesn't support OAuth.")
    issuer = r.json()["authorization_servers"][0].rstrip("/")
    return await auth_server_meta(issuer)


async def auth_server_meta(issuer: str) -> dict:
    r = await http().get(f"{issuer}/.well-known/oauth-authorization-server")
    if r.status_code != 200:
        raise NetworkError(f"{issuer} has no OAuth metadata.")
    meta = r.json()
    if meta.get("issuer", "").rstrip("/") != issuer:
        raise NetworkError("OAuth issuer mismatch.")
    return meta


# ---------- JWTs ----------

def dpop_proof(key: ECKey, method: str, url: str, nonce: str | None = None, access_token: str | None = None) -> str:
    parsed = urlparse(url)
    htu = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    claims = {"jti": secrets.token_urlsafe(16), "htm": method.upper(), "htu": htu, "iat": int(time.time())}
    if nonce:
        claims["nonce"] = nonce
    if access_token:
        claims["ath"] = b64url(hashlib.sha256(access_token.encode()).digest())
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": key.as_dict(private=False)}
    return jwt.encode(header, claims, key)


def client_assertion(db, audience: str) -> dict:
    if not is_confidential():
        return {}
    key = client_key(db)
    now = int(time.time())
    token = jwt.encode({"alg": "ES256", "kid": key.kid},
                       {"iss": client_id(), "sub": client_id(), "aud": audience,
                        "jti": secrets.token_urlsafe(16), "iat": now, "exp": now + 60}, key)
    return {"client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": token}


async def auth_server_post(url: str, data: dict, key: ECKey, nonce: str | None) -> tuple[dict, str | None]:
    """POST to the authorization server with DPoP, retrying once with a fresh nonce."""
    for _ in range(2):
        r = await http().post(url, data=data, headers={"DPoP": dpop_proof(key, "POST", url, nonce)})
        nonce = r.headers.get("DPoP-Nonce", nonce)
        if r.status_code == 400 and r.json().get("error") == "use_dpop_nonce":
            continue
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = {"error": r.text[:200]}
            raise NetworkError(f"Bluesky sign-in failed: {err.get('error_description') or err.get('error')}")
        return r.json(), nonce
    raise NetworkError("Bluesky sign-in failed: DPoP nonce loop.")


# ---------- flow ----------

async def start_login(db, identifier: str) -> str:
    ident = identifier.strip().lstrip("@")
    did = handle = pds = None
    if ident.startswith(("https://", "http://")):
        meta = await auth_server_for(ident.rstrip("/"))
    else:
        if not ident:
            ident = "bsky.social"
        did = ident if ident.startswith("did:") else await resolve_handle(ident)
        if not did:
            raise NetworkError(f"Couldn't find the Bluesky handle {ident}.")
        doc = await resolve_did(did)
        pds = pds_from_doc(doc)
        handle = handle_from_doc(doc) or ident
        meta = await auth_server_for(pds)

    key = ECKey.generate_key("P-256", private=True)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    issuer = meta["issuer"].rstrip("/")
    body = {"response_type": "code", "code_challenge": challenge, "code_challenge_method": "S256",
            "client_id": client_id(), "state": state, "redirect_uri": callback_url(), "scope": SCOPE,
            **client_assertion(db, issuer)}
    if handle:
        body["login_hint"] = handle
    par, nonce = await auth_server_post(meta["pushed_authorization_request_endpoint"], body, key, None)
    db.put_state(state, "bluesky", {
        "did": did, "handle": handle, "pds": pds, "issuer": issuer,
        "token_endpoint": meta["token_endpoint"], "verifier": verifier,
        "dpop_jwk": key.as_dict(private=True), "dpop_nonce": nonce})
    return meta["authorization_endpoint"] + "?" + urlencode({"client_id": client_id(), "request_uri": par["request_uri"]})


async def finish_login(db, code: str, state: str, iss: str) -> dict:
    """Exchange the code; returns token data plus did, handle and pds."""
    st = db.pop_state(state, "bluesky")
    if not st:
        raise NetworkError("The sign-in link expired. Please try again.")
    if iss and iss.rstrip("/") != st["issuer"]:
        raise NetworkError("Sign-in came back from an unexpected server.")
    key = ECKey.import_key(st["dpop_jwk"])
    tok, nonce = await auth_server_post(st["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "code_verifier": st["verifier"],
        "redirect_uri": callback_url(), "client_id": client_id(), **client_assertion(db, st["issuer"])},
        key, st.get("dpop_nonce"))
    did = tok["sub"]
    if st.get("did") and did != st["did"]:
        raise NetworkError("Signed in as a different account than requested.")
    pds, handle = st.get("pds"), st.get("handle")
    if not pds or not st.get("did"):
        doc = await resolve_did(did)
        pds, handle = pds_from_doc(doc), handle_from_doc(doc)
        if (await auth_server_for(pds))["issuer"].rstrip("/") != st["issuer"]:
            raise NetworkError("That account's server doesn't trust this sign-in server.")
    return {"did": did, "handle": handle, "pds": pds, "tokens": {
        "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 300)), "issuer": st["issuer"],
        "token_endpoint": st["token_endpoint"], "dpop_jwk": st["dpop_jwk"], "as_nonce": nonce, "pds_nonce": None}}


async def refresh(db, tokens: dict) -> dict:
    if not tokens.get("refresh_token"):
        raise NetworkError("Bluesky session expired; please sign in again.")
    key = ECKey.import_key(tokens["dpop_jwk"])
    tok, nonce = await auth_server_post(tokens["token_endpoint"], {
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id(),
        **client_assertion(db, tokens["issuer"])}, key, tokens.get("as_nonce"))
    return {**tokens, "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", tokens["refresh_token"]),
            "expires_at": time.time() + int(tok.get("expires_in", 300)), "as_nonce": nonce}
