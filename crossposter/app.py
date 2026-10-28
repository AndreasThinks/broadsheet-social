"""The FastHTML app: routes for reading, posting, notifications, feeds, settings and sign-in."""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from fasthtml.common import (A, Beforeware, Blockquote, Button, Code, Details, Div, Fieldset, Form, Legend, H1, H2, H3, Hidden, Input, Label, Li,
                             Link, Meta, Ol, Option, P, Pre, Script, Section, Select, Small, Span, Strong, Summary, Textarea,
                             Ul, fast_app)
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import adapters as registry
from . import cache, community, feedcheck, feeds, people, push
from .adapters import NetworkError, Upload
from .adapters import bluesky as bsky
from .adapters import bluesky_oauth, mastodon, x as xapi
from .config import settings
from .crypto import app_secret
from .db import DB
from .models import Notification
from .richtext import length_for
from .ui import (NETWORK_LABELS, safe, account_chip, avatar, badge, error_box, icon, load_more, notification_row, page, post_card,
                 profile_url)

log = logging.getLogger("crossposter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
db = DB(settings.db_path, wal=not settings.browser)


def seed_feeds() -> None:
    """On a fresh volume, copy the starter feeds so there is something to edit."""
    bundled = ROOT / "feeds"
    target = settings.feeds_dir
    if target.resolve() == bundled.resolve():
        return
    target.mkdir(parents=True, exist_ok=True)
    if not any(target.glob("*.py")):
        for f in bundled.glob("*.py"):
            shutil.copy(f, target / f.name)


async def lifespan(app):
    seed_feeds()
    tasks = [asyncio.create_task(push.poller(db)), asyncio.create_task(community.stats_loop(db, tab_positions))]
    yield
    for task in tasks:
        task.cancel()


def auth_check(req, session):
    if not settings.app_password or session.get("auth"):
        return
    if req.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


hdrs = (
    Meta(name="theme-color", content="#ffffff", media="(prefers-color-scheme: light)"),
    Meta(name="theme-color", content="#121212", media="(prefers-color-scheme: dark)"),
    Meta(name="color-scheme", content="light dark"),
    Meta(name="apple-mobile-web-app-capable", content="yes"),
    Meta(name="mobile-web-app-capable", content="yes"),
    Meta(name="apple-mobile-web-app-status-bar-style", content="default"),
    Meta(name="apple-mobile-web-app-title", content="Broadsheet"),
    Link(rel="manifest", href="/manifest.webmanifest"),
    Link(rel="apple-touch-icon", href="/static/icons/apple-touch-icon.png"),
    Link(rel="icon", type="image/png", href="/static/icons/icon-192.png"),
    Link(rel="stylesheet", href="/static/app.css"),
    Script(src="/static/app.js", defer=True),
) + ((Meta(name="broadsheet-browser", content="1"), Script(src="/web-files/frame.js", defer=True)) if settings.browser else ())

app, rt = fast_app(
    pico=False, hdrs=hdrs, secret_key=app_secret(), lifespan=lifespan, title="Broadsheet",
    sess_https_only=settings.base_url.startswith("https://"), session_cookie="crossposter",
    before=Beforeware(auth_check, skip=[r"/login", r"/login/desktop", r"/static/.*", r"/oauth/bluesky/.*", r"/sw\.js",
                                         r"/manifest\.webmanifest", r"/favicon\.ico", r"/healthz"]),
)
# fast_app adds a catch-all that serves files from the working directory; it would shadow
# /sw.js and the Bluesky OAuth JSON documents, so drop it and serve /static explicitly below.
app.router.routes[:] = [r for r in app.router.routes if "{ext:static}" not in getattr(r, "path", "")]


# ---------- helpers ----------

def accounts():
    return registry.accounts(db)


def unread() -> int:
    seen = db.get("notif_seen_at", 0)
    row = db.one("select count(*) as n from notifications where created_at > ?", (seen,))
    return row["n"] if row else 0


def shell(title: str, *content, active: str = "/", wide: bool = False, aside=None):
    return page(title, *content, active=active, unread=unread(), wide=wide, aside=aside)


def home_aside(accts) -> list:
    """The column beside Home on wide screens: what's new since you last looked, and where you post."""
    n = unread()
    news = (P(f"{n} new {'notification' if n == 1 else 'notifications'} since you last looked."),
            A("See activity", href="/notifications")) if n else (P("Nothing new since you last looked."),)
    x_default = xapi.post_by_default(db)
    rows = [Li(Span(NETWORK_LABELS.get(a.network, a.network)),
               Span(a.rec.handle + ("" if a.network != "x" or x_default else " · off by default")))
            for a in accts]
    return [Section(H2("While you were away"), *news),
            Section(H3("Your accounts", cls="aside-label"), Ul(*rows, cls="aside-accounts"),
                    A("Manage", href="/settings#accounts"))]


async def native_feeds(a) -> list:
    """An account's native feeds, cached for an hour (it's a network call)."""
    key = f"feeds:{a.account_id}"
    cached = db.get(key)
    if cached and time.time() - cached["at"] < 3600:
        from .models import FeedInfo
        return [FeedInfo(**f) for f in cached["feeds"]]
    try:
        fs = await a.feeds()
    except Exception as e:
        log.warning("feeds for %s failed: %s", a.account_id, e)
        return []
    db.set(key, {"at": time.time(), "feeds": [f.__dict__ for f in fs]})
    return fs


def split_key(key: str) -> tuple[str, str]:
    account_id, _, post_id = key.partition("|")
    return account_id, post_id


# ---------- sign in to the app ----------

@rt("/login")
def get(error: str = ""):
    return (page("Sign in", Section(H1("Broadsheet"), P("Enter the app password to continue."),
                                    Form(Input(type="password", name="password", placeholder="App password",
                                               autofocus=True, required=True, autocomplete="current-password"),
                                         Button("Sign in", cls="primary"), method="post", action="/login", cls="stack"),
                                    P(error, cls="error") if error else None, cls="panel narrow")))


@rt("/login")
async def post(password: str, session):
    if settings.app_password and hmac.compare_digest(password.encode(), settings.app_password.encode()):
        session["auth"] = True
        return RedirectResponse("/", status_code=303)
    await asyncio.sleep(1)
    return RedirectResponse("/login?error=Wrong+password", status_code=303)


@rt("/login/desktop")
def get(session, token: str = ""):
    """The desktop app opens its window here with the per-launch password, so you never type it."""
    if settings.desktop and settings.app_password and hmac.compare_digest(token.encode(), settings.app_password.encode()):
        session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login", status_code=303)


@rt("/logout")
def post(session):
    session.clear()
    return RedirectResponse("/login", status_code=303)


@rt("/healthz")
def get():
    return {"ok": True}


# ---------- reading ----------

@rt("/")
async def get(feed: str = "", acct: str = "", custom: str = ""):
    accts = accounts()
    if not accts:
        return shell("Home", Section(
            H1("Welcome"), P("Connect a Bluesky or Mastodon account to start reading and posting."),
            A("Connect an account", href="/settings#accounts", cls="button primary"), cls="panel"))
    if not feed and not custom:
        custom = "combined"
    current = None
    tabs = []
    for t in await home_tabs(accts):
        active = (t.lf is not None and custom == t.lf.slug) or (t.lf is None and not custom and (feed, acct) == t.native)
        current = t.lf if active and t.lf else current
        if t.visible or active:
            tabs.append(A(*([badge(t.networks[0])] if t.lf is None else []), t.name, href=t.href,
                          cls="tab" + (" active" if active else ""), title=t.about or None))
    tabs.append(A("Arrange", href="/feeds/arrange", cls="tab more-tab", title="Reorder, show or hide these tabs"))
    tabs.append(A("All feeds…", href="/feeds", cls="tab more-tab"))
    src = ("/timeline?" + urlencode({"custom": custom}) if custom
           else "/timeline?" + urlencode({"feed": feed, "acct": acct}))
    # Pulling down at the top of the page re-fetches this (custom feeds re-rank from fresh posts).
    again = ("/timeline?" + urlencode({"custom": custom, "fresh": 1}) if custom else src)
    return shell("Read", Div(*tabs, cls="tabs"), feed_header(current) if current else None,
                 Div(Div(Span(cls="spinner"), "Loading", cls="more"), hx_get=src, hx_trigger="load", cls="timeline",
                     data_refresh=again),
                 aside=home_aside(accts))


@dataclass
class HomeTab:
    key: str                  # a custom feed's slug, or native_key() for a network's own feed
    name: str
    href: str
    networks: list
    visible: bool
    default_visible: bool     # custom feeds and feeds pinned on the network show unless you hide them
    about: str = ""
    lf: object = None         # the custom feed, if it is one
    native: tuple = ()        # (source, account id), if it's a network's own feed


def native_key(account_id: str, source: str) -> str:
    return f"native|{account_id}|{source}"


async def home_tabs(accts=None) -> list[HomeTab]:
    """Every feed that can be a tab on Home, in your chosen order, each marked shown or hidden."""
    accts = accounts() if accts is None else accts
    prefs = db.get("feed_prefs", {})
    hidden, shown = set(prefs.get("hidden") or []), set(prefs.get("shown") or [])
    tabs = []
    for lf in feeds.all_feeds():
        f = lf.feed
        nets = list(dict.fromkeys(s.split(":", 1)[0] for s in f.sources)) if f else []
        tabs.append(HomeTab(lf.slug, f.name if f else lf.slug, f"/?custom={lf.slug}", nets, lf.slug not in hidden, True,
                            (f.description if f else "") or "Custom feed", lf=lf))
    multi = {}
    for a in accts:
        multi[a.network] = multi.get(a.network, 0) + 1
    for a in accts:
        for f in await native_feeds(a):
            key = native_key(a.account_id, f.source)
            label = f.name + (f" · {a.rec.handle}" if multi[a.network] > 1 else "")
            tabs.append(HomeTab(key, label, "/?" + urlencode({"feed": f.source, "acct": a.account_id}), [a.network],
                                key in shown or (f.pinned and key not in hidden), f.pinned,
                                f"{NETWORK_LABELS.get(a.network, a.network)} · {a.rec.handle}", native=(f.source, a.account_id)))
    return feeds.arrange(tabs, prefs, key=lambda t: t.key)[0]


def feed_header(lf):
    """A line above a custom feed: what it shows, which networks it draws on, and shortcuts to refresh or edit."""
    f = lf.feed
    nets = list(dict.fromkeys(s.split(":", 1)[0] for s in f.sources)) if f else []
    return Div(Div(P(f.description or f.name, cls="feed-desc") if f else P(Code(lf.error), cls="error"),
                   Div(*[badge(n) for n in nets], cls="feed-nets"), cls="feed-about"),
               Div(Button("Refresh", cls="small", hx_get="/timeline?" + urlencode({"custom": lf.slug, "fresh": 1}),
                          hx_target="next .timeline"),
                   A("Edit", href=f"/feeds/{lf.slug}/edit", cls="button small") if lf.path else None,
                   cls="row"), cls="feed-bar")


PAGE = 25


@rt("/timeline")
async def get(feed: str = "", acct: str = "", custom: str = "", cursor: str = "", offset: int = 0, fresh: int = 0):
    try:
        if custom:
            ranked = await feeds.run(db, accounts(), custom, fresh=bool(fresh))
            chunk = ranked[offset: offset + PAGE]
            nxt = f"/timeline?{urlencode({'custom': custom, 'offset': offset + PAGE})}" if offset + PAGE < len(ranked) else None
            if not ranked:
                return Div("Nothing here yet. The feed fills as posts are fetched in the background; check back in a minute.",
                           cls="empty")
            community.record_shown(db, custom, [r.post.key for r in chunk], offset == 0)
            return (*[post_card(r.post, (r.score, r.terms)) for r in chunk], load_more(nxt))
        a = registry.account(db, acct)
        if not a:
            return error_box("That account isn't connected any more.")
        pg = await a.timeline(feed, cursor or None)
        cache.save_posts(db, pg.posts, feed)
        posts = cache.collapse(pg.posts)
        nxt = f"/timeline?{urlencode({'feed': feed, 'acct': acct, 'cursor': pg.cursor})}" if pg.cursor else None
        return (*[post_card(p) for p in posts], load_more(nxt))
    except NetworkError as e:
        return error_box(str(e))
    except Exception as e:
        log.exception("timeline failed")
        return error_box(f"{type(e).__name__}: {e}")


@rt("/thread")
async def get(key: str):
    account_id, post_id = split_key(key)
    a = registry.account(db, account_id)
    if not a:
        return shell("Thread", error_box("That account isn't connected any more."))
    try:
        ancestors, post, replies = await a.thread(post_id)
    except NetworkError as e:
        return shell("Thread", error_box(str(e)))
    cache.save_posts(db, ancestors + [post] + replies)
    return shell("Thread", H2("Thread", cls="page-title"),
                 Div(*[post_card(p) for p in ancestors], cls="ancestors"),
                 post_card(post, focus=True),
                 A("Reply", href=f"/compose?reply={quote(post.key)}", cls="button primary reply-btn"),
                 Div(*[post_card(p) for p in replies], cls="replies"))


@rt("/profile/{network}/{handle:path}")
async def get(network: str, handle: str, acct: str = ""):
    a = registry.account(db, acct) if acct else None
    if not a or a.network != network:
        a = registry.for_network(db, network)
    if not a:
        return shell("Profile", error_box(f"Connect a {NETWORK_LABELS.get(network, network)} account to view profiles there."))
    try:
        prof = await a.profile(handle)
    except NetworkError as e:
        return shell("Profile", error_box(str(e)))
    counts = [f"{n:,} {label}" for n, label in ((prof.followers, "followers"), (prof.following, "following"),
                                                (prof.posts, "posts")) if n is not None]
    head = Section(Div(avatar(prof, "lg"), Div(H1(prof.name), Small(prof.at_handle), " ", badge(network)), cls="profile-head"),
                   P(prof.bio, cls="bio") if prof.bio else None, P(" · ".join(counts), cls="muted"),
                   A("Open on " + NETWORK_LABELS.get(network, network), href=safe(prof.url), target="_blank", rel="noopener")
                   if prof.url else None, cls="panel profile")
    src = "/profile-feed?" + urlencode({"handle": handle, "acct": a.account_id})
    people.remember(db, [prof])
    return shell(prof.name, head, same_person_panel(network, prof, a.account_id), Div(Div(Span(cls="spinner"), "Loading", cls="more"), hx_get=src, hx_trigger="load"))


def same_person_panel(network: str, prof, account_id: str):
    """Who this account is on the other networks, so mentions in cross-posts reach them everywhere."""
    here = f"/profile/{network}/{quote(prof.handle)}?" + urlencode({"acct": account_id})
    person = people.find(db, network, prof.handle)
    mine = people.normalise(network, prof.handle)

    def link_form(other_network: str = "", other_handle: str = "", label: str = "Link"):
        fields = [Hidden(name="network", value=network), Hidden(name="handle", value=prof.handle),
                  Hidden(name="name", value=prof.display_name), Hidden(name="back", value=here)]
        if other_handle:
            return Form(*fields, Hidden(name="other_network", value=other_network),
                        Hidden(name="other_handle", value=other_handle),
                        Button(label, " ", badge(other_network), " @", other_handle, cls="small"),
                        method="post", action="/people/link", cls="inline")
        return Form(*fields,
                    Select(*[Option(NETWORK_LABELS[n], value=n) for n in people.NETWORKS if n != network],
                           name="other_network", aria_label="Network"),
                    Input(name="other_handle", placeholder="their handle there", required=True,
                          autocapitalize="none", autocorrect="off", cls="grow"),
                    Button("Link", cls="small"), method="post", action="/people/link", cls="row link-form")

    others = [(n, h) for n, h in person.all_handles() if (n, h) != (network, mine)] if person else []
    linked = set(others)
    found = [(n, h) for n, h in people.handles_in_bio(prof) if (n, h) not in linked]
    rows = [Div(*[A(badge(n), " @", h, href=profile_url(n, h), cls="chip handle-chip") for n, h in others],
                A("Edit", href=f"/people#person-{person.id}", cls="small"), cls="row wrap")] if others else []
    if found:
        rows.append(Div(Small("Their profile mentions:", cls="muted"), *[link_form(n, h) for n, h in found], cls="row wrap"))
    return Section(H3("Also on" if others else "Same person on other networks"),
                   *rows,
                   P("Link their accounts and a mention of them in a cross-post tags them on every network.",
                     cls="muted") if not others else None,
                   Details(Summary("Link another account"), link_form()) if others else link_form(),
                   cls="panel people-panel")


@rt("/profile-feed")
async def get(handle: str, acct: str, cursor: str = ""):
    a = registry.account(db, acct)
    try:
        pg = await a.author_feed(handle, cursor or None)
    except NetworkError as e:
        return error_box(str(e))
    cache.save_posts(db, pg.posts)
    nxt = "/profile-feed?" + urlencode({"handle": handle, "acct": acct, "cursor": pg.cursor}) if pg.cursor else None
    return (*[post_card(p) for p in pg.posts], load_more(nxt))


async def _toggle(key: str, what: str):
    from .ui import action_bar
    post = cache.get_post(db, key)
    a = registry.account(db, split_key(key)[0])
    if not post or not a:
        return Response("Post not found", status_code=404)
    on = not (post.liked if what == "like" else post.reposted)
    try:
        await (a.like(post, on) if what == "like" else a.repost(post, on))
    except NetworkError as e:
        return Div(action_bar(post), Small(str(e), cls="error-inline"))
    if on:
        community.record_action(db, key)
    if what == "like":
        post.liked, post.likes = on, max(0, post.likes + (1 if on else -1))
    else:
        post.reposted, post.reposts = on, max(0, post.reposts + (1 if on else -1))
    cache.update_post(db, post)
    return action_bar(post)


@rt("/act/like")
async def post(key: str):
    return await _toggle(key, "like")


@rt("/act/repost")
async def post(key: str):
    return await _toggle(key, "repost")


# ---------- posting ----------

@rt("/compose")
async def get(reply: str = ""):
    accts = [a for a in accounts()]
    if not accts:
        return shell("Post", error_box("Connect an account first."), active="/compose")
    reply_post, reply_acct, prefill = None, None, ""
    if reply:
        reply_post = cache.get_post(db, reply)
        reply_acct = split_key(reply)[0]
        mine = next((a for a in accts if a.account_id == reply_acct), None)
        if reply_post and mine and mine.network == "mastodon":
            prefill = people.reply_mentions(reply_post, {a.rec.handle.lower() for a in accts})
    boxes = []
    for a in accts:
        limit = await a.max_chars()
        checked = (a.account_id == reply_acct) if reply else (a.network != "x" or xapi.post_by_default(db))
        disabled = bool(reply) and a.account_id != reply_acct
        boxes.append(Label(Input(type="checkbox", name="accounts", value=a.account_id, checked=checked, disabled=disabled,
                                 data_network=a.network, data_limit=str(limit), cls="acct-box"),
                           avatar(a.me(), "xs"), Span(a.rec.handle), badge(a.network),
                           Span("", cls="counter", data_for=a.account_id),
                           Small("costs money", cls="muted") if a.experimental else None,
                           cls="acct-option"))
    overrides = Details(Summary("Customise the text per network"),
                        *[Label(Span(a.rec.handle, " ", badge(a.network)),
                                Textarea(name=f"text_{a.account_id}", rows=3, placeholder="Leave empty to use the main text",
                                         data_override=a.account_id, data_network=a.network, cls="mentionable"))
                          for a in accts],
                        cls="overrides")
    preview = Div(id="mention-preview", hx_post="/compose/preview", hx_trigger="input delay:700ms from:#compose-form, "
                  "change from:#compose-form, load", hx_include="#compose-form textarea, #compose-form .acct-box, "
                  "#compose-form [name=reply]", aria_live="polite")
    form = Form(
        Hidden(name="reply", value=reply),
        Textarea(prefill, name="text", id="compose-text", rows=6, placeholder="Write something. Type @ to mention someone.",
                 autofocus=True, required=True, cls="mentionable", autocapitalize="sentences"),
        preview,
        Div(*boxes, cls="acct-list"),
        Div(Label("Images (up to 4)", Input(type="file", name="images", accept="image/*", multiple=True, id="images")),
            Div(id="alt-fields"), cls="images"),
        Div(Label("Content warning", Input(name="cw", placeholder="Optional"), cls="grow"),
            Label(Input(type="checkbox", name="thread", value="on", checked=True),
                  "Split long text into a thread"), cls="row"),
        overrides,
        Div(Span(cls="htmx-indicator spinner"), Button("Publish", cls="primary", id="post-btn"), cls="row end"),
        hx_post="/compose", hx_encoding="multipart/form-data", hx_target="#compose-result", hx_swap="innerHTML",
        hx_indicator=".htmx-indicator", hx_disinherit="*", cls="compose stack", id="compose-form")
    return shell("Write", H2("Reply" if reply else "Write", cls="page-title"),
                 post_card(reply_post) if reply_post else None, form, Div(id="compose-result"),
                 active="/compose")


HOW = {"native": "tags them", "linked": "tags their linked account", "bridged": "tags them through Bridgy Fed",
       "plain": "plain text, no account known there", "name": "their name, no account linked there"}


@rt("/compose/preview")
async def post(req):
    """How each mention will be written on each network the post goes to."""
    form = await req.form()
    text = form.get("text") or ""
    chosen = form.getlist("accounts")
    reply_key = form.get("reply") or ""
    if reply_key:
        chosen = [split_key(reply_key)[0]]
    by_id = {a.account_id: a for a in accounts()}
    targets = [by_id[c] for c in chosen if c in by_id]
    outcomes: dict[str, list] = {}
    for a in targets:
        t = (form.get(f"text_{a.account_id}") or "").strip() or text
        new, mentions = await people.rewrite(db, t, a)
        for m in mentions:
            outcomes.setdefault(m.original, []).append((a, m))
        limit = await a.max_chars()
        if new != t and length_for(a.network, new) > limit:
            outcomes.setdefault("", []).append((a, length_for(a.network, new), limit))
    if not any(k for k in outcomes):
        return ""
    rows = []
    for original, items in outcomes.items():
        if not original:
            continue
        unknown = any(m.how in ("plain", "name") for _, m in items)
        net = people.shape(original)
        fix = A("Link their accounts", href=profile_url({"bare": "x"}.get(net, net), original.lstrip("@")),
                cls="small") if unknown and net != "bare" else (A("Add them in People", href="/people", cls="small")
                                                                   if unknown else None)
        person = people.find_any(db, original)
        rows.append(Li(Strong(person.name if person and person.name else original),
                       " ", Small(original, cls="muted") if person and person.name else None,
                       Ul(*[Li(NETWORK_LABELS.get(a.network, a.network), ": ", Code(m.replacement), " ",
                               Small(HOW[m.how]), cls="how-" + m.how) for a, m in items]), fix,
                       cls="unknown" if unknown else None))
    long = [Li(badge(a.network), f" {a.rec.handle}: {used} of {limit} characters after mentions are rewritten")
            for a, used, limit in outcomes.get("", [])]
    return Section(H3("How your mentions will read"), Ul(*rows, cls="mention-rows"),
                   Ul(*long, cls="warn") if long else None, cls="panel mention-preview")


@rt("/compose")
async def post(req):
    form = await req.form()
    text = (form.get("text") or "").strip()
    chosen = form.getlist("accounts")
    reply_key = form.get("reply") or ""
    cw = (form.get("cw") or "").strip()
    thread = form.get("thread") == "on"
    if not text:
        return error_box("Write something first.")
    if reply_key:
        chosen = [split_key(reply_key)[0]]
    by_id = {a.account_id: a for a in accounts()}
    targets = [by_id[c] for c in chosen if c in by_id]
    if not targets:
        return error_box("Pick at least one account.")
    uploads = []
    files = [f for f in form.getlist("images") if getattr(f, "filename", "")]
    for i, f in enumerate(files[:4]):
        uploads.append(Upload(data=await f.read(), mime=f.content_type or "image/jpeg",
                              alt=(form.get(f"alt_{i}") or "").strip(), filename=f.filename))
    reply_post = None
    if reply_key:
        reply_post = cache.get_post(db, reply_key)
        if reply_post is None:
            _, reply_post, _ = await targets[0].thread(split_key(reply_key)[1])

    problems = []
    texts = {}
    for a in targets:
        t = (form.get(f"text_{a.account_id}") or "").strip() or text
        t, _ = await people.rewrite(db, t, a)
        texts[a.account_id] = t
        limit = await a.max_chars()
        if not thread and length_for(a.network, t) > limit:
            problems.append(f"{a.rec.handle}: {length_for(a.network, t)} of {limit} characters")
    if problems:
        return error_box("Too long, turn on threading or shorten it. " + "; ".join(problems))

    async def one(a):
        return await a.post(texts[a.account_id], uploads, reply_post, cw, thread)

    results = await asyncio.gather(*[one(a) for a in targets], return_exceptions=True)
    rows, record = [], []
    for a, res in zip(targets, results):
        if isinstance(res, Exception):
            rows.append(Li(badge(a.network), " ", a.rec.handle, ": ", Strong("failed"), f" ({res})", cls="fail"))
            record.append({"account": a.account_id, "error": str(res)})
        else:
            first = res[0]
            rows.append(Li(badge(a.network), " ", a.rec.handle, ": posted",
                           f" ({len(res)} posts in a thread)" if len(res) > 1 else "", " · ",
                           A("view", href=first.url, target="_blank", rel="noopener"), cls="ok"))
            record.append({"account": a.account_id, "ids": [r.id for r in res], "urls": [r.url for r in res]})
    db.x("insert into cross_posts(created_at, text, results) values(?, ?, ?)", (time.time(), text, json.dumps(record)))
    feeds._results.clear()
    ok = all(not isinstance(r, Exception) for r in results)
    if reply_key and ok:
        community.record_action(db, reply_key)
    return Div(Strong("Posted." if ok else "Some posts failed."), Ul(*rows),
               Script("document.getElementById('compose-form').reset();document.getElementById('alt-fields').innerHTML='';"
                      "document.getElementById('mention-preview').innerHTML='';") if ok else None,
               cls="result " + ("ok" if ok else "warn"))


# ---------- people ----------

def _handles_field(p, network: str):
    value = ", ".join((p.handles.get(network) or []) if p else [])
    hint = {"bluesky": "alice.bsky.social", "mastodon": "alice@mastodon.social", "x": "alice"}[network]
    return Label(NETWORK_LABELS[network], Input(name=network, value=value, placeholder=hint, autocapitalize="none",
                                                autocorrect="off"))


def person_form(p=None):
    return Form(Hidden(name="pid", value=str(p.id) if p else ""),
                Label("Name", Input(name="name", value=p.name if p else "", placeholder="Alice Example")),
                *[_handles_field(p, n) for n in people.NETWORKS],
                Small("Several accounts on one network? Separate them with commas; the first is used in posts.",
                      cls="muted"),
                Div(Button("Save" if p else "Add person", cls="small primary"), cls="row"),
                method="post", action="/people/save", cls="stack person-form")


@rt("/people")
async def get(ok: str = ""):
    cards = []
    for p in people.all_people(db):
        chips = [A(badge(n), " @", h, href=profile_url(n, h), cls="chip handle-chip") for n, h in p.all_handles()]
        cards.append(Section(Div(Strong(p.name or "(no name)"), *chips, cls="row wrap"),
                             Details(Summary("Edit"), person_form(p),
                                     Form(Hidden(name="pid", value=str(p.id)), Button("Delete", cls="small danger"),
                                          method="post", action="/people/delete")),
                             cls="panel person", id=f"person-{p.id}"))
    return shell("People",
                 H2("People", cls="page-title"),
                 P("Link the accounts one person has on each network. When you mention them in a cross-post, each "
                   "network gets their handle there. Mentions of people you haven't linked are posted as plain text "
                   "on other networks, or through Bridgy Fed when they're bridged, so they never tag a stranger.",
                   cls="muted"),
                 Div(ok, cls="result ok") if ok else None,
                 Section(H3("Add someone"), person_form(), cls="panel"),
                 *(cards or [P("No one linked yet. You can also link accounts from anyone's profile page.",
                                         cls="muted")]),
                 active="/settings")


@rt("/people/save")
async def post(req):
    form = await req.form()
    handles = {n: [h for h in (form.get(n) or "").replace("\n", ",").split(",") if h.strip()] for n in people.NETWORKS}
    pid = form.get("pid") or ""
    if not any(handles.values()):
        if pid:
            people.delete(db, int(pid))
        return RedirectResponse("/people", status_code=303)
    pid = people.save(db, form.get("name") or "", handles, int(pid) if pid else None)
    return RedirectResponse(f"/people?ok=Saved#person-{pid}", status_code=303)


@rt("/people/delete")
async def post(pid: int):
    people.delete(db, pid)
    return RedirectResponse("/people?ok=Deleted", status_code=303)


@rt("/people/link")
async def post(network: str, handle: str, other_network: str, other_handle: str, name: str = "", back: str = ""):
    if network in people.NETWORKS and other_network in people.NETWORKS and other_handle.strip():
        people.link(db, network, handle, other_network, other_handle, name)
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/people", status_code=303)


@rt("/people/suggest")
async def get(q: str = "", networks: str = ""):
    """People to offer when you type @ in the composer: linked people, accounts you've seen, then a live search."""
    q = q.strip().lstrip("@")
    if not q:
        return JSONResponse([])
    wanted = [n for n in networks.split(",") if n in people.NETWORKS] or list(people.NETWORKS)
    found, seen_accts = people.suggest_local(db, q)
    out, taken, pids = [], set(), set()

    def add_person(p):
        if p.id in pids:
            return
        pids.add(p.id)
        hs = p.all_handles()
        best = next(((n, h) for w in wanted for n, h in hs if n == w), hs[0])
        face = next((s for n, h in hs if (s := people.seen(db, n, h)) and s.avatar), None)
        taken.update(hs)
        out.append({"name": p.name or best[1], "avatar": face.avatar if face else "", "insert": "@" + best[1],
                    "handles": [{"network": n, "handle": h} for n, h in hs], "linked": True})

    def add_account(a):
        h = people.normalise(a.network, a.handle)
        if (a.network, h) in taken:
            return
        p = people.find(db, a.network, h)
        if p:
            return add_person(p)
        taken.add((a.network, h))
        out.append({"name": a.display_name or h, "avatar": a.avatar, "insert": "@" + h,
                    "handles": [{"network": a.network, "handle": h}], "linked": False})

    for p in found:
        add_person(p)
    for a in seen_accts:
        if a.network in wanted:
            add_account(a)
    if len(out) < 8 and len(q) >= 2:
        searchers, nets = [], set()
        for a in accounts():
            if a.network in wanted and a.network not in nets:
                nets.add(a.network)
                searchers.append(a.search_people(q, 6))
        try:
            results = await asyncio.wait_for(asyncio.gather(*searchers, return_exceptions=True), 3)
        except asyncio.TimeoutError:
            results = []
        for res in results:
            if isinstance(res, list):
                people.remember(db, res)
                for a in res:
                    add_account(a)
    return JSONResponse(out[:8])


# ---------- notifications ----------

@rt("/notifications")
async def get(only: str = ""):
    try:
        await asyncio.wait_for(_refresh_notifications(), timeout=15)
    except asyncio.TimeoutError:
        pass
    rows = db.q("select data from notifications order by created_at desc limit 150")
    items = [Notification.from_dict(json.loads(r["data"])) for r in rows]
    if only == "replies":
        items = [n for n in items if n.kind in ("reply", "mention", "quote")]
    db.set("notif_seen_at", time.time())
    tabs = Div(A("All", href="/notifications", cls="tab" + ("" if only else " active")),
               A("Replies and mentions", href="/notifications?only=replies", cls="tab" + (" active" if only else "")),
               cls="tabs")
    body = [notification_row(n) for n in items] or [Div("No notifications yet.", cls="empty")]
    return shell("Notifications", H2("Notifications", cls="page-title"), tabs, Div(*body, cls="notifs", data_refresh="reload"),
                 active="/notifications")


async def _refresh_notifications():
    for a in accounts():
        if a.network == "x" and not xapi.reading_enabled(db):
            continue
        try:
            push.store_notifications(db, await a.notifications())
        except Exception as e:
            log.warning("notifications %s: %s", a.account_id, e)


# ---------- custom feeds ----------

def feed_card(lf, hidden: bool = False):
    f = lf.feed
    slug = lf.slug
    nets = list(dict.fromkeys(s.split(":", 1)[0] for s in f.sources)) if f else []
    return Section(
        Div(Div(H3(f.name if f else slug), *[badge(n) for n in nets], cls="feed-title"),
            Small(f"{slug}.py" if lf.path else "built in", cls="muted"), cls="feed-head"),
        P(f.description) if f and f.description else None,
        P(Strong("Error: "), Code(lf.error), cls="error") if lf.error else None,
        P("Sources: ", *[Code(s) for s in f.sources], cls="sources") if f else None,
        Div(A("Open", href=f"/?custom={slug}", cls="button primary small") if f else None,
            A("Edit", href=f"/feeds/{slug}/edit", cls="button small") if lf.path else None,
            A("Copy", href=f"/feeds/new/edit?{urlencode({'start': slug})}", cls="button small",
              title="Start a new feed from this one"),
            A("Share", href=f"/feeds/{slug}/share", cls="button small", title="Share it in the community directory")
            if lf.path and f else None,
            Form(Button("Show on Home" if hidden else "Hide from Home", cls="small"),
                 method="post", action=f"/feeds/{slug}/visibility"),
            Form(Button("Remove", cls="small danger"), method="post", action=f"/feeds/{slug}/remove",
                 onsubmit=f"return confirm({json.dumps(f'Remove {f.name if f else slug}? The file is kept as _removed_{slug}.py.')})")
            if lf.path else None, cls="row feed-actions"),
        Details(Summary("View source"), Pre(Code(lf.source, cls="language-python"))),
        cls="panel feed-card" + (" hidden-feed" if hidden else ""), id=slug)


def starter_card(lf):
    f = lf.feed
    nets = list(dict.fromkeys(s.split(":", 1)[0] for s in f.sources)) if f else []
    return Section(
        Div(Div(H3(f.name if f else lf.slug), *[badge(n) for n in nets], cls="feed-title"), cls="feed-head"),
        P(f.description) if f and f.description else None,
        Div(Form(Button("Add feed", cls="primary small"), method="post", action=f"/feeds/{lf.slug}/install"),
            Details(Summary("View source"), Pre(Code(lf.source, cls="language-python"))), cls="row"),
        cls="panel feed-card starter")


@rt("/feeds")
async def get():
    ordered, hidden = feeds.arrange(feeds.all_feeds(), db.get("feed_prefs", {}))
    cards = [feed_card(lf, lf.slug in hidden) for lf in ordered]
    starters = [starter_card(lf) for lf in feeds.starter_feeds() if lf.feed]
    native = []
    for a in accounts():
        fs = await native_feeds(a)
        native.append(Li(badge(a.network), " ", Strong(a.rec.handle), Ul(*[
            Li(A(f.name, href="/?" + urlencode({"feed": f.source, "acct": a.account_id})), " ", Code(f.source, cls="muted"))
            for f in fs])))
    return shell("Feeds", feeds_subnav("/feeds"), H2("Your custom feeds", cls="page-title"),
                 P("Custom feeds are Python files. Each one picks posts from the sources it lists and ranks them with a ",
                   Code("score()"), " function. Edit one here or in your editor; changes apply on save.", cls="muted"),
                 Div(A("New feed", href="/feeds/new/edit", cls="button primary"),
                     A("Arrange tabs", href="/feeds/arrange", cls="button", title="Put your Home tabs in the order you want"),
                     A("Browse community feeds", href="/feeds/community", cls="button"),
                     cls="row"), *cards,
                 *([H2("Add more feeds", cls="page-title"),
                    P("Ready-made feeds that ship with the app. Adding one copies it into your feeds folder so you can edit it.",
                      cls="muted"), *starters] if starters else []),
                 H2("Native feeds", cls="page-title"),
                 P("Use these source names in a custom feed's sources list.", cls="muted"),
                 Ul(*native, cls="native-feeds"), active="/feeds")


def _save_prefs(prefs: dict) -> None:
    db.set("feed_prefs", prefs)


@rt("/feeds/{slug}/visibility")
async def post(slug: str):
    prefs = db.get("feed_prefs", {})
    hidden = set(prefs.get("hidden") or [])
    hidden ^= {slug}
    _save_prefs({**prefs, "hidden": sorted(hidden)})
    return RedirectResponse(f"/feeds#{slug}", status_code=303)


@rt("/feeds/arrange")
async def get():
    rows = []
    for i, t in enumerate(await home_tabs()):
        name_id = f"tab-{i}"
        rows.append(Li(
            Button(icon("grip"), cls="grip", type="button", title="Drag to move", aria_label=f"Move {t.name}",
                   aria_describedby="arrange-help"),
            Label(Input(type="checkbox", checked=t.visible, cls="arrange-show", aria_labelledby=name_id),
                  Span(Strong(t.name, id=name_id),
                       Small("Custom feed", *[badge(n) for n in t.networks], cls="muted") if t.lf
                       else Small(badge(t.networks[0]), t.about.split(" · ", 1)[-1], cls="muted"),
                       cls="arrange-name"), cls="arrange-label"),
            Span(Button("↑", type="button", cls="nudge", data_step="-1", title="Move up", aria_label=f"Move {t.name} up"),
                 Button("↓", type="button", cls="nudge", data_step="1", title="Move down", aria_label=f"Move {t.name} down"),
                 cls="nudges"),
            cls="arrange-item" + ("" if t.visible else " off"), data_key=t.key))
    return shell("Arrange tabs", H2("Arrange your tabs", cls="page-title"),
                 P("Drag a feed by its handle to put your Home tabs in the order you want. Untick a feed to keep it "
                   "off Home. Changes save as you go.", cls="muted", id="arrange-help"),
                 Ol(*rows, id="arrange", cls="arrange-list"),
                 Div(A("Done", href="/", cls="button primary"), Span(id="arrange-status", cls="muted", aria_live="polite"),
                     cls="row arrange-foot"), active="/feeds")


@rt("/feeds/arrange")
async def post(req):
    body = await req.json()
    known = {t.key: t for t in await home_tabs()}
    chosen = [(row["key"], bool(row.get("visible")), known[row["key"]].default_visible)
              for row in body.get("tabs") or [] if row.get("key") in known]
    chosen += [(k, t.visible, t.default_visible) for k, t in known.items() if k not in {c[0] for c in chosen}]
    _save_prefs(feeds.save_arrangement(db.get("feed_prefs", {}), chosen, set(known)))
    return JSONResponse({"ok": True})


@rt("/feeds/{slug}/remove")
async def post(slug: str):
    feeds.remove_feed(_safe_slug(slug))
    return RedirectResponse("/feeds", status_code=303)


@rt("/feeds/{slug}/install")
async def post(slug: str):
    slug = _safe_slug(slug)
    feeds.install_starter(slug)
    return RedirectResponse(f"/?custom={slug}" if feeds.get_feed(slug) else "/feeds", status_code=303)


TEMPLATE = '''from crossposter.feeds import Feed, Post, Ctx

feed = Feed(
    name="My feed",
    description="What this feed shows.",
    sources=["bluesky:following", "mastodon:home"],
    window_hours=24,
)


def keep(post: Post, ctx: Ctx) -> bool:
    return True


def score(post: Post, ctx: Ctx) -> dict:
    return {
        "engagement": ctx.normalised_engagement(post),
        "freshness": 1 / (ctx.age_hours(post) + 2),
    }
'''


def _safe_slug(slug: str) -> str:
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in slug.lower()).strip("_")
    return s[:60] or "my_feed"


