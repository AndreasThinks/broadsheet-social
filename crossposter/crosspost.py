"""Cross-posting: copy a post you saw on one network onto your accounts on the others.

The composer is filled in with the post's text, a line linking back to the original, and its pictures or
video with their alt text, so you can choose where it goes and edit it before it's sent.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from .adapters.base import Upload, http
from .adapters import NETWORK_LABELS
from .models import Post
from .richtext import length_for

MAX_MEDIA_BYTES = 100 * 1024 * 1024
_BSKY_VIDEO = re.compile(r"/watch/([^/]+)/([^/]+)/playlist\.m3u8")
_DEFAULT_MIME = {"image": "image/jpeg", "gif": "video/mp4", "video": "video/mp4", "audio": "audio/mpeg"}


def body_text(post: Post) -> str:
    """The post's words, with full links (not the shortened text networks display) and full @handles."""
    out = []
    for s in post.text:
        if s.kind == "link" and s.href.startswith(("https://", "http://")):
            out.append(s.href)
        elif s.kind == "mention" and s.target and not s.target.startswith("did:"):
            out.append("@" + s.target.lstrip("@"))
        else:
            out.append(s.text)
    return "".join(out).strip()


def attribution(post: Post, mine: bool) -> str:
    """The line that links back to the original. Uses the author's name rather than their @handle, so it never
    tags a stranger who happens to have the same handle on another network."""
    net = NETWORK_LABELS.get(post.network, post.network)
    if mine:
        return f"Originally on {net}: {post.url}"
    who = (post.author.display_name or post.author.handle).strip()
    return f"Via {who} on {net}: {post.url}"


def compose(body: str, attr: str) -> str:
    return f"{body}\n\n{attr}" if body else attr


def fit(body: str, attr: str, network: str, limit: int) -> str | None:
    """A version that fits in limit, cutting words (never the link back), or None if the whole thing fits."""
    if length_for(network, compose(body, attr)) <= limit:
        return None
    words = body.split(" ")
    while words:
        words.pop()
        cut = " ".join(words).rstrip(" ,.;:-\n")
        if cut and length_for(network, compose(cut + "…", attr)) <= limit:
            return compose(cut + "…", attr)
    return attr


def is_mine(post: Post, accts) -> bool:
    return any(a.network == post.network and a.rec.handle.lower() == post.author.handle.lower() for a in accts)


def default_networks_off(post: Post) -> set[str]:
    """Networks the post is already on, so they start unticked."""
    return {post.network} | {o.get("network", "") for o in post.also_on}


def can_take(network: str, mime: str) -> bool:
    """Whether Broadsheet can put this kind of file on that network."""
    if mime.startswith("image/"):
        return True
    if mime.startswith("video/"):
        return network in ("mastodon", "bluesky")
    if mime.startswith("audio/"):
        return network == "mastodon"
    return False


def uploads_for(network: str, uploads: list[Upload]) -> tuple[list[Upload], int]:
    """The files a network gets, and how many it can't take. A post carries up to four pictures or one video."""
    usable = [u for u in uploads if can_take(network, u.mime)]
    if usable and not usable[0].mime.startswith("image/"):
        usable = usable[:1]
    else:
        usable = [u for u in usable if u.mime.startswith("image/")][:4]
    return usable, len(uploads) - len(usable)


async def _bluesky_video_url(url: str) -> str | None:
    """Bluesky shows videos as an HLS playlist; the original file is a blob on the author's server."""
    m = _BSKY_VIDEO.search(url)
    if not m:
        return None
    from .adapters import bluesky_oauth as oauth
    did, cid = unquote(m.group(1)), m.group(2)
    pds = oauth.pds_from_doc(await oauth.resolve_did(did))
    return f"{pds.rstrip('/')}/xrpc/com.atproto.sync.getBlob?did={did}&cid={cid}"


async def fetch_media(post: Post, chosen: list[tuple[int, str]]) -> tuple[list[Upload], list[str]]:
    """Download the chosen pictures and videos of a post, with the alt text given for each.
    Returns the uploads and a note for each one that couldn't be copied."""
    uploads, problems = [], []
    for i, alt in chosen:
        if not 0 <= i < len(post.media):
            continue
        m = post.media[i]
        what = "picture" if m.kind == "image" else m.kind
        try:
            url = m.url
            if post.network == "bluesky" and m.kind == "video":
                url = await _bluesky_video_url(m.stream or m.url) or ""
            if not url.startswith(("https://", "http://")):
                raise ValueError("no file to copy")
            r = await http().get(url)
            if r.status_code != 200:
                raise ValueError(f"the server said {r.status_code}")
            if len(r.content) > MAX_MEDIA_BYTES:
                raise ValueError("it's over 100 MB")
            mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not mime.startswith(("image/", "video/", "audio/")):
                mime = _DEFAULT_MIME.get(m.kind, "image/jpeg")
            ext = mime.split("/")[1].replace("jpeg", "jpg").replace("quicktime", "mov")
            uploads.append(Upload(data=r.content, mime=mime, alt=alt, filename=f"{what}-{i + 1}.{ext}"))
        except Exception as e:  # noqa: BLE001 - one missing picture shouldn't stop the post
            problems.append(f"The {what} {i + 1} couldn't be copied ({e}).")
    return uploads, problems
