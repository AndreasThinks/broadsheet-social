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

import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fasthtml.common import (A, Button, Code, Div, Em, Footer, Form, H1, H2, H3, Header, Input, Label, Li, Main, Nav, Ol,
                             Option, P, Pre, Script, Section, Select, Span, Strong, Table, Tbody, Td, Textarea, Title, Tr, Ul,
                             Link, Meta, fast_app)
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
SOURCE_URL = "https://github.com/AndreasThinks/cross-poster-"
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


def layout(title: str, *content, active: str = "/", session=None):
    links = [("/", "Get the app"), ("/feeds", "Community feeds"), ("/data", "How the numbers work"), ("/write", "Write a feed")]
    if session is not None and is_admin(session):
        links.append(("/review", f"Review {len(store.queue()) or ''}".strip()))
    return (Title(f"{title} · Broadsheet"),
            Header(Div(A("Broadsheet", href="/", cls="brand"),
                       Nav(*[A(label, href=h, cls="active" if h == active else None,
                               aria_current="page" if h == active else None) for h, label in links],
                           cls="nav", aria_label="Sections"),
                       A("Download", href="/#download", cls="button primary"), cls="wrap"), cls="masthead"),
            Main(*content, cls="wrap page"),
            Footer(Div(Span("Broadsheet runs on your device. Your accounts, posts and feeds never pass through this site."),
                       A("Source on GitHub", href=SOURCE_URL), A("Data we keep", href="/data"), cls="wrap"), cls="footer"))


def numbers_box():
    return Section(
        H2("How the numbers work"),
        P("Broadsheet keeps your accounts and posts on your device. Downloads are counted for everyone: the site adds one "
          "each time an app fetches a feed, and keeps nothing about who fetched it."),
        P("Everything else comes only from people who said yes to ", Em("Share anonymous feed stats"),
          ". Each app is asked once, with neither answer picked, and can change its mind in Settings."),
        Ul(Li(Span("✓", cls="yes"), Span("Once a week, a few counts per community feed: is it on Home, how often it was "
                                         "opened, how many posts were read.")),
           Li(Span("✕", cls="no"), Span("Never handles, accounts, posts, follows, or feeds people wrote themselves.")),
           Li(Span("✕", cls="no"), Span("No device ID and no IP addresses kept. Reports are added to weekly totals and "
                                        "thrown away.")),
           Li(Span("✓", cls="yes"), Span(f"A weekly figure only shows once {MIN_REPORTS} or more apps report it.")),
           cls="checks"),
        P(A("See exactly what a report contains", href="/data"), cls="small"))


def sort_key(sort: str):
    def users(f):
        return (f["stats"] or {}).get("users", 0)
    return {
        "kept": lambda f: ((f["stats"] or {}).get("long_term_share", -1), f["downloads"]),
        "rising": lambda f: ((f["stats"] or {}).get("users_change") or -9, users(f)),
        "downloads": lambda f: (f["downloads"],),
        "newest": lambda f: (f["created_at"],),
    }.get(sort, lambda f: (f["downloads"],))


def spark(stats_weeks: list[dict]) -> str:
    vals = [w.get("users", 0) if w["shown"] else None for w in stats_weeks]
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    return " ".join(f"{2 + i * 52 / (len(vals) - 1):.1f},{18 - (v - lo) / span * 16:.1f}" for i, v in pts)


# ---------- the app and its downloads ----------

PLATFORMS = (
    ("android", "Android", "Broadsheet-android.apk", "Download APK",
     ("Android 8 or later. Allow installs from your browser when asked.",)),
    ("windows", "Windows", "Broadsheet-windows.zip", "Download for Windows",
     ("Unzip, then open ", Code("Broadsheet.exe"), ". Choose “More info › Run anyway” the first time.")),
    ("mac", "macOS", "Broadsheet-macos.zip", "Download for Mac",
     ("Unzip and move it to Applications. Right-click › Open the first time.",)),
)
# Put the visitor's own platform first.
PICK_OS = Script("""const ua = navigator.userAgent;
const os = /Android/.test(ua) ? "android" : /Windows/.test(ua) ? "windows" : /Mac/.test(ua) ? "mac" : "";
const mine = os && document.querySelector(`[data-os="${os}"]`);
if (mine) { mine.classList.add("mine"); mine.parentNode.prepend(mine); }""")