def _starting_point(start: str) -> str:
    """Source for a new feed copied from an existing or starter feed, renamed so it doesn't clash."""
    lf = feeds.get_feed(start) or next((f for f in feeds.starter_feeds() if f.slug == start), None) if start else None
    if not lf:
        return TEMPLATE
    if lf.feed:
        return lf.source.replace(f'name="{lf.feed.name}"', f'name="{lf.feed.name} (copy)"', 1)
    return lf.source


def feed_reference():
    """The helpers and sources a feed can use, built from the docstrings so it stays current."""
    helpers = []
    for name in sorted(vars(feeds.Ctx)):
        fn = getattr(feeds.Ctx, name)
        if name.startswith("_") or not callable(fn) or not fn.__doc__:
            continue
        args = ", ".join(p for p in inspect.signature(fn).parameters if p != "self")
        helpers.append((f"ctx.{name}({args})", fn.__doc__.strip().splitlines()[0]))
    attrs = [("ctx.posts", "every candidate post"), ("ctx.now", "the time ranking started (unix seconds)")]
    post_fields = ("network, author.handle, created_at, plain_text, likes, reposts, replies, quotes, engagement, "
                   "card, media, quote, reposted_by, is_reply, lang, also_on")
    sources = [line.strip() for line in (feeds.__doc__ or "").split("Sources a feed can draw from:")[-1].splitlines() if line.strip()]
    return Details(Summary("Reference: sources and helpers"),
                   H3("Sources"), Pre(Code("\n".join(sources))),
                   H3("Post fields"), P(Code(post_fields)),
                   H3("ctx helpers"), Ul(*[Li(Code(n), " ", d) for n, d in helpers],
                                         *[Li(Code(n), " ", d) for n, d in attrs]),
                   cls="panel reference")


