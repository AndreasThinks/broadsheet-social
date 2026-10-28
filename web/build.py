"""Build the browser version's files into web/dist: the app bundle, the extra wheels, and build.json.

    python -m web.build

Run when the home page's image is built (directory/Dockerfile). The page at /web reads build.json to know what to load.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
PYODIDE = "0.29.5"
# Pyodide's own builds of what the app needs (and their dependencies, which Pyodide resolves).
PACKAGES = ["cryptography", "pillow", "beautifulsoup4", "python-dateutil", "sqlite3", "httpx", "anyio",
            "ssl", "typing-extensions"]


def bundle() -> bytes:
    """The crossposter package, the app's static files and the starter feeds, as one zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        files = [*sorted((ROOT / "crossposter").rglob("*.py")), *sorted((ROOT / "feeds").glob("*.py")),
                 *sorted(p for p in (ROOT / "static").rglob("*") if p.is_file())]
        for f in files:
            info = zipfile.ZipInfo(str(f.relative_to(ROOT)), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return buf.getvalue()


def main() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    wheels = DIST / "wheels"
    wheels.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--only-binary", ":all:",
                    "--dest", str(wheels), "-r", str(ROOT / "web" / "requirements.txt")], check=True)
    data = bundle()
    name = f"app-{hashlib.sha256(data).hexdigest()[:12]}.zip"
    (DIST / name).write_bytes(data)
    build = {"pyodide": PYODIDE, "packages": PACKAGES, "bundle": name,
             "wheels": sorted(p.name for p in wheels.glob("*.whl"))}
    (DIST / "build.json").write_text(json.dumps(build, indent=2))
    print(f"web/dist: {name} ({len(data) // 1024} KB), {len(build['wheels'])} wheels")


if __name__ == "__main__":
    main()
