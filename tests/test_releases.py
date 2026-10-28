"""The website's downloads: GitHub Actions uploads a release with an OIDC token, then the site serves it."""
import hashlib
import json
import time

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.testclient import TestClient

from directory import releases as rel

SHA = "a" * 40
KEY = RSAKey.generate_key(2048, parameters={"kid": "test"})


@pytest.fixture
def site(tmp_path, monkeypatch):
    import directory.app as dapp
    monkeypatch.setattr(dapp, "releases", rel.Releases(tmp_path / "releases"))
    monkeypatch.setattr(rel, "_keyset", lambda refresh=False: KeySet([KEY]))   # stands in for GitHub's published keys
    with TestClient(dapp.app, base_url="https://testserver") as c:
        yield c, dapp.releases


def token(**over):
    claims = {"iss": rel.ISSUER, "aud": rel.AUDIENCE, "exp": int(time.time()) + 300, "repository": rel.REPOSITORY,
              "repository_owner_id": rel.OWNER_ID, "ref": "refs/heads/main", "sha": SHA} | over
    return {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "test"}, claims, KEY)}


def files():
    bundle = b"PK app code"
    manifest = {"build": 123, "bundle": "crossposter-app.zip", "sha256": hashlib.sha256(bundle).hexdigest()}
    return {"Broadsheet-windows.zip": b"win", "Broadsheet-macos.zip": b"mac", "Broadsheet-android.apk": b"apk",
            "crossposter-app.zip": bundle, "manifest.json": json.dumps(manifest).encode()}


def upload(c, sha=SHA, **over):
    for name, data in files().items():
        assert c.put(f"/api/releases/{sha}/{name}", content=data, headers=token(sha=sha, **over)).status_code == 200


def test_publish_and_download(site):
    c, r = site
    assert c.get("/downloads/Broadsheet-android.apk").status_code == 404
    assert "Coming soon" in c.get("/download").text
    upload(c)
    assert c.get("/desktop/manifest.json").status_code == 404          # nothing is live until it's published
    assert c.post(f"/api/releases/{SHA}/publish", headers=token()).json()["build"] == 123
    apk = c.get("/downloads/Broadsheet-android.apk")
    assert apk.content == b"apk" and "attachment" in apk.headers["content-disposition"]
    assert c.get("/desktop/crossposter-app.zip").content == b"PK app code"
    assert c.get("/desktop/manifest.json").json()["build"] == 123
    assert c.get("/api/releases").json()["commit"] == SHA
    assert "/downloads/Broadsheet-windows.zip" in c.get("/").text
    assert "/downloads/Broadsheet-android.apk" in c.get("/download").text
    assert c.get("/downloads/manifest.json").status_code == 404

    # The next release replaces it, and the old files go.
    upload(c, sha="b" * 40)
    assert c.post(f"/api/releases/{'b' * 40}/publish", headers=token(sha="b" * 40)).status_code == 200
    assert sorted(p.name for p in r.root.iterdir()) == ["b" * 40, "current.json"]


@pytest.mark.parametrize("over,status", [
    ({"ref": "refs/heads/feature"}, 403),
    ({"repository": "someone/else"}, 403),
    ({"repository_owner_id": "1"}, 403),
    ({"sha": "c" * 40}, 403),
    ({"aud": "something-else"}, 401),
    ({"exp": int(time.time()) - 10}, 401),
])
def test_only_main_of_the_repo_can_upload(site, over, status):
    c, _ = site
    r = c.put(f"/api/releases/{SHA}/manifest.json", content=b"{}", headers=token(**over))
    assert r.status_code == status, r.text


def test_refuses_unsigned_strange_or_incomplete_uploads(site):
    c, _ = site
    assert c.put(f"/api/releases/{SHA}/manifest.json", content=b"{}").status_code == 401
    other = RSAKey.generate_key(2048, parameters={"kid": "test"})
    forged = jwt.encode({"alg": "RS256", "kid": "test"}, {"iss": rel.ISSUER, "aud": rel.AUDIENCE, "sha": SHA}, other)
    assert c.put(f"/api/releases/{SHA}/manifest.json", content=b"{}",
                 headers={"Authorization": "Bearer " + forged}).status_code == 401
    assert c.put(f"/api/releases/{SHA}/evil.sh", content=b"x", headers=token()).status_code == 400
    c.put(f"/api/releases/{SHA}/manifest.json", content=b"{}", headers=token())
    assert "missing" in c.post(f"/api/releases/{SHA}/publish", headers=token()).json()["error"]
