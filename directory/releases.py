"""The app downloads and the desktop app's updates, served from this site.

GitHub Actions builds the apps on every push to main (.github/workflows/apps.yml) and uploads them here: one
PUT /api/releases/{commit}/{name} per file, then POST /api/releases/{commit}/publish to switch over. Each call
carries a GitHub Actions OIDC token, which proves it comes from a workflow run on main of the Broadsheet repo,
so there's no shared password to keep. Files live in DATA_DIR/releases/<commit>/ and current.json names the
live release. Only the newest release is kept.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "broadsheet-releases"
REPOSITORY = os.environ.get("RELEASE_REPOSITORY", "AndreasThinks/cross-poster-")
OWNER_ID = os.environ.get("RELEASE_OWNER_ID", "3072143")  # survives the account being renamed and the name reused
REF = "refs/heads/main"

APPS = {"Broadsheet-windows.zip": "windows", "Broadsheet-macos.zip": "macos", "Broadsheet-android.apk": "android"}
DESKTOP = ("manifest.json", "crossposter-app.zip")
FILES = (*APPS, *DESKTOP)
MAX_BYTES = 400 * 1024 * 1024
COMMIT = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Releases:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def current(self) -> dict | None:
        try:
            info = json.loads((self.root / "current.json").read_text())
        except Exception:
            return None
        return info if (self.root / info["commit"]).is_dir() else None

    def path(self, name: str) -> Path | None:
        info = self.current()
        if not info or name not in FILES:
            return None
        path = self.root / info["commit"] / name
        return path if path.is_file() else None

    def _prune(self, keep: set[str]) -> None:
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in keep:
                shutil.rmtree(p, ignore_errors=True)

    async def receive(self, commit: str, name: str, chunks) -> int:
        """Store one uploaded file. Starting a new release clears out unfinished ones from earlier runs."""
        if name not in FILES:
            raise Refused(f"unexpected file {name}")
        cur = self.current()
        self._prune({commit} | ({cur["commit"]} if cur else set()))
        folder = self.root / commit
        folder.mkdir(exist_ok=True)
        tmp, size = folder / (name + ".partial"), 0
        with open(tmp, "wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_BYTES:
                    f.close()
                    tmp.unlink()
                    raise Refused("file is too large", 413)
                f.write(chunk)
        tmp.replace(folder / name)
        return size

    def publish(self, commit: str) -> dict:
        folder = self.root / commit
        missing = [n for n in FILES if not (folder / n).is_file()]
        if missing:
            raise Refused("missing " + ", ".join(missing))
        manifest = json.loads((folder / "manifest.json").read_text())
        bundle = (folder / manifest.get("bundle", "")).read_bytes() if manifest.get("bundle") in DESKTOP else b""
        if hashlib.sha256(bundle).hexdigest() != manifest.get("sha256"):
            raise Refused("the app-code bundle doesn't match its manifest")
        info = {"commit": commit, "published_at": time.time(), "build": manifest.get("build"),
                "sizes": {n: (folder / n).stat().st_size for n in FILES}}
        tmp = self.root / "current.json.tmp"
        tmp.write_text(json.dumps(info, indent=2))
        tmp.replace(self.root / "current.json")
        self._prune({commit})
        return info


# ---------- who may upload ----------

_jwks: dict = {"keys": None, "at": 0.0}


def _keyset(refresh: bool = False) -> KeySet:
    if refresh or _jwks["keys"] is None or time.time() - _jwks["at"] > 3600:
        data = httpx.get(ISSUER + "/.well-known/jwks", timeout=10).raise_for_status().json()
        _jwks.update(keys=KeySet.import_key_set(data), at=time.time())
    return _jwks["keys"]


def verify(authorization: str, commit: str) -> dict:
    """Check the bearer token is a GitHub Actions OIDC token from main of our repo, for this commit."""
    if not COMMIT.fullmatch(commit):
        raise Refused("commit must be a full 40-character SHA")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        raise Refused("sign in with a GitHub Actions token", 401)
    try:
        try:
            decoded = jwt.decode(token, _keyset(), algorithms=["RS256"])
        except JoseError:
            decoded = jwt.decode(token, _keyset(refresh=True), algorithms=["RS256"])  # GitHub rotated its keys
        jwt.JWTClaimsRegistry(iss={"essential": True, "value": ISSUER}, aud={"essential": True, "value": AUDIENCE},
                              exp={"essential": True}).validate(decoded.claims)
    except (JoseError, ValueError) as e:
        raise Refused(f"token not accepted: {e}", 401)
    except httpx.HTTPError as e:
        raise Refused(f"couldn't fetch GitHub's signing keys: {e}", 503)
    c = decoded.claims
    if (str(c.get("repository", "")).lower() != REPOSITORY.lower() or str(c.get("repository_owner_id")) != OWNER_ID
            or c.get("ref") != REF):
        raise Refused("only the main branch of the Broadsheet repo can publish", 403)
    if c.get("sha") != commit:
        raise Refused("the token is for a different commit", 403)
    return c
