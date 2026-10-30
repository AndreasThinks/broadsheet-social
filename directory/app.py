"""Broadsheet's website: the app downloads and desktop updates (see releases.py), and the community feed directory, which
lists shared feeds with their download counts and anonymous weekly stats, takes submissions from the app, and gives
maintainers a review queue.

Run locally with `python -m directory.app` (http://127.0.0.1:5002). Environment:
    DATA_DIR        where directory.db lives (default ./directory-data; /data on Railway)
    ADMIN_PASSWORD  password for the review queue (no password: the queue is closed)
    SECRET_KEY      signs the maintainer's session cookie
    SEED_CREDIT     credit shown on the starter feeds listed on first start
Downloads are stored in DATA_DIR/releases.
"""
from __future__ import annotations

import ast
import hmac
import json
import math
import os
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fasthtml.common import (A, Article, Button, Code, Details, Div, Footer, Form, H1, H2, H3, Header, Input, Label, Li,
                             Link, Main, Meta, Nav, Noscript, NotStr, Ol, Option, P, Pre, Script, Section, Select, Span, Strong,
                             Summary, Table, Tbody, Td, Template, Textarea, Th, Thead, Title, Tr, Ul, fast_app)
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .releases import APPS, DESKTOP, Refused, Releases, verify
from .store import MIN_REPORTS, Rejected, Store

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
WEB = REPO / "web"
DATA_DIR = Path(os.environ.get("DATA_DIR", "directory-data"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SOURCE_URL = "https://github.com/AndreasThinks/broadsheet-social"
NETS = {"bluesky": "Bluesky", "mastodon": "Mastodon", "x": "X"}

store = Store(DATA_DIR / "directory.db")
store.seed(REPO / "feeds", os.environ.get("SEED_CREDIT", "AndreasThinks"))
releases = Releases(DATA_DIR / "releases")

hdrs = (Meta(name="color-scheme", content="light dark"), Link(rel="stylesheet", href="/static/site.css"),
        Link(rel="icon", type="image/png", href="/static/icons/icon-192.png"))
app, rt = fast_app(pico=False, hdrs=hdrs, secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
                   session_cookie="broadsheet_feeds", sess_https_only=os.environ.get("HTTPS", "1") == "1")
app.router.routes[:] = [r for r in app.router.routes if "{ext:static}" not in getattr(r, "path", "")]


# ---------- helpers ----------

def n(v) -> str:
    return f"{v:,}"


def pct(v: float) -> str:
    return f"{v * 100:.0f}%" if v >= 0.1 else f"{v * 100:.1f}%"


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%-d %b %Y")


def networks(sources: list[str]) -> list[str]:
    return list(dict.fromkeys(s.split(":", 1)[0] for s in sources))


def pills(sources):
    return [Span(NETS.get(net, net), cls=f"pill pill-{net}") for net in networks(sources)]


def is_admin(session) -> bool:
    return bool(ADMIN_PASSWORD) and session.get("admin") is True


def layout(title: str, *content, active: str = "/", session=None, main_cls: str = "page"):
    links = [("/feeds", "Feeds"), ("/download", "Download"), ("/privacy", "Privacy")]
    if session is not None and is_admin(session):
        links.append(("/review", f"Review {len(store.queue()) or ''}".strip()))
    return (Title(f"{title} · Broadsheet"),
            Header(Div(A("Broadsheet", href="/", cls="brand"),
                       Nav(*[A(label, href=h, cls="active" if h == active else None,
                               aria_current="page" if h == active else None) for h, label in links],
                           cls="nav", aria_label="Sections"),
                       A("Open Broadsheet", href="/web", cls="button primary open"), cls="wrap"), cls="masthead"),
            Main(*content, cls=f"wrap {main_cls}"),
            Footer(Div(Span("Broadsheet runs on your device or in your browser. Your accounts and posts never pass through "
                            "this site."),
                       A("Source on GitHub", href=SOURCE_URL), A("Privacy", href="/privacy"), cls="wrap"), cls="footer"))


def numbers_box():
    return Section(
        H2("In short"),
        Ul(Li(Span("✓", cls="yes"), Span("Downloads are counted, with nothing kept about who downloaded.")),
           Li(Span("✓", cls="yes"), Span("Apps that said yes send a few counts per community feed once a week.")),
           Li(Span("✕", cls="no"), Span("Never handles, accounts, posts, follows, or feeds people wrote themselves.")),
           Li(Span("✕", cls="no"), Span("No device ID and no IP addresses kept.")),
           Li(Span("✓", cls="yes"), Span(f"A weekly figure only shows once {MIN_REPORTS} or more apps report it.")),
           cls="checks"))


def sort_key(sort: str):
    def users(f):
        return (f["stats"] or {}).get("users", 0)
    return {
        "kept": lambda f: ((f["stats"] or {}).get("long_term_share", -1), f["downloads"]),
        "rising": lambda f: ((f["stats"] or {}).get("users_change") or -9, users(f)),
        "downloads": lambda f: (users(f), f["downloads"]),
        "newest": lambda f: (f["created_at"],),
    }.get(sort, lambda f: (users(f), f["downloads"]))


def spark(stats_weeks: list[dict]) -> str:
    vals = [w.get("users", 0) if w["shown"] else None for w in stats_weeks]
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    return " ".join(f"{2 + i * 52 / (len(vals) - 1):.1f},{18 - (v - lo) / span * 16:.1f}" for i, v in pts)


def try_link(slug: str) -> str:
    """The feed's page inside the browser version, where it can be added (see web/shell.js)."""
    return f"/web/feeds/community/{slug}"


def is_new(f: dict) -> bool:
    return datetime.now().timestamp() - f["created_at"] < 30 * 86400


def feed_card(f: dict, cta: bool = True):
    """One feed in a grid: shows numbers only once the feed has some."""
    s = f["stats"]
    usage = (f"{n(s['users'])} people this week" if s else
             f"{n(f['downloads'])} download{'s' if f['downloads'] != 1 else ''}" if f["downloads"] else None)
    return Article(
        Span("New", cls="new-badge") if is_new(f) and not s else None,
        H3(A(f["name"], href=f"/feeds/{f['slug']}")),
        P(f["description"], cls="desc"),
        Div(Span(f"by {f['credit']}" if f["credit"] else "Shared anonymously"), Span(*pills(f["sources"])), cls="card-foot"),
        P(usage, cls="usage") if usage else None,
        Div(A("Open in your browser", href=try_link(f["slug"]), cls="button"),
            A("Details", href=f"/feeds/{f['slug']}"), cls="row") if cta else
        Div(A("Open in your browser", href=try_link(f["slug"])), cls="row"),
        cls="feed-card")


# ---------- the app and its downloads ----------

PLATFORMS = (
    ("android", "Android", "Broadsheet-android.apk", "Download APK", "Android 8 or later",
     ("Open the downloaded file.", "Allow installs from your browser when Android asks.",
      "To update, install a new APK over the old one, or let Obtainium do it.")),
    ("windows", "Windows", "Broadsheet-windows.zip", "Download for Windows", "",
     ("Unzip the download.", ("Open ", Code("Broadsheet.exe"), "."),
      ("Windows warns you the first time. Choose ", Strong("More info"), ", then ", Strong("Run anyway"), "."))),
    ("mac", "macOS", "Broadsheet-macos.zip", "Download for Mac", "",
     ("Unzip it and move Broadsheet to Applications.",
      ("The first time, right-click it and choose ", Strong("Open"), "."))),
)
OS_NAMES = {key: label for key, label, *_ in PLATFORMS}
# Find the visitor's platform: name the download button after it, and on Android lead with it (that's where the
# installed app adds most). Old links to /#download now go to the Download page.
PICK_OS = Script("""if (location.hash === "#download") location.replace("/download");
const ua = navigator.userAgent;
const os = /Android/.test(ua) ? "android" : /Windows/.test(ua) ? "windows" : /Macintosh|Mac OS X/.test(ua) && !/Mobile/.test(ua) ? "mac" : "";
const names = %s;
const dl = document.getElementById("door-download"), web = document.getElementById("door-web");
if (dl && os) { dl.textContent = "Download for " + names[os]; dl.href = "/download#" + os; }
if (dl && os === "android") { dl.classList.add("primary"); web.classList.remove("primary"); dl.after(web); }
const mine = os && document.querySelector(`.platform[data-os="${os}"]`);
if (mine) { mine.classList.add("mine"); mine.parentNode.prepend(mine); }""" % json.dumps(OS_NAMES))


NET_ICONS = {
    "bluesky": '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M6.3 4.4c2.2 1.6 4.6 5 5.7 6.8 1.1-1.8 3.5-5.2 5.7-6.8 '
               '1.6-1.2 4.3-2.1 4.3.8 0 .6-.3 4.9-.5 5.6-.7 2.4-3.2 3-5.4 2.7 3.9.7 4.9 2.9 2.8 5.1-4 4.1-5.8-1-6.2-2.4L12 '
               '14.3l-.7 1.9c-.4 1.4-2.2 6.5-6.2 2.4-2.1-2.2-1.1-4.4 2.8-5.1-2.2.3-4.7-.3-5.4-2.7C2.3 10.1 2 5.8 2 5.2c0-2.9 '
               '2.7-2 4.3-.8z"/></svg>',
    "mastodon": '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M4 4.5h16a1.5 1.5 0 0 1 1.5 1.5v9a4 4 0 0 1-4 4H9.5L5 '
                '22v-3.2A2 2 0 0 1 2.5 17V6A1.5 1.5 0 0 1 4 4.5z"/><path class="m" fill="none" stroke-width="2.4" '
                'stroke-linecap="round" d="M7.5 15V10a2.25 2.25 0 0 1 4.5 0v3M12 10a2.25 2.25 0 0 1 4.5 0v5"/></svg>',
    "x": '<svg viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" '
         'd="M5 4l14 16M19 4L5 20"/></svg>',
}
# Made-up people for the front-page picture: name, networks (first is where the post arrives first), when, replies, likes,
# drawn face (background, skin, hair, shirt, hair style), text. The first three are on screen; the rest arrive one by one.
PICTURE_POSTS = (
    ("Priya Raman", ("bluesky", "mastodon"), "12m", 4, 48, ("#cfe0fb", "#a86b4c", "#1f1a17", "#2f5d8a", "long"),
     "New chart: solar's share of global electricity passed 9% this year, faster than any forecast from 2015."),
    ("Tom Ellery", ("mastodon",), "40m", 7, 31, ("#e3defb", "#f0c9a8", "#8a5a2b", "#5a4f8f", "short"),
     "Every service form should start by asking what you're trying to do, then ask only what that needs."),
    ("Jo Mensah", ("bluesky",), "1h", 2, 19, ("#fbe3cf", "#6b4130", "#1a1311", "#b4532a", "curly"),
     "Writing up this week's newsletter now. Shorter than usual, which is good news for everyone."),
    ("Sam Okafor", ("x",), "now", 3, 12, ("#e6e6e6", "#5a3a2a", "#141010", "#2b2b2b", "beard"),
     "Train is 20 minutes late, which finally gives me time to read the transport white paper."),
    ("Aiko Tanaka", ("bluesky", "mastodon"), "now", 6, 40, ("#fde2e4", "#f1cfae", "#26201d", "#a83f5a", "long"),
     "Spent the morning in the archive. Found a 1952 bus timetable with better typography than anything we ship today."),
    ("Lena Fischer", ("mastodon",), "now", 5, 22, ("#dff3e6", "#f3d3b8", "#c99a4a", "#2f7a55", "bun"),
     "Our open data portal has an API now. The docs are short and the rate limits are generous."),
    ("Marcus Hale", ("x",), "now", 9, 27, ("#fff1c9", "#e9b994", "#6b6b6b", "#3f5b3a", "bald"),
     "Most dashboards should be a sentence."),
    ("Nadia Costa", ("bluesky", "x"), "now", 3, 35, ("#efe2fb", "#d6a07c", "#3b2416", "#6b3fa0", "curly"),
     "Small win: the bike lane on Mill Road finally connects to the station."),
    ("Dev Patel", ("mastodon",), "now", 1, 16, ("#d9f0f5", "#9a6447", "#191414", "#1f6f80", "short"),
     "Reminder that the library lends out tools now. I borrowed a drill and a moisture meter."),
    ("Ruth Adeyemi", ("x",), "now", 2, 21, ("#fde7d6", "#4e3024", "#120d0b", "#c0702f", "bun"),
     "Writing documentation is just future you leaving notes for confused you."),
)


def face(bg, skin, hair, shirt, style):
    """A small drawn portrait, so the picture has faces without photos of anyone real."""
    back = cap = extra = ""
    if style == "long":
        back = f'<rect x="10" y="8.5" width="20" height="24" rx="10" fill="{hair}"/>'
    if style == "bun":
        back = f'<circle cx="20" cy="7.4" r="3.8" fill="{hair}"/>'
    if style == "curly":
        cap = "".join(f'<circle cx="{20 + 7.6 * math.cos(math.radians(a)):.1f}" cy="{17 + 7.6 * math.sin(math.radians(a)):.1f}" '
                      f'r="3.3" fill="{hair}"/>' for a in (200, 225, 250, 270, 290, 315, 340))
    elif style != "bald":
        cap = f'<circle cx="20" cy="15.8" r="8.4" fill="{hair}"/>'
    if style == "beard":
        extra = f'<path d="M12.6 18.5Q20 31 27.4 18.5Q20 23.5 12.6 18.5Z" fill="{hair}"/>'
    return Span(NotStr(f'<svg viewBox="0 0 40 40"><rect width="40" height="40" fill="{bg}"/>{back}'
                       f'<path d="M5 41Q6 28.5 20 28.5Q34 28.5 35 41Z" fill="{shirt}"/>'
                       f'<rect x="17" y="22" width="6" height="7" fill="{skin}"/>{cap}'
                       f'<circle cx="20" cy="18" r="7.6" fill="{skin}"/>{extra}</svg>'), cls="pic-face")


def picture_post(name, nets, when, replies, likes, look, text, arriving=False):
    """One post in the picture. An arriving cross-post starts on its first network; home.js adds the rest."""
    shown = nets[:1] if arriving else nets
    return Div(Div(Span(face(*look), Span(*[Span(NotStr(NET_ICONS[k]), cls=f"pic-badge {k}") for k in shown], cls="pic-badges"),
                        cls="pic-av"),
                   Div(Div(Strong(name), Span(f" · {when}"), cls="pic-who"), P(text),
                       P(f"Also on {' and '.join(NETS[k] for k in nets[1:])} · shown once", cls="pic-also",
                         hidden=arriving or None) if len(nets) > 1 else None,
                       Div(Span(*[x for k in shown for x in (" + ", Span(NETS[k], cls=f"net-{k}"))][1:], cls="pic-nets"),
                           Span("↩ ", Span(replies)), Span("♥ ", Span(likes, cls="pic-likes")), cls="pic-meta")),
                   cls="pic-post"),
               cls="pic-slot", data_nets=" ".join(nets) if arriving and len(nets) > 1 else None)


def app_picture():
    """A drawing of the reading view, with made-up people, so the front page shows what Broadsheet looks like. home.js keeps
    it ticking over: new posts arrive from the queue, likes count up, and a post seen on a second network folds into one."""
    return Div(Div(Strong("Broadsheet"), Span("Read", cls="on"), Span("Activity"), Span("Feeds"), cls="pic-bar"),
               Div(Span("Following", cls="on"), Span("Across networks"), Span("Links worth reading"), Span("Quiet posters"),
                   cls="pic-tabs"),
               Div(cls="pic-pill"),
               Div(*[picture_post(*p) for p in PICTURE_POSTS[:3]], cls="pic-feed"),
               Template(*[picture_post(*p, arriving=True) for p in PICTURE_POSTS[3:]], cls="pic-queue"),
               cls="app-picture", aria_hidden="true")


@rt("/")
def get(session):
    rel = releases.current()
    top = sorted(store.feeds(), key=sort_key("downloads"), reverse=True)[:3]
    direct = [A(label, href=f"/downloads/{fname}") for _, label, fname, *_ in PLATFORMS]
    yes, no = Span("Yes", cls="yes"), Span("No", cls="muted")
    return layout("Read and post across your networks", Div(
        Div(Div(P("For Bluesky and Mastodon · X in the apps", cls="kicker"),
                H1("All your timelines, read like one paper."),
                P("Broadsheet puts Bluesky and Mastodon in one calm reading view, posts to both at once, and ranks your "
                  "timeline with feeds you can read and change.", cls="dek"),
                Div(A("Open in your browser", href="/web", cls="button primary large", id="door-web"),
                    A("Download the app", href="/download", cls="button large", id="door-download"), cls="row doors"),
                P(Span("No sign-up"), Span("Free and open source"), Span("Your accounts stay on your device"), cls="reassure"),
                P("Download directly: ", *[x for a in direct for x in (a, " · ")][:-1], cls="muted small") if rel else
                P("The apps are coming soon.", cls="muted small"), cls="hero-text"),
            app_picture(), cls="hero"),
        Div(Section(H3("One timeline, every network"),
                    P("Posts from Bluesky and Mastodon together. A post cross-posted to both shows once.")),
            Section(H3("Feeds you can read"),
                    P("Each feed is a short ranking rule. Use the community's, or write your own.")),
            Section(H3("Nothing on our server"),
                    P("The app runs on your device or in your browser. This site never sees your accounts. ",
                      A("What we keep", href="/privacy"))), cls="three"),
        Section(H2("Browser or app?"),
                Div(Table(Thead(Tr(Th(""), Th("In your browser"), Th("Windows and Mac"), Th("Android"))),
                          Tbody(Tr(Th("Getting started"), Td("Open the page"), Td("Download and unzip"), Td("Install the APK")),
                                Tr(Th("Bluesky and Mastodon"), Td(yes), Td(yes), Td(yes)),
                                Tr(Th("X"), Td(no), Td(yes), Td(yes)),
                                Tr(Th("Alerts while closed"), Td("Mastodon only"), Td(no), Td("About every 15 minutes")),
                                Tr(Th("Updates"), Td("Automatic"), Td("Automatic"), Td("Install over it, or use Obtainium")),
                                Tr(Th("Where your data lives"), Td("This browser"), Td("Your user folder"), Td("The app"))),
                          cls="compare"), cls="scroll"),
                P("Clearing your browser's data for this site signs the browser version out, so keep a backup from its "
                  "Settings. The first visit downloads about 20 MB.", cls="muted small"), cls="block"),
        Section(H2("From the community"),
                Div(*[feed_card(f, cta=False) for f in top], cls="feed-cards three-up") if top else None,
                P(A("All community feeds →", href="/feeds")), cls="block") if top else None,
        cls="home-body"), PICK_OS, Script(src="/static/home.js", defer=True), active="/", session=session, main_cls="home")


@rt("/download")
def get(session):
    rel = releases.current()
    platforms = []
    for key, label, fname, button, needs, steps in PLATFORMS:
        size = f"{rel['sizes'][fname] / 1048576:.0f} MB" if rel and fname in rel["sizes"] else ""
        platforms.append(Section(
            Div(H2(label), Span(" · ".join(x for x in (needs, size) if x), cls="muted small"), cls="platform-head"),
            A(button, href=f"/downloads/{fname}", cls="button primary") if rel else Span("Coming soon", cls="muted"),
            Ol(*[Li(*(s if isinstance(s, tuple) else (s,))) for s in steps]),
            cls="platform", data_os=key, id=key))
    faq = (("How do updates work?",
            "The desktop apps fetch new versions in the background and switch over the next time you open them. The "
            "browser version is always the latest. On Android, install the new APK over the old one."),
           ("Can Android update automatically?",
            ("Yes, with ", A("Obtainium", href="https://obtainium.imranr.dev/"),
             ". Add this site's front page as the source and it finds new APKs.")),
           ("Will I get notifications?",
            "On Android, the app checks for replies, mentions, quotes and follows about every 15 minutes, even when it's "
            "closed (Android may check less often to save battery). The browser version can notify you about Mastodon "
            "while the page is closed, and about Bluesky only while it's open."),
           ("Which version is this?",
            f"Built from commit {rel['commit'][:7]} on {day(rel['published_at'])}." if rel else
            "The first build hasn't been published yet."))
    return layout("Download", Div(
        P("Download", cls="kicker"), H1("Get Broadsheet"),
        P("The same app on every platform. It runs entirely on your device.", cls="dek"),
        Div(Div(*platforms, cls="platforms"),
            Div(Section(H2("Or skip the install"),
                        P("Broadsheet also runs inside a web page, with Bluesky and Mastodon. Your accounts stay in that "
                          "browser."),
                        A("Open in your browser", href="/web", cls="button")),
                Section(H2("Questions"), *[Details(Summary(q), P(*(a if isinstance(a, tuple) else (a,)))) for q, a in faq],
                        cls="faq"), cls="side"), cls="download-grid"),
        cls="content wide"), PICK_OS, active="/download", session=session)


def _release_file(name: str):
    path = releases.path(name)
    if not path:
        return Response("Not published yet.", status_code=404)
    # no-cache: browsers and the desktop app always revalidate, so a new build shows up straight away
    return FileResponse(path, filename=name if name in APPS else None, headers={"Cache-Control": "no-cache"})


@rt("/downloads/{name}")
def get(name: str):
    return _release_file(name) if name in APPS else Response("Not found", status_code=404)


@rt("/desktop/{name}")
def get(name: str):
    """What installed desktop apps check for updates (crossposter_desktop/updater.py)."""
    return _release_file(name) if name in DESKTOP else Response("Not found", status_code=404)


@rt("/api/releases")
def get():
    rel = releases.current()
    return JSONResponse({k: rel[k] for k in ("commit", "build", "published_at", "sizes")} if rel else {})


@rt("/api/releases/{commit}/publish")
async def post(commit: str, req: Request):
    try:
        await run_in_threadpool(verify, req.headers.get("authorization", ""), commit)
        rel = releases.publish(commit)
    except Refused as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse({"ok": True, "commit": rel["commit"], "build": rel["build"]})


@rt("/api/releases/{commit}/{name}")
async def put(commit: str, name: str, req: Request):
    """One file of a release, uploaded by .github/workflows/apps.yml."""
    try:
        await run_in_threadpool(verify, req.headers.get("authorization", ""), commit)
        size = await releases.receive(commit, name, req.stream())
    except Refused as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse({"ok": True, "bytes": size})


# ---------- the browser version (web/) ----------
# /web is a page that runs the app itself in the browser. This server only hands out its files; the app's data never
# comes back here. See crossposter/browser.py and web/sw.js.

def _web_shell():
    return FileResponse(WEB / "shell.html", headers={"Cache-Control": "no-cache"})


def browser_client_metadata(base_url: str) -> dict:
    """Must match crossposter.adapters.bluesky_oauth.browser_client_metadata (a test checks they agree)."""
    return {
        "client_id": f"{base_url}/oauth/bluesky/client-metadata.json",
        "client_name": "Broadsheet",
        "client_uri": base_url,
        "application_type": "web",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "redirect_uris": [f"{base_url}/auth/bluesky/callback"],
        "scope": "atproto transition:generic",
        "token_endpoint_auth_method": "none",
        "dpop_bound_access_tokens": True,
    }


def _origin(req: Request) -> str:
    host = req.headers.get("host", "localhost")
    return ("http://" if host.startswith(("localhost", "127.0.0.1")) else "https://") + host


@rt("/web")
def get():
    return _web_shell()


@rt("/web/oauth/bluesky/client-metadata.json")
def get(req: Request):
    """The browser version signs in to Bluesky as this public client (no secret: tokens stay in the browser)."""
    return JSONResponse(browser_client_metadata(_origin(req) + "/web"), headers={"Cache-Control": "max-age=300"})


@rt("/web/{path:path}")
def get(path: str):
    """Any page of the app, such as a sign-in coming back: the shell opens it inside the browser version."""
    return _web_shell()


@rt("/web-files/{fname:path}")
def get(fname: str):
    for base in (WEB / "dist", WEB):
        path = (base / fname).resolve()
        if path.is_file() and base.resolve() in path.parents and path.suffix != ".py":
            break
    else:
        return Response("Not found", status_code=404)
    headers = {"Cache-Control": "no-cache"}
    if fname == "sw.js":
        headers["Service-Worker-Allowed"] = "/"  # it serves the app's frame, whose pages sit at the site's root
    elif fname.startswith("wheels/") or (fname.startswith("app-") and fname.endswith(".zip")):
        headers["Cache-Control"] = "public, max-age=31536000, immutable"  # versioned names
    media = {".webmanifest": "application/manifest+json", ".whl": "application/zip"}.get(path.suffix)
    return FileResponse(path, media_type=media, headers=headers)


# ---------- directory ----------

def explain(source: str) -> tuple[list[str], list[str]]:
    """A feed's own explanation (its docstring, as paragraphs) and the names of the parts its score() adds up."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []
    paras = [" ".join(p.split()) for p in (ast.get_docstring(tree) or "").split("\n\n") if p.strip()]
    parts = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "score":
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict):
                    parts += [k.value for k in ret.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return paras, list(dict.fromkeys(parts))


def source_label(src: str) -> str:
    net, _, name = src.partition(":")
    return f"{NETS.get(net, net)} {name.replace('-', ' ')}".strip()


@rt("/feeds")
def get(session, q: str = "", net: str = "", sort: str = "downloads"):
    feeds = store.feeds()
    if q:
        ql = q.lower()
        feeds = [f for f in feeds if ql in (f["name"] + " " + f["description"] + " " + f["credit"]).lower()]
    if net == "across":
        feeds = [f for f in feeds if len(networks(f["sources"])) > 1]
    elif net in NETS:
        feeds = [f for f in feeds if net in networks(f["sources"])]
    feeds.sort(key=sort_key(sort), reverse=True)

    def chip(value, label):
        params = {k: v for k, v in (("q", q), ("net", value), ("sort", sort if sort != "downloads" else "")) if v}
        return A(label, href="/feeds" + ("?" + urlencode(params) if params else ""), cls="chip on" if value == net else "chip",
                 aria_current="true" if value == net else None)

    return layout("Community feeds", Div(
        Div(P("Community feeds", cls="kicker"), H1("Ways to read your timeline"),
            P("Each feed is a short ranking rule that runs on your device, on posts already in your timelines. Every one "
              "is read by a maintainer before it's listed.", cls="dek"), cls="intro"),
        Form(Div(chip("", "All"), chip("bluesky", "Bluesky"), chip("mastodon", "Mastodon"),
                 chip("across", "Across networks"), cls="chips", aria_label="Works with"),
             Input(type="hidden", name="net", value=net) if net else None,
             Label(Span("Search feeds", cls="visually-hidden"),
                   Input(type="search", name="q", value=q, placeholder="Search feeds"), cls="grow"),
             Label(Span("Sort", cls="visually-hidden"),
                   Select(*[Option(lbl, value=v, selected=v == sort) for v, lbl in
                            (("downloads", "Popular"), ("kept", "Most kept"), ("rising", "Rising this week"),
                             ("newest", "Newest"))], name="sort", onchange="this.form.submit()")),
             Noscript(Button("Show", cls="button")), method="get", action="/feeds", cls="filters"),
        Div(*[feed_card(f) for f in feeds], cls="feed-cards") if feeds else P("No feeds match that.", cls="muted"),
        Div(P("Wrote a feed you like? Share it from the app, and a maintainer reads it before it's listed."),
            A("How sharing works", href="/feeds/share", cls="button"), cls="band"),
        cls="content"), active="/feeds", session=session, main_cls="page single")


def _spark_svg(points: str):
    return NotStr(f'<svg role="img" aria-label="Weekly users, last 12 weeks" width="56" height="20" viewBox="0 0 56 20" '
                  f'style="vertical-align:middle"><polyline points="{points}" fill="none" stroke="currentColor" '
                  f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)"/></svg>')


@rt("/feeds/share")
def get(session):
    return layout("Share a feed", Div(
        P(A("← All feeds", href="/feeds"), cls="small"),
        P("For authors", cls="kicker"), H1("Write and share a feed"),
        P("A feed is a Python file with a name, the timelines it reads, and a score() function that ranks posts.", cls="dek"),
        Ol(Li("In Broadsheet, open Feeds and choose New feed, or copy one you like."),
           Li("Save it and use it for a while. Preview shows the top posts and why each one ranked."),
           Li("When you're happy, choose Share with the community. The app checks for anything personal first, such as "
              "one of your lists."),
           Li("A maintainer reads the code, usually within a few days. Follow it under Feeds › Your submissions."),
           Li("Once listed, it appears here and in everyone's Community tab. Updates go through the same review."),
           cls="steps"),
        Section(H2("What shared feeds can use"),
                P("Only ", Code("crossposter.feeds"), " and standard tools like ", Code("math"), ", ", Code("collections"),
                  " and ", Code("re"), ". No network, files, ", Code("eval"), " or ", Code("exec"),
                  ". Feeds only see posts already fetched into your timelines."), cls="block"),
        Section(H2("What sharing sends"),
                P("The file, its name and description, and the credit line if you gave one. Never your handle or accounts. "
                  "The app keeps a private receipt so it can check on the review; this site keeps only a fingerprint of it."),
                cls="block"),
        cls="content"), Div(Section(H3("Start from one of these"),
                                    Ul(*[Li(A(f["name"], href=f"/feeds/{f['slug']}#code")) for f in store.feeds()[:5]],
                                       cls="checks")), cls="side"), active="/feeds", session=session)


@rt("/feeds/{slug}")
def get(slug: str, session):
    f = store.feed(slug)
    if not f:
        return Response("No feed by that name.", status_code=404)
    s = f["stats"]
    paras, parts = explain(f["source"])
    usage = None
    if s:
        weeks = store.weekly(slug, 12)
        top = max([w["users"] for w in weeks if w["shown"]] + [1])
        scale = max(10, int(top * 1.15))
        bars = []
        for w in weeks:
            label = w["week"].split("-W")[1]
            if w["shown"]:
                bars.append(Div(Span(style=f"height:{w['users'] / scale * 100:.1f}%"), cls="bar", tabindex="0",
                                title=f"Week {label}: {n(w['users'])} users",
                                aria_label=f"Week {label}: {n(w['users'])} users"))
            else:
                bars.append(Div(Span(), cls="bar gap", tabindex="0", title=f"Week {label}: fewer than {MIN_REPORTS} apps reported",
                                aria_label=f"Week {label}: fewer than {MIN_REPORTS} apps reported"))
        ch = s["users_change"]
        pos_labels = ("First tab", "Second tab", "Third tab", "Further along")
        top_pos = max(s.get("positions") or [0]) or 1
        tiles = [
            Div(Div("Weekly users", cls="label"), Div(n(s["users"]), cls="value"),
                Div(f"{'▲' if ch >= 0 else '▼'} {abs(ch) * 100:.0f}% on the week before", cls="sub up" if ch >= 0 else "sub")
                if ch is not None else Div(f"week {s['week'].split('-W')[1]}", cls="sub"), cls="tile"),
            Div(Div("Using it 30+ days", cls="label"), Div(pct(s["long_term_share"]), cls="value"),
                Div("of weekly users", cls="sub"), cls="tile"),
            Div(Div("Posts read per visit", cls="label"), Div(f"{s['read_per_visit']:.0f}", cls="value"),
                Div("on average", cls="sub"), cls="tile"),
            Div(Div("Liked or replied to", cls="label"), Div(pct(s["acted_share"]), cls="value"),
                Div("of posts read", cls="sub"), cls="tile"),
            Div(Div("Downloads", cls="label"), Div(n(f["downloads"]), cls="value"),
                Div(f"{n(f['downloads_week'])} this week", cls="sub"), cls="tile")]
        usage = Section(
            H2("How people use it"), Div(*tiles, cls="tiles"),
            P("Apps that shared stats and opened this feed at least once that week. Last 12 weeks; a dashed mark means "
              f"fewer than {MIN_REPORTS} apps reported.", cls="muted small"),
            Div(Div(Span(n(scale)), cls="grid", style="top:0"), Div(Span(n(scale // 2)), cls="grid", style="top:50%"),
                *bars, cls="chart", role="group", aria_label="Weekly users, last 12 weeks"),
            Div(*[Span(w["week"].split("-W")[1]) for w in weeks], cls="chart-x", aria_hidden="true"),
            H3("Where people put it", style="margin-top:24px"),
            Div(*[Div(Span(lbl), Div(Span(style=f"width:{v / top_pos * 100:.0f}%"), cls="track"), Span(pct(v), cls="v"),
                      cls="hbar") for lbl, v in zip(pos_labels, s["positions"])], cls="hbars"), cls="block")
    if s:
        numbers = P(f"Figures from {n(s['reports'])} apps that chose to share anonymous counts for week "
                    f"{s['week'].split('-W')[1]}. People who said no aren't counted, so real use is higher. ",
                    A("How the numbers work", href="/privacy#stats"))
    else:
        numbers = P(f"{n(f['downloads'])} download{'s' if f['downloads'] != 1 else ''} so far. Weekly figures appear once "
                    f"{MIN_REPORTS} apps share anonymous stats. ", A("How the numbers work", href="/privacy#stats"))
    versions = store.versions(slug)
    return layout(f["name"], Div(
        P(A("← All feeds", href="/feeds"), cls="small"),
        P("Community feed", cls="kicker"), H1(f["name"]),
        P(Span(f"by {f['credit']}" if f["credit"] else "Shared anonymously"),
          f" · version {f['version']} · updated {day(f['updated_at'])} · reads ",
          ", ".join(source_label(x) for x in f["sources"]), cls="muted small"),
        Div(*[P(p) for p in (paras or [f["description"]])], cls="explain"),
        Section(H2("What it rewards"), Ul(*[Li(p[:1].upper() + p[1:]) for p in parts], cls="rewards"),
                P("A post's score is these added together.", cls="muted small"), cls="block") if parts else None,
        Details(Summary(Strong("Read the code"), Span(f" · {len(f['source'].splitlines())} lines of Python", cls="muted")),
                Pre(Code(f["source"])), id="code", cls="code-fold"),
        usage, cls="content"),
        Div(Section(A("Open in your browser", href=try_link(slug), cls="button primary large"),
                    P("Opens Broadsheet in this browser on this feed, where you can add it. Nothing to install.",
                      cls="muted small"),
                    P("Have the app? Go to ", Strong(f"Feeds › Community › {f['name']}"), ".", cls="small have-app"),
                    cls="add-box"),
            Section(H3("Checks"), Ul(Li(Span("✓", cls="yes"), "No network or file access"),
                                     Li(Span("✓", cls="yes"), "Only Broadsheet's feed helpers and the standard maths tools"),
                                     Li(Span("✓", cls="yes"), "Read by a maintainer before listing"), cls="checks")),
            Section(H3("Use"), numbers, cls="quiet"),
            Section(H3("Versions"), Ol(*[Li(Strong(str(v["version"])), f" · {day(v['created_at'])} · ",
                                           Span(v["note"] or "Update", cls="muted")) for v in versions],
                                       cls="checks", style="list-style:none;padding:0")),
            cls="side"), active="/feeds", session=session)


@rt("/privacy")
def get(session):
    example = {"week": "2026-W44", "feeds": [
        {"feed": "slow-news", "on_home": True, "installed_for": "8-30d", "visits": 9, "posts_read": 171,
         "posts_liked_or_replied": 6, "tab_position": 2}]}
    return layout("Privacy", Div(
        P("Privacy", cls="kicker"), H1("Where your data goes"),
        P("Broadsheet is local-first. Your accounts, posts and the feeds you write stay on your device, and this site "
          "only knows what's on this page.", cls="dek"),
        Section(H2("What stays with you"),
                Ul(Li(Strong("The apps"), " keep your accounts, feeds and database in your own user data folder on "
                      "Windows and Mac, and in the app's private storage on Android."),
                   Li(Strong("The browser version"), " keeps them in that browser's storage for this site. This site only "
                      "hands out its files. Clearing the site's data signs it out, so keep a backup from its Settings."),
                   Li("Sign-ins go straight from your device to Bluesky, Mastodon or X. Your posts are fetched from them "
                      "directly, never through us."), cls="plain-list"), cls="block"),
        Section(H2("Downloads"), P("When an app adds a feed, it fetches the file from this site and the count for that feed "
                                   "and day goes up by one. Nothing else about the request is stored. App downloads aren't "
                                   "tied to anyone either, and the site keeps no request logs."), cls="block"),
        Section(H2("Anonymous weekly stats"),
                P("The first time someone adds a community feed, the app asks whether to share anonymous counts, with neither "
                  "answer picked. They can change their mind in Settings. If they say yes, once a week the app sends one "
                  "report like this, only for feeds that came from this directory:"),
                Pre(Code(json.dumps(example, indent=2))),
                P("The site adds each report to that feed's weekly totals and throws the report away. There's no device ID, "
                  "so reports can't be linked to each other, and IP addresses aren't kept."),
                P(f"A weekly figure is only shown once {MIN_REPORTS} or more apps have reported it."), cls="block", id="stats"),
        Section(H2("Shared feeds"), P("When you share a feed, the app sends the file, its name and description, and the "
                                      "credit line if you gave one. It never sends your handle or accounts. The app makes a "
                                      "private receipt so it can check on the review; the site keeps only a fingerprint "
                                      "of it."), cls="block"),
        cls="content"), Div(numbers_box(), cls="side"), active="/privacy", session=session)


@rt("/data")
def get():
    return RedirectResponse("/privacy", status_code=301)


@rt("/write")
def get():
    return RedirectResponse("/feeds/share", status_code=301)


# ---------- review queue (maintainers) ----------

@rt("/login")
def get(session, error: str = ""):
    return layout("Sign in", Div(H1("Maintainer sign-in"),
                                 P(error, cls="notice error") if error else None,
                                 Form(Label("Password", Input(type="password", name="password", required=True,
                                                              autocomplete="current-password"), cls="field"),
                                      Button("Sign in", cls="button primary"), method="post", action="/login",
                                      cls="row", style="align-items:flex-end")
                                 if ADMIN_PASSWORD else P("The review queue is closed: ADMIN_PASSWORD isn't set.", cls="muted"),
                                 cls="content"), session=session)


@rt("/login")
def post(session, password: str = ""):
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        session["admin"] = True
        return RedirectResponse("/review", status_code=303)
    return RedirectResponse("/login?error=That+password+isn%27t+right.", status_code=303)


@rt("/logout")
def post(session):
    session.pop("admin", None)
    return RedirectResponse("/", status_code=303)


def _check_rows(checks: dict):
    items = checks.get("items") or []
    rows = [Tr(Td(Span("✓", cls="yes")), Td("Parses, defines feed and score, and uses only allowed modules and helpers"))]
    rows += [Tr(Td(Span("!" if i["level"] == "warning" else "✕", cls="warn" if i["level"] == "warning" else "no")),
                Td(i["text"], Span(f" (line {i['line']})", cls="muted") if i.get("line") else None)) for i in items]
    if not items:
        rows.append(Tr(Td(Span("✓", cls="yes")), Td("No handles, account IDs or private lists in the file")))
    return Table(Tbody(*rows), cls="check-table")


@rt("/review")
def get(session, id: int = 0, ok: str = "", error: str = ""):
    if not is_admin(session):
        return RedirectResponse("/login", status_code=303)
    queue = store.queue()
    current = next((s for s in queue if s["id"] == id), queue[0] if queue else None)
    side = Div(H1("Waiting for review", style="font-size:1.8rem"),
               Ul(*[Li(A(Div(s["name"], cls="name"),
                         Div(("Update to " + s["slug"] if s["slug"] else "New feed") + " · " + day(s["created_at"]),
                             cls="muted small"),
                         href=f"/review?id={s['id']}", cls="active" if current and s["id"] == current["id"] else None))
                    for s in queue], cls="queue") if queue else P("Nothing waiting. ", cls="muted"),
               P("Submissions carry no account. You see the file, its name and description, and the credit line if the "
                 "author gave one.", cls="muted small"),
               Form(Button("Sign out", cls="button"), method="post", action="/logout"), cls="side", style="order:-1")
    if not current:
        main = Div(P(ok, cls="notice ok") if ok else None, P("The queue is empty.", cls="dek"), cls="content")
        return layout("Review", side, main, active="/review", session=session)
    listed = store.feed(current["slug"]) if current["slug"] else None
    main = Div(
        P(ok, cls="notice ok") if ok else None, P(error, cls="notice error") if error else None,
        P(f"Update to version {listed['version'] + 1}" if listed else "New feed", cls="kicker"),
        H2(current["name"], style="font-size:2.2rem"), P(current["description"], cls="italic"),
        P(f"Credit: {current['credit'] or 'none (anonymous)'} · {current['checks'].get('lines', 0)} lines"
          + (f" · Author's note: {current['note']}" if current["note"] else ""), cls="muted small"),
        Section(H3("Automatic checks"), _check_rows(current["checks"]), cls="block"),
        Section(H3("The code"), Pre(Code(current["source"])), cls="block"),
        Section(H3("What it replaces"), Pre(Code(listed["source"])), cls="block") if listed else None,
        Section(Form(Label("Note to the author (shown in their app)",
                           Textarea(name="note", rows=3, placeholder="Optional for approval, needed when asking for changes"),
                           cls="field"),
                     Div(Button("Approve and list", name="decision", value="approve", cls="button primary"),
                         Button("Ask for changes", name="decision", value="changes", cls="button"),
                         Button("Decline", name="decision", value="declined", cls="button danger"),
                         cls="row", style="margin-top:12px"),
                     method="post", action=f"/review/{current['id']}"), cls="block"),
        cls="content")
    return layout("Review", side, main, active="/review", session=session)


@rt("/review/{sid}")
def post(sid: int, session, decision: str = "", note: str = ""):
    if not is_admin(session):
        return RedirectResponse("/login", status_code=303)
    try:
        s = store.review(sid, decision, note)
    except Rejected as e:
        return RedirectResponse("/review?" + urlencode({"id": sid, "error": str(e)}), status_code=303)
    msg = {"approved": f"Listed {s['name']}.", "changes": f"Asked for changes to {s['name']}.",
           "declined": f"Declined {s['name']}."}[s["status"]]
    return RedirectResponse("/review?" + urlencode({"ok": msg}), status_code=303)


# ---------- API used by the app ----------

def _public(f: dict, with_source: bool = False) -> dict:
    keys = ["slug", "name", "description", "sources", "credit", "version", "created_at", "updated_at", "downloads",
            "downloads_week", "stats"]
    d = {k: f[k] for k in keys}
    if with_source:
        d["source"] = f["source"]
        d["versions"] = store.versions(f["slug"])
    return d


@rt("/api/feeds")
def get():
    return JSONResponse({"feeds": [_public(f) for f in store.feeds()], "min_reports": MIN_REPORTS})


@rt("/api/feeds/{slug}")
def get(slug: str):
    f = store.feed(slug)
    return JSONResponse(_public(f, True)) if f else JSONResponse({"error": "not found"}, status_code=404)


@rt("/api/feeds/{slug}/download")
def post(slug: str):
    """The app calls this when someone adds or updates a feed. It counts one download and returns the file."""
    f = store.feed(slug)
    if not f:
        return JSONResponse({"error": "not found"}, status_code=404)
    store.count_download(slug)
    return JSONResponse({"slug": slug, "version": f["version"], "name": f["name"], "source": f["source"]})


async def _json(req: Request) -> dict:
    if int(req.headers.get("content-length") or 0) > 64_000:
        raise Rejected("That's too large.")
    try:
        body = await req.json()
    except Exception:
        raise Rejected("Send JSON.")
    if not isinstance(body, dict):
        raise Rejected("Send a JSON object.")
    return body


@rt("/api/submissions")
async def post(req: Request):
    try:
        b = await _json(req)
        s = store.submit(b.get("receipt"), b.get("name", ""), b.get("description", ""), b.get("credit", ""),
                         b.get("source", ""), b.get("slug") or "", b.get("note", ""))
    except Rejected as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": s["id"], "status": s["status"], "checks": s["checks"]}, status_code=201)


@rt("/api/submissions/status")
async def post(req: Request):
    try:
        b = await _json(req)
    except Rejected as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    receipts = b.get("receipts")
    return JSONResponse({"submissions": store.mine(receipts if isinstance(receipts, list) else [])})


@rt("/api/submissions/{sid}/withdraw")
async def post(sid: int, req: Request):
    try:
        b = await _json(req)
    except Rejected as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    ok = store.withdraw(sid, str(b.get("receipt") or ""))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@rt("/api/report")
async def post(req: Request):
    """One app's anonymous weekly report. Folded into totals and discarded; see /data."""
    try:
        counted = store.record_report(await _json(req))
    except Rejected as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "counted": counted})


@rt("/healthz")
def get():
    return {"ok": True}


@rt("/static/{fname:path}")
def get(fname: str):
    for base in (ROOT / "static", REPO / "static"):
        path = (base / fname).resolve()
        if path.is_file() and base.resolve() in path.parents:
            return FileResponse(path)
    return Response("Not found", status_code=404)


def main():
    import uvicorn
    # access_log off: request lines would include IP addresses, which this site promises not to keep
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5002")), access_log=False,
                proxy_headers=False, log_level="info")


if __name__ == "__main__":
    main()