@rt("/")
def get(session):
    rel = releases.current()
    cards = [Div(H2(label), P(*note), A(button, href=f"/downloads/{fname}", cls="button primary") if rel
                 else Span("Coming soon", cls="muted"),
                 P(f"{rel['sizes'][fname] / 1048576:.0f} MB", cls="muted small") if rel else None,
                 cls="download", data_os=key) for key, label, fname, button, note in PLATFORMS]
    top = sorted(store.feeds(), key=lambda f: f["downloads"], reverse=True)[:3]
    return layout("Get the app", Div(
        P("Bluesky · Mastodon · X", cls="kicker"), H1("Broadsheet"),
        P("Read and post across your networks from one app. It runs entirely on your device: your accounts and posts "
          "never pass through anyone else's server.", cls="dek"),
        Div(*cards, cls="downloads", id="download"),
        P(f"Built from commit {rel['commit'][:7]} on {day(rel['published_at'])}." if rel else
          "The first build hasn't been published yet.", cls="muted small"),
        Section(H2("Or use it in your browser"),
                P("Nothing to install: Broadsheet runs inside the web page, and your accounts, posts and feeds stay in "
                  "that browser. This site never sees them. The first visit downloads about 20 MB."),
                P("The browser version does Bluesky and Mastodon. X doesn't let web pages connect to it, so for X use one "
                  "of the apps above. Mastodon can notify you while the page is closed; Bluesky only while it's open. "
                  "Clearing your browser's data for this site signs you out, so keep a backup from Settings.",
                  cls="muted"),
                P(A("Open Broadsheet in your browser", href="/web", cls="button primary")), cls="block", id="browser"),
        Section(H2("Staying up to date"),
                P("The desktop apps update themselves: they fetch new versions in the background and switch over the next "
                  "time you open them. On Android, install the new APK over the old one, or add this page to ",
                  A("Obtainium", href="https://obtainium.imranr.dev/"), " to get updates automatically."), cls="block"),
        Section(H2("Notifications"),
                P("On Android, the app checks for replies, mentions, quotes and follows about every 15 minutes, even when "
                  "it's closed. Android may check less often to save battery."), cls="block"),
        Section(H2("Community feeds"),
                P("Ranking rules people have written for Broadsheet, each a short Python file that runs on your device. "
                  "Add them in the app under Feeds › Community."),
                Ul(*[Li(A(f["name"], href=f"/feeds/{f['slug']}"), Span(f" · {f['description']}", cls="muted"))
                     for f in top], cls="checks") if top else None,
                P(A("Browse all community feeds", href="/feeds", cls="button")), cls="block"),
        cls="content"),
        Div(Section(H3("Your data stays with you"),
                    P("The apps keep your accounts, feeds and database in your own user data folder. This site only "
                      "hands out the apps and the community feeds, and keeps nothing about who downloads them.")),
            cls="side"), PICK_OS, active="/", session=session)


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
    rows = []
    for i, f in enumerate(feeds, 1):
        s = f["stats"]
        pts = spark(store.weekly(f["slug"], 12))
        rows.append(Li(
            Span(str(i), cls="rank"),
            Div(Div(A(f["name"], href=f"/feeds/{f['slug']}", cls="name"), " ",
                    Span(f"by {f['credit']}" if f["credit"] else "anonymous", cls="muted small"), " ",
                    Span("✓ Reviewed", cls="reviewed")),
                P(f["description"], cls="desc"), Div(*pills(f["sources"])), cls="about"),
            Div(Div(Div("Downloads", cls="label"), Div(n(f["downloads"]), cls="value"), cls="num"),
                Div(Div("Weekly users", cls="label"),
                    Div(n(s["users"]) if s else "Too new", " ",
                        _spark_svg(pts) if pts else None, cls="value"), cls="num"),
                Div(Div("Using it 30+ days", cls="label"), Div(pct(s["long_term_share"]) if s else "–", cls="value"), cls="num"),
                Div(Div("Read per visit", cls="label"), Div(f"{s['read_per_visit']:.0f} posts" if s else "–", cls="value"),
                    cls="num"),
                A("Details", href=f"/feeds/{f['slug']}", cls="button"), cls="nums"),
            cls="feed-row"))
    total = store.total_downloads()
    return layout("Directory", Div(
        P("The directory", cls="kicker"), H1("Community feeds"),
        P("Ranking rules people have written for Broadsheet. Each one is a short Python file that runs on your own device, "
          "on posts already in your timelines. Open Broadsheet, go to Feeds › Community, read the code, and add it in a tap.",
          cls="dek"),
        Div(Div(Div(n(len(store.feeds())), cls="value"), Div("feeds listed", cls="sub"), cls="tile"),
            Div(Div(n(total), cls="value"), Div("feed downloads, all time", cls="sub"), cls="tile"),
            Div(Div(n(store.reporting_apps()), cls="value"), Div("apps sharing anonymous stats this week", cls="sub"),
                cls="tile"), cls="tiles"),
        Form(Label("Search", Input(type="search", name="q", value=q, placeholder="News, art, quiet people, links…"),
                   cls="field grow"),
             Label("Works with", Select(*[Option(lbl, value=v, selected=v == net) for v, lbl in
                                          (("", "Any network"), ("bluesky", "Bluesky"), ("mastodon", "Mastodon"),
                                           ("across", "More than one network"))], name="net"), cls="field"),
             Label("Sort by", Select(*[Option(lbl, value=v, selected=v == sort) for v, lbl in
                                       (("downloads", "Most downloaded"), ("kept", "Most kept"), ("rising", "Rising this week"),
                                        ("newest", "Newest"))], name="sort"), cls="field"),
             Button("Show", cls="button"), method="get", action="/feeds", cls="filters"),
        Ol(*rows, cls="feed-list") if rows else P("No feeds match that.", cls="muted"),
        P(f"Feeds with fewer than {MIN_REPORTS} apps reporting show “Too new” instead of weekly figures.", cls="muted small"),
        cls="content"),
        Div(numbers_box(),
            Section(H2("Share a feed you wrote"),
                    P("Open the feed in Broadsheet and choose ", Em("Share with the community"),
                      ". A maintainer reads the code before it's listed."),
                    A("How sharing works", href="/write", cls="button primary")),
            cls="side"), active="/feeds", session=session)