@rt("/feeds/{slug}/edit")
async def get(slug: str, start: str = ""):
    is_new = slug == "new"
    lf = None if is_new else feeds.get_feed(slug)
    code = _starting_point(start) if is_new else (lf.source if lf else "")
    return shell("Edit feed", H2("New feed" if is_new else f"Edit {slug}.py", cls="page-title"),
                 Form(Label("File name", Input(name="slug", value=(f"{_safe_slug(start)}_copy" if start else "") if is_new else slug, placeholder="my_feed",
                                               required=True, pattern="[A-Za-z0-9_]+", readonly=not is_new)),
                      Textarea(code, name="code", rows=24, cls="code", spellcheck="false"),
                      Div(Button("Save", cls="primary", formaction="/feeds/save", formmethod="post"),
                          Button("Preview", type="button", hx_post="/feeds/preview", hx_target="#preview",
                                 hx_include="closest form", hx_indicator=".htmx-indicator"),
                          Span(cls="htmx-indicator spinner"), cls="row"),
                      method="post", action="/feeds/save", cls="stack"),
                 feed_reference(), Div(id="preview", cls="preview"), wide=True, active="/feeds")


@rt("/feeds/save")
async def post(slug: str, code: str):
    slug = _safe_slug(slug)
    settings.feeds_dir.mkdir(parents=True, exist_ok=True)
    (settings.feeds_dir / f"{slug}.py").write_text(code.replace("\r\n", "\n"), encoding="utf-8")
    feeds._results.pop(slug, None)
    return RedirectResponse(f"/feeds#{slug}" if (feeds.get_feed(slug) or feeds.COMBINED).error else f"/?custom={slug}",
                            status_code=303)


