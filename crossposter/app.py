"""The FastHTML app: routes for reading, posting, notifications, feeds, settings and sign-in."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fasthtml.common import (A, Beforeware, Button, Code, Details, Div, Form, H1, H2, H3, Hidden, Input, Label, Li,
                             Link, Meta, Option, P, Pre, Script, Section, Select, Small, Span, Strong, Summary, Textarea,
                             Ul, fast_app)
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import adapters as registry
from . import cache, feeds, people, push
from .adapters import NetworkError, Upload
from .adapters import bluesky as bsky
from .adapters import bluesky_oauth, mastodon, x as xapi
from .config import settings
from .crypto import app_secret
from .db import DB
from .models import Notification
from .richtext import length_for
from .ui import (NETWORK_LABELS, safe, account_chip, avatar, badge, error_box, load_more, notification_row, page, post_card,
                 profile_url)

log = logging.getLogger("crossposter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
db = DB(settings.db_path)


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
    task = asyncio.create_task(push.poller(db))
    yield
    task.cancel()


def auth_check(req, session):
    if not settings.app_password or session.get("auth"):
        return
    if req.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


hdrs = (
    Meta(name="theme-color", content="#4f46e5"),
    Meta(name="color-scheme", content="light dark"),
    Meta(name="apple-mobile-web-app-capable", content="yes"),
    Meta(name="mobile-web-app-capable", content="yes"),
    Meta(name="apple-mobile-web-app-status-bar-style", content="default"),
    Meta(name="apple-mobile-web-app-title", content="Cross-poster"),
    Link(rel="manifest", href="/manifest.webmanifest"),
    Link(rel="apple-touch-icon", href="/static/icons/apple-touch-icon.png"),
    Link(rel="icon", type="image/png", href="/static/icons/icon-192.png"),
    Link(rel="stylesheet", href="/static/app.css"),
    Script(src="/static/app.js", defer=True),
)

app, rt = fast_app(
    pico=False, hdrs=hdrs, secret_key=app_secret(), lifespan=lifespan, title="Cross-poster",
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


def shell(title: str, *content, active: str = "/", wide: bool = False):
    return page(title, *content, active=active, unread=unread(), wide=wide)


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
    return (page("Sign in", Section(H1("Cross-poster"), P("Enter the app password to continue."),
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
    tabs = []
    for lf in feeds.all_feeds():
        name = lf.feed.name if lf.feed else lf.slug
        tabs.append(A(name, href=f"/?custom={lf.slug}", cls="tab" + (" active" if custom == lf.slug else ""),
                      title="Custom feed"))
    multi = {}
    for a in accts:
        multi[a.network] = multi.get(a.network, 0) + 1
    for a in accts:
        for f in await native_feeds(a):
            if not f.pinned and not (feed == f.source and acct == a.account_id):
                continue
            label = f.name + (f" · {a.rec.handle}" if multi[a.network] > 1 else "")
            tabs.append(A(badge(a.network), label, href="/?" + urlencode({"feed": f.source, "acct": a.account_id}),
                          cls="tab" + (" active" if (feed == f.source and acct == a.account_id) else "")))
    tabs.append(A("All feeds…", href="/feeds", cls="tab more-tab"))
    src = ("/timeline?" + urlencode({"custom": custom}) if custom
           else "/timeline?" + urlencode({"feed": feed, "acct": acct}))
    return shell("Home", Div(*tabs, cls="tabs"),
                 Div(Div(Span(cls="spinner"), "Loading", cls="more"), hx_get=src, hx_trigger="load", cls="timeline"))


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
        checked = (a.account_id == reply_acct) if reply else (a.network != "x")
        disabled = bool(reply) and a.account_id != reply_acct
        boxes.append(Label(Input(type="checkbox", name="accounts", value=a.account_id, checked=checked, disabled=disabled,
                                 data_network=a.network, data_limit=str(limit), cls="acct-box"),
                           avatar(a.me(), "xs"), Span(a.rec.handle), badge(a.network),
                           Span("", cls="counter", data_for=a.account_id),
                           Small("experimental, costs money", cls="muted") if a.experimental else None,
                           cls="acct-option"))
    overrides = Details(Summary("Customise the text per network"),
                        *[Label(Span(a.rec.handle, " ", badge(a.network)),
                                Textarea(name=f"text_{a.account_id}", rows=3, placeholder="Leave empty to use the main text",
                                         data_override=a.account_id, data_network=a.network, cls="mentionable"))
                          for a in accts],
                        cls="overrides")
    form = Form(
        Hidden(name="reply", value=reply),
        Textarea(prefill, name="text", id="compose-text", rows=6, placeholder="What's happening? Type @ to mention someone",
                 autofocus=True, required=True, cls="mentionable", autocapitalize="sentences"),
        Div(*boxes, cls="acct-list"),
        overrides,
        Div(Label("Images (up to 4)", Input(type="file", name="images", accept="image/*", multiple=True, id="images")),
            Div(id="alt-fields"), cls="images"),
        Div(Label("Content warning", Input(name="cw", placeholder="Optional"), cls="grow"),
            Label(Input(type="checkbox", name="thread", value="on", checked=True),
                  "Split long text into a thread"), cls="row"),
        Div(Button("Post", cls="primary", id="post-btn"), Span(cls="htmx-indicator spinner"), cls="row end"),
        hx_post="/compose", hx_encoding="multipart/form-data", hx_target="#compose-result", hx_swap="innerHTML",
        hx_indicator=".htmx-indicator", cls="compose stack", id="compose-form")
    preview = Div(id="mention-preview", hx_post="/compose/preview", hx_trigger="input delay:700ms from:#compose-form, "
                  "change from:#compose-form, load", hx_include="#compose-form textarea, #compose-form .acct-box, "
                  "#compose-form [name=reply]", aria_live="polite")
    return shell("Post", H2("Reply" if reply else "New post", cls="page-title"),
                 post_card(reply_post) if reply_post else None, form, preview, Div(id="compose-result"),
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
        rows.append(Li(Strong(original), " ", Small(person.name, cls="muted") if person and person.name else None,
                       Ul(*[Li(badge(a.network), " ", Code(m.replacement), " ", Small(HOW[m.how], cls="muted"),
                               cls="how-" + m.how) for a, m in items]), fix))
    long = [Li(badge(a.network), f" {a.rec.handle}: {used} of {limit} characters after mentions are rewritten")
            for a, used, limit in outcomes.get("", [])]
    return Section(H3("Mentions on each network"), Ul(*rows, cls="mention-rows"),
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
    return shell("Notifications", H2("Notifications", cls="page-title"), tabs, Div(*body, cls="notifs"),
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

@rt("/feeds")
async def get():
    cards = []
    for lf in feeds.all_feeds():
        f = lf.feed
        cards.append(Section(
            Div(H3(f.name if f else lf.slug), Small(f"{lf.slug}.py" if lf.path else "built in", cls="muted"), cls="feed-head"),
            P(f.description) if f and f.description else None,
            P(Strong("Error: "), Code(lf.error), cls="error") if lf.error else None,
            P("Sources: ", *[Code(s) for s in f.sources], cls="sources") if f else None,
            Div(A("Open", href=f"/?custom={lf.slug}", cls="button") if f else None,
                A("Edit", href=f"/feeds/{lf.slug}/edit", cls="button") if lf.path else None, cls="row"),
            Details(Summary("View source"), Pre(Code(lf.source, cls="language-python"))), cls="panel feed-card"))
    native = []
    for a in accounts():
        fs = await native_feeds(a)
        native.append(Li(badge(a.network), " ", Strong(a.rec.handle), Ul(*[
            Li(A(f.name, href="/?" + urlencode({"feed": f.source, "acct": a.account_id})), " ", Code(f.source, cls="muted"))
            for f in fs])))
    return shell("Feeds", H2("Your custom feeds", cls="page-title"),
                 P("Custom feeds are Python files. Each one picks posts from the sources it lists and ranks them with a ",
                   Code("score()"), " function. Edit one here or in your editor; changes apply on save.", cls="muted"),
                 A("New feed", href="/feeds/new/edit", cls="button primary"), *cards,
                 H2("Native feeds", cls="page-title"),
                 P("Use these source names in a custom feed's sources list.", cls="muted"),
                 Ul(*native, cls="native-feeds"), active="/feeds")


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


@rt("/feeds/{slug}/edit")
async def get(slug: str):
    is_new = slug == "new"
    lf = None if is_new else feeds.get_feed(slug)
    code = TEMPLATE if is_new else (lf.source if lf else "")
    return shell("Edit feed", H2("New feed" if is_new else f"Edit {slug}.py", cls="page-title"),
                 Form(Label("File name", Input(name="slug", value="" if is_new else slug, placeholder="my_feed",
                                               required=True, pattern="[A-Za-z0-9_]+", readonly=not is_new)),
                      Textarea(code, name="code", rows=24, cls="code", spellcheck="false"),
                      Div(Button("Save", cls="primary", formaction="/feeds/save", formmethod="post"),
                          Button("Preview", type="button", hx_post="/feeds/preview", hx_target="#preview",
                                 hx_include="closest form", hx_indicator=".htmx-indicator"),
                          Span(cls="htmx-indicator spinner"), cls="row"),
                      method="post", action="/feeds/save", cls="stack"),
                 Div(id="preview", cls="preview"), wide=True, active="/feeds")


@rt("/feeds/save")
async def post(slug: str, code: str):
    slug = _safe_slug(slug)
    settings.feeds_dir.mkdir(parents=True, exist_ok=True)
    (settings.feeds_dir / f"{slug}.py").write_text(code.replace("\r\n", "\n"))
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
    x_section = Section(
        H3("X (experimental)"),
        P("X charges per request. Posting costs $0.015 per post, or $0.20 if it contains a link. Reading costs "
          "$0.005 per post. Reads are off unless you switch them on, and everything stops at your monthly cap.",
          cls="muted"),
        P(Strong(f"${usage['cost']:.2f}"), f" of ${usage['cap']:.2f} used in {usage['month']} · "
          f"{usage['post_reads']} posts read, {usage['posts'] + usage['link_posts']} posted"),
        Form(Label(Input(type="checkbox", name="on", value="1", checked=xapi.reading_enabled(db), onchange="this.form.submit()"),
                   "Read my X timeline and mentions (costs money)"), method="post", action="/settings/x"),
        A("Connect X account", href="/auth/x/start", cls="button") if settings.x_client_id else
        P("Set X_ENABLED=true, X_CLIENT_ID and X_CLIENT_SECRET to connect an account.", cls="muted"),
        cls="panel") if settings.x_enabled else Section(
        H3("X (experimental)"), P("X is switched off. Set X_ENABLED=true and the X app credentials to try it.", cls="muted"),
        cls="panel")
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
                 Section(H3("Notifications on this device"),
                         P("On iPhone, first add this app to your home screen (Share, then Add to Home Screen) and open it "
                           "from there. Then switch notifications on.", cls="muted"),
                         Div(Button("Turn on notifications", id="push-on", cls="primary"),
                             Button("Send a test", id="push-test"), Span(id="push-status", cls="muted"), cls="row"),
                         Form(Div(*kind_boxes, cls="checks"), Button("Save", cls="small"), method="post",
                              action="/settings/notify"),
                         cls="panel"),
                 x_section,
                 Section(H3("People"), P("Link someone's Bluesky, Mastodon and X accounts so mentions in your "
                                         "cross-posts tag them on every network.", cls="muted"),
                         A("Manage people", href="/people", cls="button"), cls="panel"),
                 Section(H3("Muted accounts"), P("One handle per line. Hidden from custom feeds.", cls="muted"),
                         Form(Textarea("\n".join(db.get("muted", [])), name="muted", rows=4),
                              Button("Save", cls="small"), method="post", action="/settings/muted", cls="stack"),
                         cls="panel"),
                 Form(Button("Sign out", cls="small"), method="post", action="/logout") if settings.app_password else None,
                 active="/settings")


@rt("/settings/notify")
async def post(req):
    form = await req.form()
    db.set("notify_kinds", form.getlist("kinds"))
    return RedirectResponse("/settings?ok=Saved", status_code=303)


@rt("/settings/x")
async def post(on: str = ""):
    db.set("x_read_enabled", bool(on))
    return RedirectResponse("/settings", status_code=303)


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

@rt("/push/key")
def get():
    return JSONResponse({"publicKey": push.vapid_keys(db)["public"]})


@rt("/push/subscribe")
async def post(req):
    sub = await req.json()
    if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
        return JSONResponse({"ok": False}, status_code=400)
    push.add_subscription(db, sub)
    return JSONResponse({"ok": True})


@rt("/push/unsubscribe")
async def post(req):
    push.remove_subscription(db, (await req.json()).get("endpoint", ""))
    return JSONResponse({"ok": True})


@rt("/push/test")
async def post():
    n = await push.send(db, "Cross-poster", "Notifications are working.", "/notifications", tag="test")
    return JSONResponse({"sent": n})


# ---------- PWA files ----------

@rt("/manifest.webmanifest")
def get():
    return Response(json.dumps({
        "name": "Cross-poster", "short_name": "Cross-poster", "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#f7f7fb", "theme_color": "#4f46e5",
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
