"""Keep the desktop app's code in step with main, without reinstalling.

On each launch the app runs the newest code it has: either the code it shipped with, or a newer bundle
it downloaded earlier into <data>/updates/ from Broadsheet's website. After the window is up it checks the release manifest, and
if there's newer code it downloads it for next time. If the new code needs libraries this shell doesn't
have, it reports that a new download of the app is needed instead.

A bundle that fails to start is marked bad and never picked again, so a broken push to main can't
leave the app unable to open.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .release import UPDATE_URL

log = logging.getLogger("crossposter.desktop")

DEFAULT_URL = UPDATE_URL  # published by .github/workflows/apps.yml
MAX_BUNDLE_BYTES = 50 * 1024 * 1024


def update_url(updates: Path | None = None) -> str:
    """Where to look: an override for testing, else where the last manifest said the site has moved to."""
    moved = _state(updates).get("update_url") if updates else None
    return os.environ.get("CROSSPOSTER_UPDATE_URL", moved or DEFAULT_URL).rstrip("/") + "/"


def read_info(code_dir: Path) -> dict:
    try:
        return json.loads((code_dir / "build.json").read_text())
    except Exception:
        return {"build": 0, "deps": "", "commit": "dev"}


def _state(updates: Path) -> dict:
    try:
        return json.loads((updates / "state.json").read_text())
    except Exception:
        return {}


def _save_state(updates: Path, state: dict) -> None:
    updates.mkdir(parents=True, exist_ok=True)
    tmp = updates / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(updates / "state.json")


def choose_code_dir(bundled: Path, updates: Path) -> Path:
    """The newest usable code: a downloaded bundle if it's newer than what shipped and not marked bad."""
    shipped = read_info(bundled)
    state = _state(updates)
    current = state.get("current")
    if current and current not in state.get("bad", []):
        path = updates / str(current)
        info = read_info(path)
        if info.get("build", 0) > shipped.get("build", 0) and info.get("deps") == shipped.get("deps"):
            return path
    return bundled


def mark_bad(updates: Path, code_dir: Path) -> None:
    state = _state(updates)
    name = code_dir.name
    state["bad"] = sorted(set(state.get("bad", [])) | {name})
    if state.get("current") == name:
        # Go back to the previous download if it's still there, otherwise to the built-in code.
        older = sorted((p.name for p in updates.iterdir() if p.is_dir() and p.name.isdigit()
                        and p.name not in state["bad"]), key=int, reverse=True)
        if older:
            state["current"] = older[0]
        else:
            state.pop("current")
    _save_state(updates, state)
    log.warning("update %s failed to start; it won't be used again", name)


@dataclass
class Result:
    status: str          # "current", "staged", "shell" (download a new app), or "error"
    detail: str = ""
    page: str = ""


def missing_modules(names: list[str]) -> list[str]:
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def _safe_extract(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.infolist():
            dest = (target / member.filename).resolve()
            if not str(dest).startswith(str(target.resolve()) + os.sep):
                raise ValueError(f"unsafe path in bundle: {member.filename}")
        z.extractall(target)


def check(bundled: Path, updates: Path, running: Path, client: httpx.Client | None = None) -> Result:
    """Look for newer code and stage it for the next launch. Never raises."""
    shipped = read_info(bundled)
    have = max(shipped.get("build", 0), read_info(running).get("build", 0))
    state = _state(updates)
    base = update_url(updates)
    own_client = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True)
    try:
        manifest = client.get(base + "manifest.json").raise_for_status().json()
        build = int(manifest["build"])
        moved = manifest.get("update_url")
        if moved and moved.startswith("https://") and moved.rstrip("/") + "/" != base:
            state["update_url"] = moved
            _save_state(updates, state)
        page = manifest.get("page", "")
        if build <= have or str(build) in state.get("bad", []):
            return Result("current")
        if manifest.get("deps") != shipped.get("deps"):
            return Result("shell", "This update needs a new version of the desktop app.", page)
        missing = missing_modules(manifest.get("modules", []))
        if missing:
            return Result("shell", "This update needs modules the app doesn't have: " + ", ".join(missing), page)
        if state.get("current") == str(build) and (updates / str(build)).is_dir():
            return Result("staged", f"build {build} is ready")

        bundle_url = base + manifest["bundle"]
        data = client.get(bundle_url).raise_for_status().content
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError("bundle is too large")
        if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise ValueError("bundle checksum doesn't match the manifest")
        tmp = updates / f"{build}.partial"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        _safe_extract(data, tmp)
        if read_info(tmp).get("build") != build:
            raise ValueError("bundle build number doesn't match the manifest")
        final = updates / str(build)
        shutil.rmtree(final, ignore_errors=True)
        tmp.rename(final)
        state["current"] = str(build)
        _save_state(updates, state)
        _prune(updates, keep={str(build), running.name})
        log.info("downloaded build %s (%s); it will be used next launch", build, manifest.get("commit", "?"))
        return Result("staged", f"build {build} is ready", page)
    except Exception as e:
        log.info("update check failed: %s", e)
        return Result("error", str(e))
    finally:
        if own_client:
            client.close()


def _prune(updates: Path, keep: set[str]) -> None:
    for path in updates.iterdir():
        if path.is_dir() and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)