@rt("/feeds/preview")
async def post(code: str):
    """Run unsaved feed code against cached posts and show the top ten with score breakdowns."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "preview.py"
        path.write_text(code)
        lf = feeds._load(path)
        if lf.error:
            return error_box(lf.error)
        accts = accounts()
        try:
            await cache.refresh(db, accts, lf.feed.sources)
            posts = cache.collapse(cache.candidates(db, lf.feed.sources, lf.feed.window_hours))[: lf.feed.limit]
            ctx = feeds.Ctx(posts, {a.rec.handle for a in accts}, set(db.get("muted", [])))
            ranked = await asyncio.wait_for(asyncio.to_thread(feeds.rank, lf, posts, ctx), settings.feed_timeout_seconds)
        except Exception as e:
            return error_box(f"{type(e).__name__}: {e}")
    return (P(Strong(f"{len(ranked)} posts"), f" matched out of {len(posts)} candidates. Top ten:", cls="muted"),
            *[post_card(r.post, (r.score, r.terms)) for r in ranked[:10]])


# ---------- community feeds ----------

def feeds_subnav(active: str):
    """Yours / Community / Your submissions, across the top of the Feeds pages."""
    links = (("/feeds", "Yours"), ("/feeds/community", "Community"), ("/feeds/submissions", "Your submissions"),
             ("/feeds/arrange", "Arrange tabs"))
    return Div(*[A(label, href=h, cls="tab" + (" active" if h == active else ""), aria_current="page" if h == active else None)
                 for h, label in links], cls="tabs feeds-subnav")


def _stats_line(f: dict) -> str:
    s = f.get("stats")
    bits = [f"{f.get('downloads', 0):,} downloads"]
    if s:
        bits.append(f"{s['users']:,} weekly users")
        bits.append(f"{s['long_term_share'] * 100:.0f}% use it 30+ days")
    else:
        bits.append("too new for stats")
    return " · ".join(bits)


def _nets(sources: list[str]) -> list[str]:
    return list(dict.fromkeys(s.split(":", 1)[0] for s in sources))


def stats_prompt_card():
    """Shown above the Community list until you've answered the stats question."""
    if community.choice(db) is not None:
        return None
    return Div(P(Strong("Help feed authors, anonymously?")),
               P("You'll be asked the first time you add a community feed. Counts only, never handles or posts.", cls="muted"),
               A("See what's sent", href="/settings/privacy"), cls="notice-card")


