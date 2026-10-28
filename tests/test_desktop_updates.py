import io
import json
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from crossposter_desktop import release, updater

ROOT = Path(__file__).resolve().parent.parent
URL = "https://updates.example.com/"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A shipped build at build 100, an empty updates folder, and a published bundle at build 200."""
    monkeypatch.setenv("CROSSPOSTER_UPDATE_URL", URL)
    shipped = release.build_info(ROOT) | {"build": 100, "commit": "old"}
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "build.json").write_text(json.dumps(shipped))
    out = tmp_path / "dist"
    manifest = release.make_bundle(ROOT, out, shipped | {"build": 200, "commit": "new"})
    return {"bundled": bundled, "updates": tmp_path / "data" / "updates", "out": out, "manifest": manifest,
            "shipped": shipped}


def serve(env, manifest=None, bundle=None):
    respx.get(URL + "manifest.json").respond(json=manifest or env["manifest"])
    respx.get(URL + release.BUNDLE_NAME).respond(content=bundle or (env["out"] / release.BUNDLE_NAME).read_bytes())


def test_bundle_contains_app_code(env):
    with zipfile.ZipFile(env["out"] / release.BUNDLE_NAME) as z:
        names = set(z.namelist())
    assert {"crossposter/app.py", "static/app.css", "build.json"} <= names
    assert not any("__pycache__" in n for n in names)
    assert "httpx" in env["manifest"]["modules"] and "crossposter" not in env["manifest"]["modules"]


@respx.mock
def test_downloads_newer_code_and_uses_it_next_launch(env):
    serve(env)
    b, u = env["bundled"], env["updates"]
    assert updater.choose_code_dir(b, u) == b
    result = updater.check(b, u, b)
    assert result.status == "staged"
    chosen = updater.choose_code_dir(b, u)
    assert chosen == u / "200" and (chosen / "crossposter" / "app.py").exists()
    # Already running the new build: nothing more to do.
    assert updater.check(b, u, chosen).status == "current"


@respx.mock
def test_new_libraries_need_a_new_app_download(env):
    serve(env, manifest=env["manifest"] | {"deps": "something-else"})
    result = updater.check(env["bundled"], env["updates"], env["bundled"])
    assert result.status == "shell" and result.page
    assert updater.choose_code_dir(env["bundled"], env["updates"]) == env["bundled"]


@respx.mock
def test_missing_module_needs_a_new_app_download(env):
    serve(env, manifest=env["manifest"] | {"modules": ["httpx", "no_such_module_xyz"]})
    result = updater.check(env["bundled"], env["updates"], env["bundled"])
    assert result.status == "shell" and "no_such_module_xyz" in result.detail


@respx.mock
def test_bad_checksum_is_rejected(env):
    serve(env, bundle=b"not the bundle")
    assert updater.check(env["bundled"], env["updates"], env["bundled"]).status == "error"
    assert updater.choose_code_dir(env["bundled"], env["updates"]) == env["bundled"]


@respx.mock
def test_zip_with_unsafe_paths_is_rejected(env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x = 1")
    data = buf.getvalue()
    import hashlib
    serve(env, manifest=env["manifest"] | {"sha256": hashlib.sha256(data).hexdigest()}, bundle=data)
    assert updater.check(env["bundled"], env["updates"], env["bundled"]).status == "error"
    assert not (env["updates"].parent / "evil.py").exists()


@respx.mock
def test_bad_build_falls_back_and_is_not_downloaded_again(env):
    serve(env)
    b, u = env["bundled"], env["updates"]
    updater.check(b, u, b)
    updater.mark_bad(u, updater.choose_code_dir(b, u))
    assert updater.choose_code_dir(b, u) == b
    assert updater.check(b, u, b).status == "current"


@respx.mock
def test_offline_is_harmless(env):
    respx.get(URL + "manifest.json").mock(side_effect=httpx.ConnectError("offline"))
    assert updater.check(env["bundled"], env["updates"], env["bundled"]).status == "error"


@respx.mock
def test_bad_build_goes_back_to_the_previous_download(env):
    b, u = env["bundled"], env["updates"]
    serve(env)
    updater.check(b, u, b)
    good = updater.choose_code_dir(b, u)
    newer = release.make_bundle(ROOT, env["out"], env["shipped"] | {"build": 300})
    respx.routes.clear()
    serve(env, manifest=newer)
    assert updater.check(b, u, good).status == "staged"
    broken = updater.choose_code_dir(b, u)
    assert broken.name == "300"
    updater.mark_bad(u, broken)
    assert updater.choose_code_dir(b, u) == good


@respx.mock
def test_follows_the_site_when_it_moves(env, monkeypatch):
    monkeypatch.delenv("CROSSPOSTER_UPDATE_URL")
    b, u = env["bundled"], env["updates"]
    new = "https://new.example.com/desktop/"
    respx.get(updater.DEFAULT_URL + "manifest.json").respond(json=env["manifest"] | {"update_url": new})
    respx.get(updater.DEFAULT_URL + release.BUNDLE_NAME).respond(content=(env["out"] / release.BUNDLE_NAME).read_bytes())
    assert updater.check(b, u, b).status == "staged"
    assert updater.update_url(u) == new
    respx.get(new + "manifest.json").respond(json=env["manifest"] | {"update_url": new})
    assert updater.check(b, u, updater.choose_code_dir(b, u)).status == "current"


def test_manifest_names_the_website(env):
    assert env["manifest"]["update_url"] == release.UPDATE_URL and "github.io" not in release.UPDATE_URL


def test_apps_from_the_github_pages_days_are_pointed_at_the_website(monkeypatch):
    from crossposter import config
    monkeypatch.setattr(config.settings, "desktop", True)
    monkeypatch.setenv("CROSSPOSTER_UPDATE_URL", "x")
    monkeypatch.delenv("CROSSPOSTER_UPDATE_URL")
    monkeypatch.setattr(updater, "DEFAULT_URL", "https://andreasthinks.github.io/cross-poster-/desktop/")
    config._point_old_desktop_apps_at_new_site()
    assert updater.update_url() == "https://broadsheet-feeds-production.up.railway.app/desktop/"

    monkeypatch.delenv("CROSSPOSTER_UPDATE_URL")
    monkeypatch.setattr(updater, "DEFAULT_URL", release.UPDATE_URL)   # a current app is left alone
    config._point_old_desktop_apps_at_new_site()
    assert "CROSSPOSTER_UPDATE_URL" not in __import__("os").environ
