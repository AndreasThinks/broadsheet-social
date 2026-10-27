"""People across networks: one person, a handle on each network, and mentions that follow them.

When you cross-post "thanks @alice.bsky.social", Mastodon and X don't know who that is. If you've
linked Alice's accounts, each network gets her handle there. If you haven't, the mention is posted
as plain text so it can't tag a stranger (X would otherwise tag @alice), or, where Alice is bridged
by Bridgy Fed, as her bridged handle.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .models import Account

NETWORKS = ("bluesky", "mastodon", "x")

# Any network's mention: @user@server.tld (Mastodon), @name.domain (Bluesky) or @name (X, or a local
# Mastodon user). Not preceded by a word character or slash, so emails and URLs are left alone.
MENTION_ANY_RE = re.compile(
    r"(?<![\w@/])@([A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)(?:@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+))?")

# Bridgy Fed (https://fed.brid.gy) bridges opted-in accounts between Bluesky and the fediverse.
BSKY_BRIDGE = "bsky.brid.gy"      # a Bluesky user seen from Mastodon: @alice.bsky.social@bsky.brid.gy
AP_BRIDGE = ".ap.brid.gy"         # a fediverse user seen from Bluesky: @alice.mastodon.social.ap.brid.gy



def normalise(network: str, handle: str) -> str:
    """The form handles are stored and compared in: no leading @, lower case (all three networks
    treat handles case-insensitively)."""
    h = (handle or "").strip().lstrip("@").lower()
    if network == "bluesky":
        h = h.removeprefix("https://bsky.app/profile/")
    return h


def shape(handle: str) -> str:
    """Which network a handle looks like it belongs to: 'mastodon', 'bluesky' or 'bare' (X or local)."""
    h = handle.lstrip("@")
    if "@" in h:
        return "mastodon"
    return "bluesky" if "." in h else "bare"


def unbridge(handle: str) -> tuple[str, str] | None:
    """A Bridgy Fed handle's native (network, handle), or None if it isn't one."""
    h = normalise("", handle)
    if h.endswith("@" + BSKY_BRIDGE):
        return "bluesky", h[: -len(BSKY_BRIDGE) - 1]
    if h.endswith(AP_BRIDGE):
        # alice.mastodon.social.ap.brid.gy -> alice@mastodon.social (usernames can't contain dots)
        user, _, server = h[: -len(AP_BRIDGE)].partition(".")
        if user and "." in server:
            return "mastodon", f"{user}@{server}"
    return None


def bridged(handle: str, target: str) -> str | None:
    """The Bridgy Fed handle that reaches a native handle from the target network, if there is one."""
    h = normalise("", handle)
    s = shape(h)
    if target == "mastodon" and s == "bluesky":
        return f"{h}@{BSKY_BRIDGE}"
    if target == "bluesky" and s == "mastodon":
        user, server = h.split("@", 1)
        return f"{user}.{server}{AP_BRIDGE}"
    return None


# ---------- people ----------

@dataclass
class Person:
    id: int
    name: str
    handles: dict[str, list[str]] = field(default_factory=dict)  # network -> handles, first is preferred

    def handle_on(self, network: str) -> str | None:
        hs = self.handles.get(network) or []
        return hs[0] if hs else None

    def all_handles(self) -> list[tuple[str, str]]:
        return [(n, h) for n in NETWORKS for h in self.handles.get(n) or []]


def _person(db, pid: int) -> Person | None:
    row = db.one("select id, name from people where id=?", (pid,))
    if not row:
        return None
    p = Person(row["id"], row["name"])
    for h in db.q("select network, handle from person_handles where person_id=? order by rowid", (pid,)):
        p.handles.setdefault(h["network"], []).append(h["handle"])
    return p


def get(db, pid: int) -> Person | None:
    return _person(db, pid)


def all_people(db) -> list[Person]:
    return [p for p in (_person(db, r["id"]) for r in db.q("select id from people order by lower(name), id")) if p]


def find(db, network: str, handle: str) -> Person | None:
    """The person with this handle. A bare handle matches an X account."""
    h = normalise(network, handle)
    row = db.one("select person_id from person_handles where network=? and handle=?", (network, h))
    return _person(db, row["person_id"]) if row else None


def find_any(db, handle: str) -> Person | None:
    """The person a handle typed in the composer belongs to, whatever network it's from."""
    h = normalise("", handle)
    native = unbridge(h)
    if native:
        return find(db, *native)
    s = shape(h)
    order = {"mastodon": ["mastodon"], "bluesky": ["bluesky"], "bare": ["x"]}[s]
    for network in order:
        p = find(db, network, h)
        if p:
            return p
    if s == "bare":
        # A local Mastodon username: match "alice" against "alice@server".
        row = db.one("select person_id from person_handles where network='mastodon' and handle like ? escape '\\'",
                     (h.replace("_", "\\_") + "@%",))
        if row:
            return _person(db, row["person_id"])
    return None