@rt("/feeds/community")
async def get(q: str = "", fresh: int = 0):
    try:
        remote = await community.list_feeds(fresh=bool(fresh))
    except community.DirectoryError as e:
        return shell("Community feeds", feeds_subnav("/feeds/community"), error_box(str(e)),
                     P("Your own feeds keep working; this only affects browsing the directory.", cls="muted"), active="/feeds")
    have = community.installed(db)
    by_remote = {rec["remote"]: (local, rec) for local, rec in have.items() if (settings.feeds_dir / f"{local}.py").is_file()}
    if q:
        ql = q.lower()
        remote = [f for f in remote if ql in (f["name"] + " " + f["description"] + " " + f["credit"]).lower()]
    rows = []
    for f in remote:
        mine = by_remote.get(f["slug"])
        same = None if mine else community.same_file_named(f)
        if mine and mine[1].get("version", 1) < f["version"]:
            action = A(f"Update to v{f['version']}", href=f"/feeds/community/{f['slug']}", cls="button small primary")
        elif mine or same:
            action = A("Open", href=f"/?custom={mine[0] if mine else same}", cls="button small")
        else:
            action = A("Get", href=f"/feeds/community/{f['slug']}", cls="button small primary")
        rows.append(Li(Div(A(H3(f["name"]), href=f"/feeds/community/{f['slug']}", cls="plain"),
                           Small(f"by {f['credit']}" if f["credit"] else "shared anonymously", " · ",
                                 *[badge(n) for n in _nets(f["sources"])], cls="muted"),
                           P(f["description"], cls="feed-desc"), Small(_stats_line(f), cls="muted"), cls="community-about"),
                       Span(Small("✓ Added" if mine else "✓ You have this", cls="added") if mine or same else None, action,
                            cls="community-action"),
                       cls="community-row"))
    return shell("Community feeds", feeds_subnav("/feeds/community"),
                 H2("Community feeds", cls="page-title"),
                 P("Feeds other people wrote and shared. Each runs on this device, on posts already in your timelines. "
                   "Read the code before you add one.", cls="muted"),
                 Form(Label("Search the directory", Input(type="search", name="q", value=q,
                                                          placeholder="News, art, quiet people…")),
                      Button("Search", cls="small"), method="get", action="/feeds/community", cls="row connect"),
                 stats_prompt_card(),
                 Ol(*rows, cls="community-list") if rows else P("Nothing matches that.", cls="muted"),
                 P(A("Browse on the web", href=settings.directory_url, target="_blank", rel="noopener"), " · ",
                   A("Refresh", href="/feeds/community?fresh=1"), cls="muted small"),
                 active="/feeds")


