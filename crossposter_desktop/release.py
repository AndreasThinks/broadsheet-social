"""Build the pieces of a desktop release: the app-code bundle and the manifest the launcher checks.

The desktop app is a "shell" (Python, the libraries in requirements.txt and a window) plus the app code
(crossposter/, static/, feeds/). The shell changes rarely; the app code changes every time main does.
So each push to main publishes a small zip of the app code, and installed shells download it quietly
and switch to it on their next launch. A new shell is only needed when the libraries change, which the
`deps` hash below detects.

    python -m crossposter_desktop.release bundle --out dist     # used by CI
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_PARTS = ("crossposter", "static", "feeds")
DEPS_FILES = ("requirements.txt", "requirements-desktop.txt")
BUNDLE_NAME = "crossposter-app.zip"
REPO = "AndreasThinks/cross-poster-"
CHANNEL = "desktop-latest"
SITE = "https://andreasthinks.github.io/cross-poster-/"


def deps_hash(root: Path = ROOT) -> str:
    """Identifies the set of libraries a shell was built with. Comments and blank lines don't count."""
    h = hashlib.sha256(f"py{sys.version_info.major}.{sys.version_info.minor}\n".encode())
    for name in DEPS_FILES:
        path = root / name
        lines = path.read_text().splitlines() if path.exists() else []
        for line in sorted(l.split("#", 1)[0].strip() for l in lines):
            if line:
                h.update(line.encode() + b"\n")
    return h.hexdigest()


def build_number(root: Path = ROOT) -> int:
    """The commit time, so builds of later commits always compare higher."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=root, capture_output=True, text=True, check=True)
        return int(out.stdout.strip())
    except Exception:
        return int(time.time())


def commit(root: Path = ROOT) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def imported_modules(root: Path = ROOT) -> list[str]:
    """Top-level modules the app code imports, so a shell can tell whether it has them all."""
    names = set()
    for path in (root / "crossposter").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    names.discard("crossposter")
    names.discard("__future__")
    return sorted(names)


def build_info(root: Path = ROOT) -> dict:
    return {"build": build_number(root), "commit": commit(root), "deps": deps_hash(root),
            "modules": imported_modules(root)}


def _files(root: Path):
    for part in APP_PARTS:
        for path in sorted((root / part).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                yield path


def make_bundle(root: Path, out: Path, info: dict | None = None) -> dict:
    """Write the app-code zip and manifest.json into `out`, and return the manifest."""
    info = info or build_info(root)
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / BUNDLE_NAME
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for path in _files(root):
            z.write(path, path.relative_to(root).as_posix())
        z.writestr("build.json", json.dumps(info, indent=2))
    manifest = {**info, "bundle": BUNDLE_NAME, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "page": SITE}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle", help="write the app-code zip and manifest.json")
    b.add_argument("--out", type=Path, default=ROOT / "dist")
    i = sub.add_parser("info", help="write build.json for a shell build")
    i.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "bundle":
        print(json.dumps(make_bundle(ROOT, args.out), indent=2))
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(build_info(ROOT), indent=2))


if __name__ == "__main__":
    main()