def save(db, name: str, handles: dict[str, list[str] | str], pid: int | None = None) -> int:
    """Create or replace a person. A handle already linked to someone else moves to this person."""
    name = (name or "").strip()
    if pid is None:
        pid = db.x("insert into people(name, created_at) values(?, ?)", (name, time.time()))
    else:
        db.x("update people set name=? where id=?", (name, pid))
        db.x("delete from person_handles where person_id=?", (pid,))
    for network, hs in handles.items():
        if network not in NETWORKS:
            continue
        for h in [hs] if isinstance(hs, str) else hs:
            h = normalise(network, h)
            if h:
                db.x("insert into person_handles(person_id, network, handle) values(?, ?, ?) "
                     "on conflict(network, handle) do update set person_id=excluded.person_id", (pid, network, h))
    _drop_empty(db)
    return pid


def link(db, network: str, handle: str, other_network: str, other_handle: str, name: str = "") -> int:
    """Say two accounts are the same person, merging the people they already belong to."""
    a, b = find(db, network, handle), find(db, other_network, other_handle)
    if a and b and a.id != b.id:
        db.x("update person_handles set person_id=? where person_id=?", (a.id, b.id))
        db.x("delete from people where id=?", (b.id,))
    target = a or b
    if target:
        db.x("insert or replace into person_handles(person_id, network, handle) values(?, ?, ?)",
             (target.id, network, normalise(network, handle)))
        db.x("insert or replace into person_handles(person_id, network, handle) values(?, ?, ?)",
             (target.id, other_network, normalise(other_network, other_handle)))
        if name and not target.name:
            db.x("update people set name=? where id=?", (name, target.id))
        return target.id
    return save(db, name, {network: [handle], other_network: [other_handle]})


def unlink(db, network: str, handle: str) -> None:
    db.x("delete from person_handles where network=? and handle=?", (network, normalise(network, handle)))
    _drop_empty(db)


def delete(db, pid: int) -> None:
    db.x("delete from person_handles where person_id=?", (pid,))
    db.x("delete from people where id=?", (pid,))


def _drop_empty(db) -> None:
    db.x("delete from people where id not in (select person_id from person_handles)")


# ---------- accounts seen in timelines, for autocomplete ----------

def remember(db, accounts: list[Account]) -> None:
    now = time.time()
    rows = {}
    for a in accounts:
        if a and a.handle and a.network in NETWORKS:
            rows[(a.network, normalise(a.network, a.handle))] = (a.display_name or "", a.avatar or "")
    if rows:
        db.xmany("""insert into seen_accounts(network, handle, display_name, avatar, last_seen) values(?, ?, ?, ?, ?)
                    on conflict(network, handle) do update set display_name=excluded.display_name,
                    avatar=excluded.avatar, last_seen=excluded.last_seen""",
                 [(n, h, d, av, now) for (n, h), (d, av) in rows.items()])


def seen(db, network: str, handle: str) -> Account | None:
    row = db.one("select * from seen_accounts where network=? and handle=?", (network, normalise(network, handle)))
    return Account(network=network, id=row["handle"], handle=row["handle"], display_name=row["display_name"],
                   avatar=row["avatar"]) if row else None


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def suggest_local(db, q: str, limit: int = 8) -> tuple[list[Person], list[Account]]:
    """People and recently seen accounts whose name or handle contains q."""
    q = q.strip().lstrip("@").lower()
    like = _like(q)
    pids = [r["id"] for r in db.q(
        """select distinct p.id from people p left join person_handles h on h.person_id=p.id
           where lower(p.name) like ? escape '\\' or h.handle like ? escape '\\' limit ?""", (like, like, limit))]
    found = [p for p in (_person(db, i) for i in pids) if p]
    linked = {(n, h) for p in found for n, h in p.all_handles()}
    rows = db.q("""select * from seen_accounts where handle like ? escape '\\' or lower(display_name) like ? escape '\\'
                   order by (handle like ? escape '\\') desc, last_seen desc limit ?""",
                (like, like, _like(q)[1:], limit * 2))
    accts = [Account(network=r["network"], id=r["handle"], handle=r["handle"], display_name=r["display_name"],
                     avatar=r["avatar"]) for r in rows if (r["network"], r["handle"]) not in linked]
    return found, accts[:limit]


# ---------- rewriting mentions for each network ----------

@dataclass
class Mention:
    start: int
    end: int
    original: str      # as typed, with the @
    replacement: str   # what the target network gets
    how: str           # "native", "linked", "bridged", "plain", "name"
    person: str = ""   # the linked person's name, if any


def find_mentions(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in MENTION_ANY_RE.finditer(text)]


def bridge_candidates(db, text: str, target: str) -> list[str]:
    """Bridged handles worth checking before rewriting text for target: foreign mentions of people
    with no account on target that we know of."""
    out = []
    for _, _, raw in find_mentions(text):
        person = find_any(db, raw)
        if person:
            if not person.handle_on(target):
                out += [b for n, h in person.all_handles() if n != "x" and (b := bridged(h, target))]
        elif unbridge(raw) is None and shape(raw) not in ("bare", _native_shape(target)):
            b = bridged(raw, target)
            if b:
                out.append(b)
    return out