@rt("/feeds/community/{slug}")
async def get(slug: str, error: str = ""):
    try:
        f = await community.get_feed(slug)
    except community.DirectoryError as e:
        return shell("Community feed", feeds_subnav("/feeds/community"), error_box(str(e)), active="/feeds")
    local = community.local_for(db, slug)
    rec = community.installed(db).get(local) if local else None
    accts = accounts()
    read_rows = []
    for src in f["sources"]:
        net = src.split(":", 1)[0]
        mine = [a.rec.handle for a in accts if a.network == net]
        if net == "x" and not xapi.reading_enabled(db):
            mine = []
        read_rows.append(Li(badge(net), " ", Code(src), " ",
                            Span("from " + ", ".join(mine) if mine else "skipped: no account connected", cls="muted")))
    s = f.get("stats")
    check = feedcheck.check(f["source"])
    if rec and rec.get("version", 1) < f["version"]:
        button = f"Update to version {f['version']}"
    elif rec:
        button = None
    else:
        button = "Add to Home"
    return shell(f["name"], feeds_subnav("/feeds/community"),
                 error_box(error) if error else None,
                 P(f"Community feed · version {f['version']}", cls="kicker"), H2(f["name"], cls="page-title"),
                 P(f["description"], cls="feed-desc"),
                 P(f"by {f['credit']}" if f["credit"] else "Shared anonymously", " · ", Span("✓ Reviewed", cls="ok-text"),
                   " · ", A("Its page in the directory", href=community.page_url(slug), target="_blank", rel="noopener"),
                   cls="muted small"),
                 Section(H3("Runs on this device"),
                         P("It ranks posts already in your timelines. It can't post, follow, go online or read your files."),
                         Ul(*read_rows, cls="read-rows"), cls="panel"),
                 Section(H3("How it's doing"),
                         Div(Div(Strong(f"{f['downloads']:,}"), Small("downloads", cls="muted")),
                             Div(Strong(f"{s['users']:,}"), Small("weekly users", cls="muted")) if s else None,
                             Div(Strong(f"{s['long_term_share'] * 100:.0f}%"), Small("use it 30+ days", cls="muted")) if s else None,
                             Div(Strong(f"{s['read_per_visit']:.0f}"), Small("posts read a visit", cls="muted")) if s else None,
                             cls="mini-stats"),
                         None if s else P("Too new for weekly figures.", cls="muted small"), cls="panel"),
                 Section(H3("The code"), P(f"{len(f['source'].splitlines())} lines. "
                                           + ("Passed the checks on this device." if check.ok else "Failed the checks on this device."),
                                           cls="muted small"),
                         Pre(Code(f["source"], cls="language-python")), cls="panel"),
                 Div(Form(Button(button, cls="primary"), method="post", action=f"/feeds/community/{slug}/add") if button and check.ok
                     else A("Open", href=f"/?custom={local}", cls="button primary") if local else None,
                     A("Back", href="/feeds/community", cls="button"), cls="row sheet-actions"),
                 P("Updates are shown to you like this before they're applied." if not rec else
                   "Updating replaces your copy, including any edits you made.", cls="muted small"),
                 active="/feeds")


@rt("/feeds/community/{slug}/add")
async def post(slug: str):
    try:
        local = await community.add(db, slug)
    except community.DirectoryError as e:
        return RedirectResponse(f"/feeds/community/{quote(slug)}?" + urlencode({"error": str(e)}), status_code=303)
    feeds._results.pop(local, None)
    nxt = f"/?custom={local}"
    if community.choice(db) is None:
        return RedirectResponse("/feeds/stats-question?" + urlencode({"next": nxt, "added": local}), status_code=303)
    return RedirectResponse(nxt, status_code=303)


STATS_EXPLAINER = ("Once a week, Broadsheet can send a few anonymous counts about the community feeds you use: whether "
                   "they're on Home, how often you open them, how many posts you read. Never your handles, accounts or "
                   "posts, never the feeds you wrote, and no ID that links one week to the next.")


@rt("/feeds/stats-question")
async def get(next: str = "/", added: str = "", error: str = ""):
    lf = feeds.get_feed(added) if added else None
    choices = [("yes", "Yes, share anonymous counts", "For community feeds only. Weekly."),
               ("no", "No thanks", "Nothing is sent. Feeds work the same.")]
    return shell("Anonymous stats",
                 P(Span("✓ ", cls="ok-text"), f"{lf.feed.name} is on your Home", cls="ok-text") if lf and lf.feed else None,
                 H2("Help feed authors see what works?", cls="page-title"),
                 P(STATS_EXPLAINER, cls="lede"),
                 error_box(error) if error else None,
                 Form(Fieldset(Legend("Choose one to continue", cls="muted small"),
                               *[Label(Input(type="radio", name="choice", value=v, required=True),
                                       Span(Strong(t), Small(sub, cls="muted")), cls="choice") for v, t, sub in choices],
                               cls="choices"),
                      Hidden(name="next", value=next),
                      A("See exactly what a report looks like", href="/settings/privacy", cls="small"),
                      Div(Button("Done", cls="primary"), cls="row"),
                      P("Asked once. Change it any time in Settings › Privacy.", cls="muted small"),
                      method="post", action="/feeds/stats-question", cls="stack"),
                 active="/feeds")


@rt("/feeds/stats-question")
async def post(choice: str = "", next: str = "/"):
    if choice not in ("yes", "no"):
        return RedirectResponse("/feeds/stats-question?" + urlencode({"next": next, "error": "Pick one of the two."}),
                                status_code=303)
    community.set_choice(db, choice)
    return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)


async def tab_positions() -> dict[str, int]:
    """Each custom feed's place among the tabs shown on Home, counting from 1."""
    visible = [t for t in await home_tabs() if t.visible]
    return {t.key: i for i, t in enumerate(visible, 1) if t.lf is not None}


@rt("/settings/privacy")
async def get(ok: str = ""):
    on = community.choice(db) == "yes"
    report = community.build_report(db, community.week_of(), await tab_positions())
    last = db.get(community.SENT)
    status = ("On. Last week's report goes out early in the week, once." if on else
              "Off. Nothing is counted or sent." if community.choice(db) == "no" else
              "Not chosen yet. Nothing is counted or sent until you say yes.")
    return shell("Privacy", H2("Privacy", cls="page-title"), Div(ok, cls="result ok") if ok else None,
                 Section(H3("Share anonymous feed stats"),
                         P(status, cls="muted"),
                         P(STATS_EXPLAINER),
                         Form(Button("Turn off" if on else "Turn on", cls="primary" if not on else ""),
                              Hidden(name="choice", value="no" if on else "yes"), method="post", action="/settings/privacy"),
                         cls="panel"),
                 Section(H3("This week's report so far, exactly"),
                         P("Not sent: this is a preview." if not on else "This is what will be sent for this week.", cls="muted small"),
                         Pre(Code(json.dumps(report, indent=2))),
                         Div(Div(Strong("Sent"), Ul(Li("Counts for feeds from the community directory"),
                                                    Li("How long you've had each one, in rough bands"))),
                             Div(Strong("Never sent"), Ul(Li("Handles or accounts"), Li("Posts, follows, lists"),
                                                          Li("Feeds you wrote"), Li("Any lasting ID"))), cls="two-col"),
                         P("The directory doesn't keep IP addresses and adds each report to weekly totals before throwing it "
                           "away. A figure only appears once 10 or more apps report it. Turning this off stops future "
                           "reports and clears what's been counted. Past reports can't be linked to you, so there's nothing "
                           "else to delete.", cls="muted small"),
                         Details(Summary("Last report sent"), Pre(Code(json.dumps(last["report"], indent=2)))) if last else None,
                         cls="panel"),
                 P(A("See the public dashboard", href=settings.directory_url, target="_blank", rel="noopener")),
                 active="/settings")


@rt("/settings/privacy")
async def post(choice: str = ""):
    community.set_choice(db, choice)
    return RedirectResponse("/settings/privacy?" + urlencode(
        {"ok": "Anonymous stats are on." if choice == "yes" else "Anonymous stats are off."}), status_code=303)


@rt("/feeds/{slug}/share")
async def get(slug: str, error: str = ""):
    lf = feeds.get_feed(slug)
    if not lf or not lf.path or not lf.feed:
        return RedirectResponse("/feeds", status_code=303)
    check = feedcheck.check(lf.source)
    remote = await community.listed_slug_for(db, slug)
    from_directory = community.installed(db).get(slug)
    items = [Li(Span("✕ " if i.level == "error" else "! ", cls="no-text" if i.level == "error" else "warn-text"), i.text,
                Small(f" line {i.line}", cls="muted") if i.line else None) for i in check.items]
    return shell("Share a feed", feeds_subnav("/feeds/submissions"),
                 H2("Share with the community", cls="page-title"),
                 P(f"{lf.feed.name} · {slug}.py" + (f" · update to your listed feed" if remote else ""), cls="muted"),
                 error_box(error) if error else None,
                 Div(P(Strong("This came from the community directory.")),
                     P("Share your own feeds, or a copy you've changed a lot, rather than someone else's as it is."),
                     cls="notice-card warn") if from_directory else None,
                 Section(H3("Checks"),
                         Ul(*items, cls="check-list") if items else P(Span("✓ ", cls="ok-text"),
                                                                       "Passed: no network or file access, nothing personal spotted."),
                         P("Fix errors before sending. Warnings are shown to the reviewer too.", cls="muted small")
                         if items else None, cls="panel"),
                 Form(Label("Name in the directory", Input(name="name", value=lf.feed.name, required=True, maxlength=80)),
                      Label("One line on what it shows", Textarea(lf.feed.description, name="description", rows=2,
                                                                   maxlength=200)),
                      Label("Credit as (shown publicly, optional)",
                            Input(name="credit", placeholder="A name, GitHub username or website", maxlength=80),
                            Small("Leave it blank to stay anonymous. Your social accounts are never attached.",
                                  cls="muted")),
                      Label("Note for the reviewer (optional)", Input(name="note", maxlength=200,
                                                                      placeholder="What changed, or anything to know")),
                      Label(Input(type="checkbox", name="licence", required=True),
                            "Share it under the MIT licence so others can copy and change it", cls="check"),
                      Hidden(name="remote", value=remote),
                      P("A maintainer reads every feed before it's listed, usually within a few days. Only the file and "
                        "what you type here are sent, never your posts or accounts.", cls="muted small"),
                      Div(Button("Send for review", cls="primary", disabled=not check.ok),
                          A("See the file", href=f"/feeds/{slug}/edit", cls="button"), cls="row"),
                      method="post", action=f"/feeds/{slug}/share", cls="stack"),
                 active="/feeds")


