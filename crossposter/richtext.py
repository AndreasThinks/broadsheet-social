"""Convert each network's rich text into Segments, and build Bluesky facets for posting."""
from __future__ import annotations

import re
from typing import Callable

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import Segment

URL_RE = re.compile(r"https?://[^\s<>\"')\]]*[^\s<>\"')\].,;:!?]")
# A Bluesky-style mention: @ followed by a domain-like handle.
MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+)")
TAG_RE = re.compile(r"(?<![\w#&])#([^\s#.,;:!?()\[\]{}'\"<>]*[^\d\s#.,;:!?()\[\]{}'\"<>][^\s#.,;:!?()\[\]{}'\"<>]*)")


def merge(segments: list[Segment]) -> list[Segment]:
    """Join neighbouring plain-text runs."""
    out: list[Segment] = []
    for s in segments:
        if not s.text:
            continue
        if out and s.kind == "text" and out[-1].kind == "text":
            out[-1] = Segment("text", out[-1].text + s.text)
        else:
            out.append(s)
    return out


# ---------- Bluesky ----------

def bsky_segments(text: str, facets: list[dict] | None) -> list[Segment]:
    """Bluesky facets index UTF-8 bytes, not characters."""
    if not facets:
        return [Segment("text", text)] if text else []
    data = text.encode("utf-8")
    spans = []
    for f in facets:
        idx = f.get("index") or {}
        start, end = idx.get("byteStart"), idx.get("byteEnd")
        if start is None or end is None or not (0 <= start < end <= len(data)):
            continue
        feats = f.get("features") or []
        if not feats:
            continue
        spans.append((start, end, feats[0]))
    spans.sort(key=lambda s: s[0])
    out, pos = [], 0
    for start, end, feat in spans:
        if start < pos:
            continue  # overlapping facets: keep the first
        out.append(Segment("text", data[pos:start].decode("utf-8", "replace")))
        chunk = data[start:end].decode("utf-8", "replace")
        t = feat.get("$type", "")
        if t.endswith("#link"):
            out.append(Segment("link", chunk, href=feat.get("uri", "")))
        elif t.endswith("#mention"):
            did = feat.get("did", "")
            out.append(Segment("mention", chunk, href=f"https://bsky.app/profile/{did}", target=chunk.lstrip("@") or did))
        elif t.endswith("#tag"):
            tag = feat.get("tag", chunk.lstrip("#"))
            out.append(Segment("tag", chunk, href=f"https://bsky.app/hashtag/{tag}", target=tag))
        else:
            out.append(Segment("text", chunk))
        pos = end
    out.append(Segment("text", data[pos:].decode("utf-8", "replace")))
    return merge(out)


def _byte_range(text: str, start: int, end: int) -> dict:
    return {"byteStart": len(text[:start].encode("utf-8")), "byteEnd": len(text[:end].encode("utf-8"))}


def build_bsky_facets(text: str, resolve_handle: Callable[[str], str | None] | None = None) -> list[dict]:
    """Find links, mentions and hashtags in text and return Bluesky facets."""
    facets = []
    taken: list[tuple[int, int]] = []
    for m in URL_RE.finditer(text):
        facets.append({"index": _byte_range(text, m.start(), m.end()),
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}]})
        taken.append((m.start(), m.end()))

    def free(a, b):
        return all(b <= s or a >= e for s, e in taken)

    for m in MENTION_RE.finditer(text):
        if not free(m.start(), m.end()) or resolve_handle is None:
            continue
        did = resolve_handle(m.group(1))
        if did:
            facets.append({"index": _byte_range(text, m.start(), m.end()),
                           "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}]})
    for m in TAG_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        tag = m.group(1)
        if len(tag) > 64:
            continue
        facets.append({"index": _byte_range(text, m.start(), m.end()),
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}]})
    facets.sort(key=lambda f: f["index"]["byteStart"])
    return facets


# ---------- Mastodon ----------

def _link_text(a: Tag) -> str:
    """Mastodon hides the scheme and long tails of links in invisible spans."""
    parts, trimmed = [], False
    for child in a.descendants:
        if isinstance(child, NavigableString):
            parent = child.parent
            classes = (parent.get("class") or []) if isinstance(parent, Tag) else []
            if "invisible" in classes:
                continue
            if "ellipsis" in classes:
                trimmed = True
            parts.append(str(child))
    text = "".join(parts)
    return text + ("…" if trimmed else "")


