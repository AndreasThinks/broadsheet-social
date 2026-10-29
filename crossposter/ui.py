"""FastHTML components: post cards, account chips, layout."""
from __future__ import annotations

import json
import time
from html import escape
from datetime import datetime
from urllib.parse import quote, urlencode

from fasthtml.common import (A, Article, Aside, Button, Details, Div, Footer, Header, Img, Li, Main, Nav, P, Script,
                             Section, Small, Span, Strong, Summary, Title, Ul, Video, NotStr)

from .models import Account, Notification, Post, Segment

NETWORK_LABELS = {"bluesky": "Bluesky", "mastodon": "Mastodon", "x": "X"}

ICONS = {
    "home": '<path d="M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "compose": '<path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4"/>',
    "bell": '<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 20a2 2 0 0 0 4 0"/>',
    "feeds": '<path d="M4 6h16M4 12h10M4 18h6M17 15l3 3-3 3"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    "reply": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12z"/>',
    "repost": '<path d="M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3"/>',
    "like": '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "grip": '<circle cx="9" cy="6" r="1"/><circle cx="15" cy="6" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="18" r="1"/><circle cx="15" cy="18" r="1"/>',
    "why": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17.5v.01"/>',
}


def icon(name: str, cls: str = "icon") -> NotStr:
    return NotStr(f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
                  f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def safe(url: str | None) -> str:
    """Only http(s) and in-app links; anything else (javascript:, data:) becomes inert."""
    url = (url or "").strip()
    return url if url.startswith(("https://", "http://", "/")) else "#"


def ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 7:
        return f"{int(d // 86400)}d"
    return datetime.fromtimestamp(ts).strftime("%-d %b")


def badge(network: str):
    return Span(NETWORK_LABELS.get(network, network), cls=f"badge badge-{network}", title=NETWORK_LABELS.get(network, network))


def profile_url(network: str, handle: str, account_id: str = "") -> str:
    url = f"/profile/{network}/{quote(handle)}"
    return url + ("?" + urlencode({"acct": account_id}) if account_id else "")


def thread_url(post: Post) -> str:
    return "/thread?" + urlencode({"key": post.key})


def avatar(acct: Account, size: str = "md"):
    if acct.avatar:
        return Img(src=acct.avatar, alt="", cls=f"avatar avatar-{size}", loading="lazy")
    initial = (acct.display_name or acct.handle or "?")[:1].upper()
    return Span(initial, cls=f"avatar avatar-{size} avatar-blank", aria_hidden="true")


def account_chip(acct: Account, account_id: str = ""):
    return A(avatar(acct, "sm"), Span(Strong(acct.name), " ", Small(acct.at_handle)), cls="chip",
             href=profile_url(acct.network, acct.handle, account_id))


def rich_text(segments: list[Segment], network: str, account_id: str):
    # Built as one HTML string: the text is shown with white-space: pre-wrap, so the
    # indentation FastHTML adds between child elements would appear as stray blank lines.
    parts = []
    for s in segments:
        text = escape(s.text)
        if s.kind == "link":
            parts.append(f'<a href="{escape(safe(s.href))}" target="_blank" rel="noopener noreferrer" class="link">{text}</a>')
        elif s.kind == "mention":
            href = profile_url(network, s.target or s.text.lstrip("@"), account_id)
            parts.append(f'<a href="{escape(href)}" class="mention">{text}</a>')
        elif s.kind == "tag":
            if network == "mastodon":
                href = f"/?feed=mastodon:tag:{quote(s.target)}&acct={quote(account_id)}"
                parts.append(f'<a href="{escape(href)}" class="tag">{text}</a>')
            else:
                parts.append(f'<a href="{escape(safe(s.href))}" target="_blank" rel="noopener" class="tag">{text}</a>')
        else:
            parts.append(text)
    return NotStr(f'<div class="text">{"".join(parts)}</div>')


VIEWABLE = ("image", "video", "gif")
MAX_TILES = 4


def media_grid(post: Post):
    """Up to four tiles; every picture and video opens in the in-app viewer (static/app.js), which also
    reaches the ones past the fourth. Without scripts, pictures still open in a new tab."""
    if not post.media:
        return None
    viewable = [m for m in post.media if m.kind in VIEWABLE]
    listing = [{"kind": m.kind, "url": safe(m.url), "thumb": safe(m.thumb) if m.thumb else "", "alt": m.alt,
                "stream": safe(m.stream) if m.stream else ""} for m in viewable]
    extra = len(viewable) - MAX_TILES
    items = []
    for i, m in enumerate(viewable[:MAX_TILES]):
        marks = [Span("ALT", cls="media-badge") if m.alt else None,
                 Span(f"+{extra}", cls="media-more") if extra > 0 and i == MAX_TILES - 1 else None]
        if m.kind == "image":
            ratio = f"aspect-ratio:{m.width}/{m.height}" if m.width and m.height and len(viewable) == 1 else ""
            label = f"Picture {i + 1} of {len(viewable)}" + (f": {m.alt}" if m.alt else "")
            items.append(A(Img(src=m.thumb or m.url, alt=m.alt, loading="lazy", style=ratio), *marks,
                           href=safe(m.url), target="_blank", rel="noopener", cls="media-item", data_index=str(i),
                           aria_label=label))
            continue
        attrs = {"preload": "none", "poster": m.thumb, "playsinline": True, "muted": True}
        if m.kind == "gif":
            attrs.update(autoplay=True, loop=True)
            controls = [Span("GIF", cls="media-badge media-badge-top"),
                        Button(cls="media-pause", type="button", aria_label="Pause")]
        else:
            attrs.update(controls=True)
            controls = []
        player = (Video(cls="hls", data_src=m.stream, aria_label=m.alt or "video", **attrs) if m.stream
                  else Video(src=m.url, aria_label=m.alt or "video", **attrs))
        items.append(Div(player, *controls, *marks,
                         Button(cls="media-open", type="button", aria_label="Open full size"),
                         cls=f"media-item media-{m.kind}", data_index=str(i)))
    for m in post.media:
        if m.kind not in VIEWABLE:
            items.append(A("Audio attachment", href=safe(m.url), target="_blank", rel="noopener", cls="media-item"))
    return Div(*items, cls=f"media media-{min(len(items), MAX_TILES)}", data_media=json.dumps(listing))


def link_card(post: Post):
    c = post.card
    if not c:
        return None
    host = c.url.split("/")[2] if c.url.count("/") >= 2 else c.url
    return A(Img(src=c.image, alt="", loading="lazy") if c.image else None,
             Div(Small(host), Strong(c.title or c.url), P(c.description) if c.description else None, cls="card-body"),
             href=safe(c.url), target="_blank", rel="noopener noreferrer", cls="card" + (" card-img" if c.image else ""))


def quote_card(post: Post):
    q = post.quote
    if not q:
        return None
    return A(Div(avatar(q.author, "xs"), Strong(q.author.name), Small(" ", q.author.at_handle, " · ", ago(q.created_at)),
                 cls="quote-head"),
             rich_text(q.text, q.network, q.account_id), media_grid(q), href=thread_url(q), cls="quote")


def action_bar(post: Post):
    k = urlencode({"key": post.key})
    return Footer(
        A(icon("reply"), Span(post.replies or ""), href=f"/compose?reply={quote(post.key)}", cls="act", title="Reply"),
        Button(icon("repost"), Span(post.reposts or ""), cls="act" + (" on-repost" if post.reposted else ""),
               hx_post=f"/act/repost?{k}", hx_target="closest footer", hx_swap="outerHTML", title="Repost"),
        Button(icon("like"), Span(post.likes or ""), cls="act" + (" on-like" if post.liked else ""),
               hx_post=f"/act/like?{k}", hx_target="closest footer", hx_swap="outerHTML", title="Like"),
        A(icon("external"), href=safe(post.url), target="_blank", rel="noopener", cls="act",
          title=f"Open on {NETWORK_LABELS.get(post.network, post.network)}"),
        cls="actions")


def why(terms: dict[str, float], total: float):
    if not terms:
        return Details(Summary(icon("why"), cls="why-btn", title="Why am I seeing this?"),
                       Div(f"Score {total:.3g}", cls="why-box"), cls="why")
    rows = [Li(Span(k), Span(f"{v:+.3g}")) for k, v in terms.items()]
    return Details(Summary(icon("why"), cls="why-btn", title="Why am I seeing this?"),
                   Div(Strong("Why this post"), Ul(*rows, Li(Strong("total"), Strong(f"{total:.3g}"))), cls="why-box"),
                   cls="why")


def parent_peek(parent: Post, href: str):
    """The post a reply answers, as a faded two-line preview joined to the reply by a thin line."""
    return A(Div(avatar(parent.author, "xs"), Span(cls="peek-line"), cls="peek-rail"),
             Div(Div(Strong(parent.author.name), " ", Span(parent.author.at_handle, cls="handle"), cls="peek-who"),
                 Div(parent.plain_text.strip() or "(no text)", cls="peek-text"), cls="peek-body"),
             href=href, cls="peek")


def replying_to(post: Post):
    if not post.reply_to_handle:
        return None
    return Div("Replying to ", A("@" + post.reply_to_handle,
                                 href=profile_url(post.network, post.reply_to_handle, post.account_id)), cls="context")


def reply_context(post: Post):
    """Above a reply in a feed: the parent itself if we have it, otherwise fetch it once the reply scrolls into view."""
    if post.parent:
        return parent_peek(post.parent, thread_url(post))
    if post.reply_to_id and post.network in ("bluesky", "mastodon"):
        q = urlencode({"key": f"{post.account_id}|{post.reply_to_id}", "child": post.key})
        return Div(replying_to(post), hx_get=f"/peek?{q}", hx_trigger="revealed", hx_swap="outerHTML", cls="peek-wait")
    return replying_to(post)


def post_card(post: Post, score: tuple[float, dict] | None = None, focus: bool = False, in_thread: bool = False,
              op: bool = False):
    a = post.author
    nets = [post.network] + [o["network"] for o in post.also_on]
    on = " and ".join(NETWORK_LABELS.get(n, n) for n in dict.fromkeys(nets))
    head = Header(
        A(avatar(a, "sm"), href=profile_url(a.network, a.handle, post.account_id), cls="avatar-link"),
        Div(Div(A(Strong(a.name), href=profile_url(a.network, a.handle, post.account_id), cls="name"),
                Span("Started it", cls="op", title="Wrote the first post in this conversation") if op else None,
                cls="name-row"),
            Div(Span(a.at_handle, cls="handle"), f" · on {on} · ",
                A(ago(post.created_at), href=thread_url(post), cls="time",
                  title=datetime.fromtimestamp(post.created_at).strftime("%d %b %Y %H:%M")), cls="byline"),
            cls="who"),
        Div(why(score[1], score[0]), cls="meta") if score else None)
    lines = []
    boosted = post.extra.get("boosted_by") or []
    if post.reposted_by or boosted:
        who = post.reposted_by.name if post.reposted_by else boosted[0]
        more = len(set(boosted)) - 1 if boosted else 0
        lines.append(Div(icon("repost", "icon sm"), f" {who} reposted" + (f" (+{more})" if more > 0 else ""), cls="context"))
    if post.is_reply and not in_thread and not focus:
        lines.append(reply_context(post))
    elif post.reply_to_handle and not in_thread and not focus:
        lines.append(replying_to(post))
    body = [rich_text(post.text, post.network, post.account_id), media_grid(post), link_card(post), quote_card(post)]
    if post.content_warning or post.sensitive:
        body = [Details(Summary(post.content_warning or "Sensitive content", cls="cw"), *body)]
    convo = None
    if not in_thread and not focus and post.is_reply:
        label = "Show conversation" + (f" · {post.replies} repl{'y' if post.replies == 1 else 'ies'}" if post.replies else "")
        convo = A(label, Span("›", aria_hidden="true"), href=thread_url(post), cls="convo-link")
    return Article(*lines, head, *body, convo, action_bar(post),
                   cls="post" + (" focus" if focus else "") + (" in-thread" if in_thread else ""),
                   data_href=None if focus else thread_url(post), id="p-" + str(abs(hash(post.key))))


THREAD_DEPTH = 3       # deeper replies are listed flat under the third level
SHOW_TOP_REPLIES = 20  # direct replies shown before folding the rest
SHOW_SUB_REPLIES = 2   # replies shown under each reply before folding the rest


def conversation(ancestors: list[Post], post: Post, replies: list[Post], reply_button=None):
    """A conversation read top to bottom: what led up to the post, the post, then replies nested under what they answer."""
    root = (ancestors[0] if ancestors else post).author.handle
    ids = {r.id for r in replies}
    kids: dict[str, list[Post]] = {}
    for r in replies:
        pid = r.reply_to_id if r.reply_to_id in ids and r.reply_to_id != r.id else post.id
        kids.setdefault(pid, []).append(r)
    seen: set[str] = set()

    def card(p: Post):
        seen.add(p.id)
        return post_card(p, in_thread=True, op=p.author.handle == root and p is not (ancestors[0] if ancestors else post))

    def descendants(pid: str) -> list[Post]:
        out = []
        for k in kids.get(pid, []):
            if k.id not in seen and k not in out:
                out.append(k)
                out.extend(d for d in descendants(k.id) if d not in out)
        return out

    def level(pid: str, depth: int, show: int) -> tuple[list, int]:
        """The replies to pid as components, and how many posts they hold in total."""
        units, sizes = [], []
        for k in kids.get(pid, []):
            if k.id in seen:
                continue
            if depth >= THREAD_DEPTH:
                flat = [card(k)] + [card(d) for d in descendants(k.id)]
                units.append(Div(*flat, cls="reply-unit"))
                sizes.append(len(flat))
            else:
                first = card(k)
                sub, n = level(k.id, depth + 1, SHOW_SUB_REPLIES)
                units.append(Div(first, Div(*sub, cls="branch") if sub else None, cls="reply-unit"))
                sizes.append(1 + n)
        total = sum(sizes)
        if len(units) > show:
            hidden = sum(sizes[show:])
            units = units[:show] + [Details(Summary(f"Show {hidden} more repl{'y' if hidden == 1 else 'ies'}",
                                                    cls="fold-btn"), *units[show:], cls="fold")]
        return units, total

    focus = post_card(post, focus=True)
    top, _ = level(post.id, 1, SHOW_TOP_REPLIES)
    n = len(replies)
    if n:
        sep = Div(f"{n} repl{'y' if n == 1 else 'ies'}", cls="thread-sep")
    elif post.network == "x":
        sep = Div(A("See replies on X", href=safe(post.url), target="_blank", rel="noopener"), cls="thread-sep")
    else:
        sep = Div("No replies yet", cls="thread-sep")
    return Div(Div(*[card(p) for p in ancestors], cls="chain") if ancestors else None,
               focus, reply_button, sep, Div(*top, cls="replies"), cls="conversation")


def load_more(url: str | None):
    if not url:
        return Div("You're all caught up.", cls="end")
    return Div(Span(cls="spinner"), "Loading", hx_get=url, hx_trigger="revealed", hx_swap="outerHTML", cls="more")


def error_box(msg: str):
    return Div(Strong("Something went wrong. "), msg, cls="error", role="alert")


def notification_row(n: Notification):
    from .push import VERBS
    a = n.actor
    target = None
    if n.post:
        target = A(n.post.plain_text[:280] or "(no text)", href=thread_url(n.post), cls="notif-text")
    return Article(
        Div(avatar(a, "sm"), cls="notif-avatar"),
        Div(Div(A(Strong(a.name), href=profile_url(a.network, a.handle, n.account_id)), f" {VERBS.get(n.kind, '')}",
                Span(badge(n.network), Small(" ", ago(n.created_at)), cls="notif-meta")),
            target, cls="notif-body"),
        cls=f"notif notif-{n.kind}")


APP_NAME = "Broadsheet"

# (href, label, extra class). "Write" is the button at the right of the masthead, and sits in the middle on a phone.
NAV = [("/", "Read", ""), ("/notifications", "Activity", ""), ("/compose", "Write", " write"), ("/feeds", "Feeds", ""),
       ("/settings", "Settings", "")]


def page(title: str, *content, active: str = "/", unread: int = 0, wide: bool = False, aside=None):
    nav = Nav(*[A(label, Span(str(unread), cls="dot", aria_label=f"{unread} new") if (href == "/notifications" and unread)
                  else None, href=href, cls="nav-item" + extra + (" active" if href == active else ""),
                  aria_current="page" if href == active else None)
                for href, label, extra in NAV], cls="nav", aria_label="Sections")
    return (Title(f"{title} · {APP_NAME}"),
            Header(Div(A(APP_NAME, href="/", cls="brand"), nav, cls="masthead-inner"), cls="masthead"),
            Div(Main(*content, cls="main" + (" wide" if wide else "")),
                Aside(*aside, cls="aside") if aside else None, cls="layout"))