def _spark_svg(points: str):
    from fasthtml.common import NotStr
    return NotStr(f'<svg role="img" aria-label="Weekly users, last 12 weeks" width="56" height="20" viewBox="0 0 56 20" '
                  f'style="vertical-align:middle"><polyline points="{points}" fill="none" stroke="currentColor" '
                  f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)"/></svg>')


@rt("/feeds/{slug}")
def get(slug: str, session):
    f = store.feed(slug)
    if not f:
        return Response("No feed by that name.", status_code=404)
    s = f["stats"]
    weeks = store.weekly(slug, 12)
    top = max([w["users"] for w in weeks if w["shown"]] + [1])
    scale = max(10, int(top * 1.15))
    bars = []
    for w in weeks:
        label = w["week"].split("-W")[1]
        if w["shown"]:
            bars.append(Div(Span(style=f"height:{w['users'] / scale * 100:.1f}%"), cls="bar", tabindex="0",
                            title=f"Week {label}: {n(w['users'])} users", aria_label=f"Week {label}: {n(w['users'])} users"))
        else:
            bars.append(Div(Span(), cls="bar gap", tabindex="0", title=f"Week {label}: fewer than {MIN_REPORTS} apps reported",
                            aria_label=f"Week {label}: fewer than {MIN_REPORTS} apps reported"))
    pos_labels = ("First tab", "Second tab", "Third tab", "Further along")
    top_pos = max((s or {}).get("positions") or [0]) or 1
    tiles = [Div(Div("Downloads", cls="label"), Div(n(f["downloads"]), cls="value"),
                 Div(f"{n(f['downloads_week'])} this week", cls="sub"), cls="tile")]
    if s:
        ch = s["users_change"]
        tiles += [
            Div(Div("Weekly users", cls="label"), Div(n(s["users"]), cls="value"),
                Div(f"{'▲' if ch >= 0 else '▼'} {abs(ch) * 100:.0f}% on the week before", cls="sub up" if ch >= 0 else "sub")
                if ch is not None else Div(f"week {s['week'].split('-W')[1]}", cls="sub"), cls="tile"),
            Div(Div("Using it 30+ days", cls="label"), Div(pct(s["long_term_share"]), cls="value"),
                Div("of weekly users", cls="sub"), cls="tile"),
            Div(Div("Posts read per visit", cls="label"), Div(f"{s['read_per_visit']:.0f}", cls="value"),
                Div("on average", cls="sub"), cls="tile"),
            Div(Div("Liked or replied to", cls="label"), Div(pct(s["acted_share"]), cls="value"),
                Div("of posts read", cls="sub"), cls="tile")]
    else:
        tiles.append(Div(Div("Weekly figures", cls="label"), Div("Too new", cls="value"),
                         Div(f"shown once {MIN_REPORTS}+ apps report", cls="sub"), cls="tile"))
    versions = store.versions(slug)
    return layout(f["name"], Div(
        P(A("← All feeds", href="/feeds"), cls="small"),
        Div(P("Community feed", cls="kicker"), H1(f["name"]), P(f["description"], cls="italic"),
            P(Span(f"by {f['credit']}" if f["credit"] else "Shared anonymously"), " · ",
              Span(f"Version {f['version']} · updated {day(f['updated_at'])}"), " · ", Span("✓ Reviewed by a maintainer", cls="reviewed"),
              cls="muted small"),
            Div(A("Read the code", href="#code", cls="button"), Span("To add it, open Broadsheet: Feeds › Community.",
                                                                       cls="muted small"), cls="row")),
        Div(*tiles, cls="tiles"),
        Section(H2("Weekly users"),
                P("Apps that shared stats and opened this feed at least once that week. Last 12 weeks; a dashed mark means "
                  f"fewer than {MIN_REPORTS} apps reported.", cls="muted small"),
                Div(Div(Span(n(scale)), cls="grid", style="top:0"), Div(Span(n(scale // 2)), cls="grid", style="top:50%"),
                    *bars, cls="chart", role="group", aria_label="Weekly users, last 12 weeks"),
                Div(*[Span(w["week"].split("-W")[1]) for w in weeks], cls="chart-x", aria_hidden="true"), cls="block"),
        Section(H2("Where people put it"), P("Its position among each app's Home tabs, this week.", cls="muted small"),
                Div(*[Div(Span(lbl), Div(Span(style=f"width:{v / top_pos * 100:.0f}%"), cls="track"), Span(pct(v), cls="v"),
                          cls="hbar") for lbl, v in zip(pos_labels, s["positions"])], cls="hbars")
                if s else P("Not enough reports yet.", cls="muted"), cls="block"),
        Section(Div(H2("The code"), Span(f"{len(f['source'].splitlines())} lines · reads ", *[Code(x) for x in f["sources"]],
                                         cls="muted small"), cls="row", style="justify-content:space-between"),
                Pre(Code(f["source"])), id="code", cls="block"),
        cls="content"),
        Div(Section(H3("Where these numbers come from"),
                    P("Downloads count every time an app fetches this feed, with nothing kept about who. "
                      + (f"The other figures come from {n(s['reports'])} apps that chose to share anonymous counts for "
                         f"week {s['week'].split('-W')[1]}. None of them sent a handle, a post or an account." if s else
                         "Weekly figures appear once enough apps share anonymous counts.")),
                    P("People who said no to stats aren't in those figures, so real use is higher.", cls="muted small")),
            Section(H3("Checks"), Ul(Li(Span("✓", cls="yes"), "No network or file access"),
                                     Li(Span("✓", cls="yes"), "Only Broadsheet's feed helpers and the standard maths tools"),
                                     Li(Span("✓", cls="yes"), "Read by a maintainer before listing"), cls="checks")),
            Section(H3("Versions"), Ol(*[Li(Strong(str(v["version"])), f" · {day(v['created_at'])} · ",
                                           Span(v["note"] or "Update", cls="muted")) for v in versions],
                                       cls="checks", style="list-style:none;padding:0")),
            cls="side"), active="/feeds", session=session)


@rt("/data")
def get(session):
    example = {"week": "2026-W44", "feeds": [
        {"feed": "slow-news", "on_home": True, "installed_for": "8-30d", "visits": 9, "posts_read": 171,
         "posts_liked_or_replied": 6, "tab_position": 2}]}
    return layout("How the numbers work", Div(
        P("Privacy", cls="kicker"), H1("How the numbers work"),
        P("Broadsheet is local-first: your accounts, posts and the feeds you write stay on your device. This site only "
          "knows what's below.", cls="dek"),
        Section(H2("Downloads"), P("When an app adds a feed, it fetches the file from this site and the count for that feed "
                                   "and day goes up by one. Nothing else about the request is stored.")),
        Section(H2("Anonymous weekly stats"),
                P("The first time someone adds a community feed, the app asks whether to share anonymous counts, with neither "
                  "answer picked. If they say yes, once a week the app sends one report like this, only for feeds that came "
                  "from this directory:"),
                Pre(Code(json.dumps(example, indent=2))),
                P("The site adds each report to that feed's weekly totals and throws the report away. There's no device ID, "
                  "so reports can't be linked to each other, and IP addresses aren't kept."),
                P(f"A weekly figure is only shown once {MIN_REPORTS} or more apps have reported it."), cls="block"),
        Section(H2("Shared feeds"), P("When you share a feed, the app sends the file, its name and description, and the "
                                      "credit line if you gave one. It never sends your handle or accounts. The app makes a "
                                      "private receipt so it can check on the review; the site keeps only a fingerprint "
                                      "of it."), cls="block"),
        cls="content"), Div(numbers_box(), cls="side"), active="/data", session=session)


@rt("/write")
def get(session):
    return layout("Write a feed", Div(
        P("For authors", cls="kicker"), H1("Write and share a feed"),
        P("A feed is a Python file with a name, the timelines it reads, and a score() function that ranks posts.", cls="dek"),
        Ol(Li("In Broadsheet, open Feeds and choose New feed, or copy one you like."),
           Li("Save it and use it for a while. Preview shows the top posts and why each one ranked."),
           Li("When you're happy, choose Share with the community. The app checks for anything personal first, such as "
              "one of your lists."),
           Li("A maintainer reads the code, usually within a few days. Follow it under Feeds › Your submissions."),
           Li("Once listed, it appears here and in everyone's Community tab. Updates go through the same review.")),
        Section(H2("What shared feeds can use"),
                P("Only ", Code("crossposter.feeds"), " and standard tools like ", Code("math"), ", ", Code("collections"),
                  " and ", Code("re"), ". No network, files, ", Code("eval"), " or ", Code("exec"),
                  ". Feeds only see posts already fetched into your timelines."), cls="block"),
        cls="content"), Div(numbers_box(), cls="side"), active="/write", session=session)


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