def mastodon_segments(html: str, mentions: list[dict] | None = None) -> list[Segment]:
    """Turn Mastodon's status HTML into segments."""
    if not html:
        return []
    by_url = {m.get("url"): m for m in (mentions or [])}
    soup = BeautifulSoup(html, "html.parser")
    out: list[Segment] = []

    def walk(node, depth=0):
        for child in node.children:
            if isinstance(child, NavigableString):
                out.append(Segment("text", str(child)))
            elif isinstance(child, Tag):
                name = child.name
                if name == "br":
                    out.append(Segment("text", "\n"))
                elif name == "a":
                    href = child.get("href", "")
                    classes = child.get("class") or []
                    text = child.get_text()
                    if "mention" in classes and "hashtag" not in classes and text.startswith("@"):
                        m = by_url.get(href)
                        target = m.get("acct") if m else text.lstrip("@")
                        out.append(Segment("mention", text, href=href, target=target))
                    elif "hashtag" in classes or text.startswith("#"):
                        tag = text.lstrip("#")
                        out.append(Segment("tag", text, href=href, target=tag))
                    else:
                        out.append(Segment("link", _link_text(child), href=href))
                elif name in ("p", "blockquote", "li"):
                    if out and not out[-1].text.endswith("\n"):
                        out.append(Segment("text", "\n\n"))
                    walk(child, depth + 1)
                else:
                    walk(child, depth + 1)

    walk(soup)
    segs = merge(out)
    if segs and segs[0].kind == "text":
        segs[0] = Segment("text", segs[0].text.lstrip("\n"))
    return [s for s in segs if s.text]


# ---------- X ----------

def x_segments(text: str, entities: dict | None) -> list[Segment]:
    if not entities:
        return [Segment("text", text)] if text else []
    spans = []
    for u in entities.get("urls") or []:
        spans.append((u["start"], u["end"], Segment("link", u.get("display_url") or u.get("expanded_url", ""),
                                                    href=u.get("expanded_url") or u.get("url", ""))))
    for m in entities.get("mentions") or []:
        spans.append((m["start"], m["end"], Segment("mention", "@" + m["username"],
                                                    href=f"https://x.com/{m['username']}", target=m["username"])))
    for h in entities.get("hashtags") or []:
        spans.append((h["start"], h["end"], Segment("tag", "#" + h["tag"],
                                                    href=f"https://x.com/hashtag/{h['tag']}", target=h["tag"])))
    spans.sort(key=lambda s: s[0])
    out, pos = [], 0
    for start, end, seg in spans:
        if start < pos:
            continue
        out.append(Segment("text", text[pos:start]))
        out.append(seg)
        pos = end
    out.append(Segment("text", text[pos:]))
    return merge(out)


# ---------- Plain text (composer previews, demo data) ----------

def plain_segments(text: str) -> list[Segment]:
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(Segment("text", text[pos:m.start()]))
        out.append(Segment("link", m.group(0), href=m.group(0)))
        pos = m.end()
    out.append(Segment("text", text[pos:]))
    return merge(out)


# ---------- Length rules ----------

REMOTE_MENTION_RE = re.compile(r"(?<![\w@/])(@[A-Za-z0-9_]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def length_for(network: str, text: str) -> int:
    """Characters as each network counts them. Mastodon and X count every URL as 23, and Mastodon
    counts @alice@example.social as just @alice."""
    if network in ("mastodon", "x"):
        text = URL_RE.sub("x" * 23, text)
    if network == "mastodon":
        text = REMOTE_MENTION_RE.sub(r"\1", text)
    return len(text)


def split_for_thread(text: str, limit: int) -> list[str]:
    """Split long text into chunks at paragraph, sentence or word boundaries."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks, rest = [], text
    budget = limit - 6  # room for a " (1/3)" style counter
    while len(rest) > budget:
        window = rest[:budget]
        cut = max(window.rfind("\n\n"), -1)
        if cut < budget // 2:
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            cut = cut + 1 if cut >= budget // 2 else -1
        if cut < budget // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = budget
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    n = len(chunks)
    return [f"{c} ({i + 1}/{n})" for i, c in enumerate(chunks)]