@rt("/feeds/{slug}/share")
async def post(slug: str, name: str = "", description: str = "", credit: str = "", note: str = "", licence: str = "",
               remote: str = ""):
    lf = feeds.get_feed(slug)
    if not lf or not lf.path:
        return RedirectResponse("/feeds", status_code=303)
    back = f"/feeds/{quote(slug)}/share?"
    if not licence:
        return RedirectResponse(back + urlencode({"error": "Tick the licence box to share it."}), status_code=303)
    try:
        await community.share(db, slug, name.strip() or lf.slug, description.strip(), credit.strip(), lf.source,
                              note.strip(), remote)
    except community.DirectoryError as e:
        return RedirectResponse(back + urlencode({"error": str(e)}), status_code=303)
    return RedirectResponse("/feeds/submissions?" + urlencode({"sent": name.strip()}), status_code=303)


STATUS_LABELS = {"pending": ("In review", "warn-text"), "changes": ("Changes asked", "accent-text"),
                 "approved": ("Listed", "ok-text"), "declined": ("Declined", "muted")}


@rt("/feeds/submissions")
async def get(sent: str = ""):
    try:
        subs = await community.my_submissions(db)
    except community.DirectoryError as e:
        return shell("Your submissions", feeds_subnav("/feeds/submissions"), H2("Your submissions", cls="page-title"),
                     error_box(str(e)), active="/feeds")
    locals_by_name = {r["name"]: r["local"] for r in community.receipts(db)}
    rows = []
    for s in subs:
        label, tone = STATUS_LABELS.get(s["status"], (s["status"], "muted"))
        local = locals_by_name.get(s["name"])
        steps = None
        if s["status"] == "pending":
            steps = Ol(Li(Strong("Sent"), Small(ui_ago(s["created_at"]) + " ago", cls="muted"), cls="done"),
                       Li(Strong("Automatic checks passed"), cls="done"),
                       Li(Strong("Maintainer review"), Small("Usually a few days.", cls="muted"), cls="now"),
                       Li(Strong("Listed in the directory"), cls="todo"), cls="steps")
        rows.append(Section(
            Div(H3(s["name"]), Span(label, cls=f"status {tone}"), cls="feed-head"),
            steps,
            Blockquote(f"“{s['reviewer_note']}”") if s["reviewer_note"] else None,
            Small(f"{s['downloads']:,} downloads · ", A("See its page", href=community.page_url(s["slug"]), target="_blank",
                                                        rel="noopener"), cls="muted")
            if s["status"] == "approved" and s["slug"] else None,
            Div(A("Edit and resend", href=f"/feeds/{local}/edit", cls="button small") if s["status"] == "changes" and local else None,
                A("Send again", href=f"/feeds/{local}/share", cls="button small") if s["status"] == "changes" and local else None,
                Form(Button("Withdraw", cls="small"), method="post", action=f"/feeds/submissions/{s['id']}/withdraw")
                if s["status"] in ("pending", "changes") else None, cls="row"),
            cls="panel submission"))
    return shell("Your submissions", feeds_subnav("/feeds/submissions"), H2("Your submissions", cls="page-title"),
                 Div(Strong(f"{sent} was sent."), " It passed the automatic checks and is waiting for a maintainer.",
                     cls="result ok") if sent else None,
                 *rows if rows else (P("You haven't shared any feeds yet. Open one of your feeds under ",
                                       A("Yours", href="/feeds"), " and choose Share.", cls="muted"),),
                 P("No sign-up needed. This device keeps a private receipt for each feed you send, which is how it checks "
                   "the status and sends updates.", cls="muted small"), active="/feeds")


def ui_ago(ts: float) -> str:
    from .ui import ago
    a = ago(ts)
    return "moments" if a == "now" else a


@rt("/feeds/submissions/{sid}/withdraw")
async def post(sid: int):
    try:
        await community.withdraw(db, sid)
    except community.DirectoryError:
        pass
    return RedirectResponse("/feeds/submissions", status_code=303)


# ---------- settings and accounts ----------

@rt("/settings")
async def get(error: str = "", ok: str = ""):
    accts = accounts()
    acct_rows = [Li(account_chip(a.me()), badge(a.network),
                    Form(Button("Remove", cls="small danger"), method="post", action=f"/accounts/remove?{urlencode({'id': a.account_id})}")
                    if not a.account_id.startswith("demo-") else Small("demo", cls="muted"), cls="acct-row")
                 for a in accts]
    kinds = push.notify_kinds(db)
    kind_boxes = [Label(Input(type="checkbox", name="kinds", value=k, checked=k in kinds), label)
                  for k, label in (("reply", "Replies"), ("mention", "Mentions"), ("quote", "Quotes"),
                                   ("follow", "New followers"), ("like", "Likes"), ("repost", "Reposts"))]
    usage = xapi.usage(db)
    if settings.browser:
        x_section = Section(H3("X"), P("X isn't available in the browser version, because X doesn't let web pages "
                                       "connect to it. The desktop and Android apps support X.", cls="muted"), cls="panel")
    elif settings.x_enabled:
        x_section = _x_settings()
    else:
        x_section = Section(H3("X"), P("X is switched off on this server (X_ENABLED=false).", cls="muted"), cls="panel")
    if settings.browser:
        notify_intro = P("Mastodon can notify this browser even when Broadsheet is closed. Bluesky notifications only "
                         "show up while Broadsheet is open.", cls="muted")
        notify_buttons = (Button("Turn on Mastodon notifications", id="push-on", cls="primary"),)
    else:
        notify_intro = P("On iPhone, first add this app to your home screen (Share, then Add to Home Screen) and open it "
                         "from there. Then switch notifications on.", cls="muted")
        notify_buttons = (Button("Turn on notifications", id="push-on", cls="primary"), Button("Send a test", id="push-test"))
    return shell("Settings",
                 H2("Settings", cls="page-title"),
                 Div(error, cls="error") if error else None, Div(ok, cls="result ok") if ok else None,
                 Section(H3("Accounts", id="accounts"), Ul(*acct_rows, cls="acct-rows") if acct_rows else P("No accounts yet.", cls="muted"),
                         Form(Label("Bluesky handle", Input(name="handle", placeholder="you.bsky.social", required=True,
                                                            autocapitalize="none", autocorrect="off")),
                              Button("Connect Bluesky", cls="primary"), method="post", action="/auth/bluesky/start", cls="row connect"),
                         Form(Label("Mastodon server", Input(name="instance", placeholder="mastodon.social", required=True,
                                                             autocapitalize="none", autocorrect="off")),
                              Button("Connect Mastodon", cls="primary"), method="post", action="/auth/mastodon/start", cls="row connect"),
                         P("Any server that speaks the Mastodon API works too: GoToSocial, Pleroma, Akkoma, Pixelfed and others.",
                           cls="muted"),
                         cls="panel"),
                 Section(H3("Notifications on this device"), notify_intro,
                         Div(*notify_buttons, Span(id="push-status", cls="muted"), cls="row"),
                         Form(Div(*kind_boxes, cls="checks"), Button("Save", cls="small"), method="post",
                              action="/settings/notify"),
                         cls="panel"),
                 x_section,
                 Section(H3("People"), P("Link someone's Bluesky, Mastodon and X accounts so mentions in your "
                                         "cross-posts tag them on every network.", cls="muted"),
                         A("Manage people", href="/people", cls="button"), cls="panel"),
                 Section(H3("Privacy"), P("Anonymous feed stats are ", Strong({"yes": "on", "no": "off"}.get(
                     community.choice(db) or "", "not chosen yet")), ". They help community feed authors and never include "
                     "handles or posts.", cls="muted"), A("Privacy settings", href="/settings/privacy", cls="button"),
                         cls="panel", id="privacy"),
                 Section(H3("Muted accounts"), P("One handle per line. Hidden from custom feeds.", cls="muted"),
                         Form(Textarea("\n".join(db.get("muted", [])), name="muted", rows=4),
                              Button("Save", cls="small"), method="post", action="/settings/muted", cls="stack"),
                         cls="panel"),
                 _browser_settings() if settings.browser else None,
                 Form(Button("Sign out", cls="small"), method="post", action="/logout") if settings.app_password else None,
                 active="/settings")


def _browser_settings():
    return Section(
        H3("This browser", id="browser"),
        P("Everything Broadsheet knows (your sign-ins, feeds and settings) is kept in this browser only. Nothing is "
          "stored on a server. Clearing this site's data in your browser signs you out of everything.", cls="muted"),
        P("To move to another device or browser, download a backup here and restore it there. Keep the file private: "
          "it holds your sign-ins.", cls="muted"),
        Div(Button("Download a backup", id="backup-download", cls="small"), cls="row"),
        Form(Label("Restore a backup", Input(type="file", name="backup", accept=".zip,application/zip", required=True)),
             Button("Restore", cls="small"), method="post", action="/browser/import", enctype="multipart/form-data",
             cls="stack"),
        Form(Button("Erase everything from this browser", cls="small danger"), method="post", action="/browser/erase",
             onsubmit="return confirm('Sign out of every account and delete your feeds and settings from this browser?')"),
        cls="panel")