def _native_shape(network: str) -> str:
    return {"bluesky": "bluesky", "mastodon": "mastodon", "x": "bare"}.get(network, "")


def plan(db, text: str, target: str, bridges: set[str] | None = None) -> list[Mention]:
    """How each mention in text should be written on the target network.

    bridges: bridged handles known to exist (from check_bridges); others aren't used."""
    bridges = bridges or set()
    out = []
    for start, end, raw in find_mentions(text):
        h = normalise("", raw)
        s = shape(h)
        native = unbridge(h)
        person = find_any(db, h)
        if person:
            mine = person.handle_on(target)
            if mine:
                out.append(Mention(start, end, raw, "@" + mine, "native" if mine == h else "linked", person.name))
                continue
            # Linked, but not on this network: their bridged handle if it exists, else their name.
            for n, other in person.all_handles():
                b = bridged(other, target) if n != "x" else None
                if b and b in bridges:
                    out.append(Mention(start, end, raw, "@" + b, "bridged", person.name))
                    break
            else:
                out.append(Mention(start, end, raw, person.name or h, "name" if person.name else "plain", person.name))
            continue
        if native and native[0] == target:
            out.append(Mention(start, end, raw, "@" + native[1], "linked"))
        elif s == "bare" or s == _native_shape(target):
            out.append(Mention(start, end, raw, raw, "native"))
        else:
            b = bridged(h, target)
            if b and b in bridges:
                out.append(Mention(start, end, raw, "@" + b, "bridged"))
            else:
                out.append(Mention(start, end, raw, raw.lstrip("@"), "plain"))
    return out


def apply(text: str, mentions: list[Mention]) -> str:
    for m in sorted(mentions, key=lambda m: m.start, reverse=True):
        text = text[: m.start] + m.replacement + text[m.end:]
    return text


async def check_bridges(db, adapter, candidates: list[str]) -> set[str]:
    """Which bridged handles exist, cached for a day. Uses the target account to look them up."""
    from .adapters.bluesky_oauth import resolve_handle

    found = set()
    for b in dict.fromkeys(candidates):
        key = f"bridge:{adapter.network}:{b}"
        cached = db.get(key)
        if cached and time.time() - cached["at"] < 86400:
            ok = cached["ok"]
        else:
            try:
                if adapter.network == "bluesky":
                    ok = bool(await resolve_handle(b))
                else:
                    ok = bool((await adapter.profile(b)).handle)
            except Exception:
                ok = False
            db.set(key, {"ok": ok, "at": time.time()})
        if ok:
            found.add(b)
    return found


async def rewrite(db, text: str, adapter, look_up_bridges: bool = True) -> tuple[str, list[Mention]]:
    """Text with each mention written the way adapter's network needs it."""
    target = adapter.network
    bridges: set[str] = set()
    if look_up_bridges and target in ("bluesky", "mastodon") and not adapter.account_id.startswith("demo-"):
        bridges = await check_bridges(db, adapter, bridge_candidates(db, text, target))
    mentions = plan(db, text, target, bridges)
    return apply(text, mentions), mentions


def reply_mentions(post, my_handles: set[str]) -> str:
    """Mastodon only notifies people you @mention, so a reply starts with the author and anyone they mentioned."""
    handles = [post.author.handle] + [s.target for s in post.text if s.kind == "mention" and s.target]
    seen_: list[str] = []
    for h in handles:
        h = h.lstrip("@")
        if h and h.lower() not in my_handles and h.lower() not in [x.lower() for x in seen_]:
            seen_.append(h)
    return "".join(f"@{h} " for h in seen_)


# ---------- other accounts named in a profile ----------

_BIO_PATTERNS = [
    ("bluesky", re.compile(r"bsky\.app/profile/([A-Za-z0-9.-]+\.[A-Za-z]{2,})")),
    ("x", re.compile(r"(?:twitter|x)\.com/(?!intent|share|home|i/)([A-Za-z0-9_]{1,15})\b")),
    ("mastodon", re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})/@([A-Za-z0-9_]+)\b(?!@)")),
    ("mastodon", MENTION_ANY_RE),
]


def handles_in_bio(account: Account) -> list[tuple[str, str]]:
    """Accounts on other networks a profile points to: links and handles in its bio and profile fields."""
    text = account.bio or ""
    out: list[tuple[str, str]] = []
    for network, rx in _BIO_PATTERNS:
        for m in rx.finditer(text):
            if rx is MENTION_ANY_RE:
                if not m.group(2):
                    continue
                network, h = "mastodon", f"{m.group(1)}@{m.group(2)}"
            elif network == "mastodon":
                if m.group(1) in ("bsky.app", "x.com", "twitter.com"):
                    continue
                h = f"{m.group(2)}@{m.group(1)}"
            else:
                h = m.group(1)
            native = unbridge(h)
            if native:
                network, h = native
            h = normalise(network, h)
            if network != account.network and (network, h) not in out:
                out.append((network, h))
    return out
