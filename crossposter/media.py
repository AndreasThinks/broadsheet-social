"""Image preparation and link-preview fetching shared by the adapters."""
from __future__ import annotations

import io
import re
from html import unescape

from PIL import Image, ImageOps


def shrink_image(data: bytes, mime: str, max_bytes: int, max_side: int = 2000) -> tuple[bytes, str, tuple[int, int]]:
    """Return image bytes under max_bytes (re-encoding as JPEG if needed) and its (width, height)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    size = img.size
    if len(data) <= max_bytes and max(size) <= max_side and mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return data, mime, size
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.convert("RGBA").split()[-1])
        img = bg
    side = min(max_side, max(size))
    quality = 88
    while True:
        im = img.copy()
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= max_bytes or (side < 400 and quality <= 50):
            return out, "image/jpeg", im.size
        if quality > 60:
            quality -= 12
        else:
            side = int(side * 0.8)


_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:-]*)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def parse_og(html: str) -> dict:
    """Pull title, description and image from Open Graph / Twitter / plain meta tags."""
    found: dict[str, str] = {}
    for tag in _META.findall(html[:200_000]):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if name and content and name not in found:
            found[name] = unescape(content).strip()
    title = found.get("og:title") or found.get("twitter:title")
    if not title:
        m = _TITLE.search(html)
        title = unescape(m.group(1)).strip() if m else ""
    return {"title": title,
            "description": found.get("og:description") or found.get("twitter:description") or found.get("description", ""),
            "image": found.get("og:image") or found.get("twitter:image") or ""}