def _x_settings():
    usage = xapi.usage(db)
    cid = xapi.client_id(db)
    connected = any(a.network == "x" for a in accounts())
    if xapi.credentials_from_env():
        creds = P("The X app's credentials are set on the server.", cls="muted")
    else:
        creds = Details(
            Summary("X app credentials" + (" (saved)" if cid else "")),
            P("Create an app at developer.x.com, switch on OAuth 2.0 with read and write permissions, and add this "
              "callback URL exactly: ", Code(xapi.callback_url()), ". A Native App needs only the client ID; a Web "
              "App also needs the client secret.", cls="muted"),
            Form(Label("Client ID", Input(name="client_id", value=cid, required=True, autocapitalize="none",
                                          autocorrect="off", autocomplete="off")),
                 Label("Client secret", Input(name="client_secret", type="password", autocomplete="off",
                                              placeholder="Saved, leave empty to keep" if xapi.client_secret(db)
                                              else "Only for a Web App")),
                 Button("Save", cls="small"), method="post", action="/settings/x/app", cls="stack"),
            open=not cid)
    return Section(
        H3("X"),
        P("X charges per request. Posting costs $0.015 per post, or $0.20 if it contains a link. Reading costs "
          "$0.005 per post. Reads are off unless you switch them on, and everything stops at your monthly cap.",
          cls="muted"),
        P(Strong(f"${usage['cost']:.2f}"), f" of ${usage['cap']:.2f} used in {usage['month']} · "
          f"{usage['post_reads']} posts read, {usage['posts'] + usage['link_posts']} posted"),
        Div(Span(style=f"width: {min(100, 100 * usage['cost'] / usage['cap']) if usage['cap'] else 0:.0f}%"),
            cls="spend", role="img", aria_label=f"{usage['cost']:.2f} of {usage['cap']:.2f} dollars used"),
        creds,
        A("Connect X account", href="/auth/x/start", cls="button primary") if cid and not connected else None,
        Form(Label(Input(type="checkbox", name="on", value="1", checked=xapi.post_by_default(db), onchange="this.form.submit()"),
                   "Include X when cross-posting (you can untick it per post)"), method="post", action="/settings/x/post"),
        Form(Label(Input(type="checkbox", name="on", value="1", checked=xapi.reading_enabled(db), onchange="this.form.submit()"),
                   "Read my X timeline and mentions (costs money)"), method="post", action="/settings/x"),
        cls="panel", id="x")


@rt("/settings/notify")
async def post(req):
    form = await req.form()
    db.set("notify_kinds", form.getlist("kinds"))
    if settings.browser and db.get("browser_push_sub"):
        try:
            await _browser_push_subscribe(db.get("browser_push_sub"))
        except Exception as e:
            return _back(e)
    return RedirectResponse("/settings?ok=Saved", status_code=303)


@rt("/settings/x")
async def post(on: str = ""):
    db.set("x_read_enabled", bool(on))
    return RedirectResponse("/settings#x", status_code=303)


@rt("/settings/x/post")
async def post(on: str = ""):
    db.set("x_post_by_default", bool(on))
    return RedirectResponse("/settings#x", status_code=303)


@rt("/settings/x/app")
async def post(client_id: str = "", client_secret: str = ""):
    if xapi.credentials_from_env():
        return RedirectResponse("/settings#x", status_code=303)
    if not client_secret.strip():
        client_secret = xapi.client_secret(db)
    xapi.save_credentials(db, client_id, client_secret)
    return RedirectResponse("/settings?ok=X+app+saved#x", status_code=303)


@rt("/settings/muted")
async def post(muted: str = ""):
    db.set("muted", [m.strip().lstrip("@") for m in muted.splitlines() if m.strip()])
    feeds._results.clear()
    return RedirectResponse("/settings?ok=Saved", status_code=303)


@rt("/accounts/remove")
async def post(id: str):
    db.x("delete from accounts where id=?", (id,))
    db.x("delete from posts where account_id=?", (id,))
    return RedirectResponse("/settings?ok=Account+removed", status_code=303)


def _back(err: Exception | str):
    return RedirectResponse("/settings?" + urlencode({"error": str(err)}) + "#accounts", status_code=303)


@rt("/auth/mastodon/start")
async def post(instance: str):
    try:
        return RedirectResponse(await mastodon.start_login(db, instance), status_code=303)
    except Exception as e:
        return _back(e)


@rt("/auth/mastodon/callback")
async def get(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _back(error or "Sign-in was cancelled.")
    try:
        rec = await mastodon.finish_login(db, code, state)
    except Exception as e:
        return _back(e)
    return RedirectResponse("/settings?" + urlencode({"ok": f"Connected {rec.handle}"}), status_code=303)


@rt("/auth/bluesky/start")
async def post(handle: str):
    try:
        return RedirectResponse(await bluesky_oauth.start_login(db, handle), status_code=303)
    except Exception as e:
        log.exception("bluesky start")
        return _back(e)


@rt("/auth/bluesky/callback")
async def get(code: str = "", state: str = "", iss: str = "", error: str = "", error_description: str = ""):
    if error or not code:
        return _back(error_description or error or "Sign-in was cancelled.")
    try:
        rec = await bsky.finish_login(db, code, state, iss)
    except Exception as e:
        log.exception("bluesky callback")
        return _back(e)
    return RedirectResponse("/settings?" + urlencode({"ok": f"Connected {rec.handle}"}), status_code=303)


@rt("/auth/x/start")
async def get():
    try:
        return RedirectResponse(await xapi.start_login(db), status_code=303)
    except Exception as e:
        return _back(e)


@rt("/auth/x/callback")
async def get(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return _back(error or "Sign-in was cancelled.")
    try:
        rec = await xapi.finish_login(db, code, state)
    except Exception as e:
        return _back(e)
    return RedirectResponse("/settings?" + urlencode({"ok": f"Connected @{rec.handle} on X"}), status_code=303)


@rt("/oauth/bluesky/client-metadata.json")
def get():
    return JSONResponse(bluesky_oauth.client_metadata())


@rt("/oauth/bluesky/jwks.json")
def get():
    return JSONResponse(bluesky_oauth.jwks(db))


# ---------- push ----------

# In the browser build there's no server to push from, so Mastodon pushes to the browser itself: we subscribe with
# the Mastodon server's key and it sends straight to the browser's push service.

def _browser_push_accounts():
    return [a for a in accounts() if a.network == "mastodon"]


async def _browser_push_subscribe(sub: dict) -> int:
    accts = _browser_push_accounts()
    if not accts:
        raise NetworkError("Connect a Mastodon account first.")
    # A browser holds one push subscription, tied to one server's key: that covers every account on that server.
    host = mastodon.normalise_instance(accts[0].rec.server)
    same = [a for a in accts if mastodon.normalise_instance(a.rec.server) == host]
    for a in same:
        await mastodon.subscribe_push(a.rec, sub, push.notify_kinds(db))
    db.set("browser_push_sub", sub)
    return len(same)


@rt("/push/key")
async def get():
    if settings.browser:
        accts = _browser_push_accounts()
        if not accts:
            return JSONResponse({"error": "Connect a Mastodon account first."})
        try:
            return JSONResponse({"publicKey": await mastodon.vapid_key(db, accts[0].rec.server)})
        except NetworkError as e:
            return JSONResponse({"error": str(e)})
    return JSONResponse({"publicKey": push.vapid_keys(db)["public"]})


@rt("/push/subscribe")
async def post(req):
    sub = await req.json()
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        return JSONResponse({"ok": False}, status_code=400)
    if settings.browser:
        try:
            n = await _browser_push_subscribe(sub)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        return JSONResponse({"ok": True, "message": f"Mastodon notifications are on for {n} "
                                                    f"{'account' if n == 1 else 'accounts'} in this browser."})
    push.add_subscription(db, sub)
    return JSONResponse({"ok": True})


@rt("/push/unsubscribe")
async def post(req):
    push.remove_subscription(db, (await req.json()).get("endpoint", ""))
    return JSONResponse({"ok": True})


@rt("/push/test")
async def post():
    n = await push.send(db, "Broadsheet", "Notifications are working.", "/notifications", tag="test")
    return JSONResponse({"sent": n})


# ---------- the browser build's own data ----------

if settings.browser:
    from . import browser

    @rt("/browser/export")
    def get():
        return Response(browser.export_zip(db, settings.data_dir), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="broadsheet-backup.zip"'})

    @rt("/browser/import")
    async def post(req):
        upload = (await req.form()).get("backup")
        try:
            browser.import_zip(db, settings.data_dir, await upload.read())
        except Exception as e:
            return RedirectResponse("/settings?" + urlencode({"error": f"Couldn't restore that backup: {e}"}) + "#browser",
                                    status_code=303)
        feeds._results.clear()
        return RedirectResponse("/settings?ok=Backup+restored", status_code=303)

    @rt("/browser/erase")
    def post():
        browser.erase(db, settings.data_dir)
        seed_feeds()
        feeds._results.clear()
        return RedirectResponse("/settings?ok=Everything+was+erased+from+this+browser", status_code=303)


# ---------- PWA files ----------

@rt("/manifest.webmanifest")
def get():
    return Response(json.dumps({
        "name": "Broadsheet", "short_name": "Broadsheet", "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#ffffff",
        "description": "Read and post across Bluesky, Mastodon and more.",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
        "shortcuts": [{"name": "New post", "url": "/compose"}, {"name": "Notifications", "url": "/notifications"}],
    }), media_type="application/manifest+json")


@rt("/sw.js")
def get():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@rt("/static/{fname:path}")
def get(fname: str):
    path = (STATIC / fname).resolve()
    if not str(path).startswith(str(STATIC)) or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@rt("/favicon.ico")
def get():
    return FileResponse(STATIC / "icons" / "icon-192.png")


def main():
    import uvicorn
    uvicorn.run("crossposter.app:app", host="0.0.0.0", port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
