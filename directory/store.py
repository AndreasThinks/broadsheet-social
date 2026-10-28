"""The directory's data: listed feeds, submissions waiting for review, download counts and anonymous weekly stats.

Privacy rules this file keeps:
- Stats reports are folded into per-feed weekly totals as they arrive and then thrown away. No report is stored,
  so nothing can link one week to the next or to a person.
- No IP addresses, device IDs or account details are stored anywhere.
- Submissions are tied to the app that sent them only by a receipt the app makes up; we keep its SHA-256.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from crossposter import feedcheck

MIN_REPORTS = 10          # a weekly figure is only shown once at least this many apps reported it
INSTALLED_FOR = ("0-7d", "8-30d", "30d+")

SCHEMA = """
create table if not exists feeds (
    slug text primary key, name text not null, description text not null default '', sources text not null,
    credit text not null default '', version integer not null, source text not null, owner_hash text not null default '',
    listed integer not null default 1, reviewed integer not null default 1, created_at real not null, updated_at real not null
);
create table if not exists versions (
    slug text not null, version integer not null, source text not null, note text not null default '',
    created_at real not null, primary key (slug, version)
);
create table if not exists submissions (
    id integer primary key autoincrement, receipt_hash text not null, slug text, name text not null,
    description text not null default '', credit text not null default '', source text not null, checks text not null,
    status text not null, reviewer_note text not null default '', note text not null default '',
    created_at real not null, updated_at real not null
);
create table if not exists downloads (slug text not null, day text not null, n integer not null, primary key (slug, day));
create table if not exists weekly (
    slug text not null, week text not null, reports integer not null default 0, users integer not null default 0,
    on_home integer not null default 0, visits integer not null default 0, posts_read integer not null default 0,
    acted integer not null default 0, long_term integer not null default 0, pos1 integer not null default 0,
    pos2 integer not null default 0, pos3 integer not null default 0, pos4 integer not null default 0,
    primary key (slug, week)
);
create table if not exists kv (key text primary key, value text not null);
"""

STATUSES = ("pending", "changes", "approved", "declined", "withdrawn")


class Rejected(ValueError):
    """A request the directory won't accept; the message is safe to show."""


def receipt_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode()).hexdigest()


def week_of(t: float | None = None) -> str:
    y, w, _ = dt.datetime.fromtimestamp(t or time.time(), dt.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def recent_weeks(n: int, t: float | None = None) -> list[str]:
    """The last n ISO weeks, oldest first, ending with this week."""
    now = dt.datetime.fromtimestamp(t or time.time(), dt.timezone.utc)
    return [week_of((now - dt.timedelta(weeks=i)).timestamp()) for i in reversed(range(n))]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:50] or "feed"


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ---------- listed feeds ----------

    def seed(self, folder: Path, credit: str = "") -> None:
        """List the app's starter feeds the first time the directory starts, so it isn't empty."""
        if self.one("select value from kv where key='seeded'"):
            return
        for path in sorted(folder.glob("*.py")):
            source = path.read_text()
            r = feedcheck.check(source)
            if r.ok:
                self._list(slugify(r.name), r, source, credit, "", "First listed")
        self.x("insert or replace into kv values('seeded', '1')")

    def _list(self, slug: str, r: feedcheck.Result, source: str, credit: str, owner_hash: str, note: str) -> str:
        now = time.time()
        row = self.one("select version from feeds where slug=?", (slug,))
        version = (row["version"] + 1) if row else 1
        with self.lock:
            self.conn.execute(
                "insert into feeds(slug, name, description, sources, credit, version, source, owner_hash, created_at, updated_at) "
                "values(?,?,?,?,?,?,?,?,?,?) on conflict(slug) do update set name=excluded.name, "
                "description=excluded.description, sources=excluded.sources, version=excluded.version, "
                "source=excluded.source, updated_at=excluded.updated_at, listed=1",
                (slug, r.name, r.description, json.dumps(r.sources), credit, version, source, owner_hash, now, now))
            self.conn.execute("insert into versions values(?,?,?,?,?)", (slug, version, source, note, now))
            self.conn.commit()
        return slug

    def feeds(self) -> list[dict]:
        return [self._feed(row) for row in self.q("select * from feeds where listed=1 order by created_at")]

    def feed(self, slug: str) -> dict | None:
        row = self.one("select * from feeds where slug=? and listed=1", (slug,))
        return self._feed(row) if row else None

    def _feed(self, row) -> dict:
        d = dict(row)
        d["sources"] = json.loads(d["sources"])
        d.pop("owner_hash", None)
        d["downloads"] = self.downloads(d["slug"])
        d["downloads_week"] = self.downloads(d["slug"], days=7)
        d["stats"] = self.stats(d["slug"])
        return d

    def versions(self, slug: str) -> list[dict]:
        return [dict(r) for r in self.q("select version, note, created_at from versions where slug=? order by version desc",
                                        (slug,))]

    def unlist(self, slug: str) -> None:
        self.x("update feeds set listed=0 where slug=?", (slug,))

    # ---------- downloads ----------

    def count_download(self, slug: str) -> None:
        day = dt.date.today().isoformat()
        self.x("insert into downloads values(?,?,1) on conflict(slug, day) do update set n=n+1", (slug, day))

    def downloads(self, slug: str, days: int | None = None) -> int:
        if days is None:
            row = self.one("select coalesce(sum(n),0) as n from downloads where slug=?", (slug,))
        else:
            since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
            row = self.one("select coalesce(sum(n),0) as n from downloads where slug=? and day>=?", (slug, since))
        return row["n"]

    def total_downloads(self) -> int:
        return self.one("select coalesce(sum(n),0) as n from downloads")["n"]

    # ---------- anonymous weekly stats ----------

    def record_report(self, report: dict, now: float | None = None) -> int:
        """Fold one app's weekly report into the totals. Returns how many feeds it counted. The report itself is not kept."""
        if not isinstance(report, dict) or set(report) - {"week", "feeds"}:
            raise Rejected("A report has exactly two keys: week and feeds.")
        week = report.get("week")
        if week not in recent_weeks(3, now):
            raise Rejected("Reports are only taken for the last couple of weeks.")
        items = report.get("feeds")
        if not isinstance(items, list) or len(items) > 100:
            raise Rejected("feeds must be a list of at most 100 entries.")
        listed = {r["slug"] for r in self.q("select slug from feeds where listed=1")}
        rows, seen = [], set()
        allowed = {"feed", "on_home", "installed_for", "visits", "posts_read", "posts_liked_or_replied", "tab_position"}
        for it in items:
            if not isinstance(it, dict) or set(it) - allowed:
                raise Rejected(f"Each feed entry may only have: {', '.join(sorted(allowed))}.")
            slug = it.get("feed")
            if slug not in listed or slug in seen:
                continue
            seen.add(slug)
            visits, read, acted = (_count(it.get(k), cap) for k, cap in
                                   (("visits", 1000), ("posts_read", 50_000), ("posts_liked_or_replied", 5_000)))
            if it.get("installed_for") not in INSTALLED_FOR:
                raise Rejected("installed_for must be one of " + ", ".join(INSTALLED_FOR))
            pos = it.get("tab_position")
            pos = pos if isinstance(pos, int) and not isinstance(pos, bool) and pos > 0 else 0
            rows.append((slug, week, 1, int(visits > 0), int(bool(it.get("on_home"))), visits, read, min(acted, read),
                         int(it["installed_for"] == "30d+"), int(pos == 1), int(pos == 2), int(pos == 3), int(pos >= 4)))
        with self.lock:
            for r in rows:
                self.conn.execute(
                    "insert into weekly values(?,?,?,?,?,?,?,?,?,?,?,?,?) on conflict(slug, week) do update set "
                    "reports=reports+excluded.reports, users=users+excluded.users, on_home=on_home+excluded.on_home, "
                    "visits=visits+excluded.visits, posts_read=posts_read+excluded.posts_read, acted=acted+excluded.acted, "
                    "long_term=long_term+excluded.long_term, pos1=pos1+excluded.pos1, pos2=pos2+excluded.pos2, "
                    "pos3=pos3+excluded.pos3, pos4=pos4+excluded.pos4", r)
            self.conn.commit()
        return len(rows)

    def weekly(self, slug: str, weeks: int = 12, now: float | None = None) -> list[dict]:
        """Weekly totals, oldest first. Weeks with fewer than MIN_REPORTS reports come back with shown=False and no figures."""
        got = {r["week"]: dict(r) for r in self.q("select * from weekly where slug=?", (slug,))}
        out = []
        for w in recent_weeks(weeks, now):
            r = got.get(w)
            if r and r["reports"] >= MIN_REPORTS:
                out.append({**r, "shown": True})
            else:
                out.append({"week": w, "shown": False})
        return out

    def stats(self, slug: str, now: float | None = None) -> dict | None:
        """Headline figures from the most recent week that has enough reports (this week or last), else None."""
        ws = [w for w in self.weekly(slug, 3, now) if w["shown"]]
        if not ws:
            return None
        w = ws[-1]
        prev = next((p for p in reversed(ws[:-1])), None)
        placed = w["pos1"] + w["pos2"] + w["pos3"] + w["pos4"]
        return {
            "week": w["week"], "reports": w["reports"], "users": w["users"],
            "users_change": (w["users"] - prev["users"]) / prev["users"] if prev and prev["users"] else None,
            "long_term_share": w["long_term"] / w["reports"],
            "read_per_visit": w["posts_read"] / w["visits"] if w["visits"] else 0,
            "acted_share": w["acted"] / w["posts_read"] if w["posts_read"] else 0,
            "positions": [w[k] / placed if placed else 0 for k in ("pos1", "pos2", "pos3", "pos4")],
        }

    def reporting_apps(self, now: float | None = None) -> int:
        """Roughly how many apps share stats: the most reports any one week got for its busiest feed."""
        w = recent_weeks(2, now)
        row = self.one("select coalesce(max(reports),0) as n from weekly where week in (?,?)", tuple(w))
        return row["n"]

    # ---------- submissions ----------

    def submit(self, receipt: str, name: str, description: str, credit: str, source: str, slug: str = "",
               note: str = "") -> dict:
        if not isinstance(receipt, str) or len(receipt) < 32 or len(receipt) > 200:
            raise Rejected("A submission needs a receipt of 32 to 200 characters.")
        r = feedcheck.check(source if isinstance(source, str) else "")
        if not r.ok:
            raise Rejected("The feed didn't pass the checks: " + "; ".join(i.text for i in r.errors))
        name = (name or r.name).strip()[:80]
        description = (description or r.description).strip()[:200]
        credit = (credit or "").strip()[:80]
        h = receipt_hash(receipt)
        if slug:
            row = self.one("select owner_hash from feeds where slug=?", (slug,))
            if not row or row["owner_hash"] != h:
                raise Rejected("Only the app that first shared a feed can send an update to it.")
        if self.one("select count(*) as n from submissions where status='pending' and created_at > ?",
                    (time.time() - 3600,))["n"] > 200:
            raise Rejected("Lots of feeds are waiting for review right now. Try again in an hour.")
        # one open submission per feed per app: a resend replaces the earlier one
        now = time.time()
        if slug:
            self.x("update submissions set status='withdrawn', updated_at=? where receipt_hash=? and slug=? "
                   "and status in ('pending','changes')", (now, h, slug))
        else:
            self.x("update submissions set status='withdrawn', updated_at=? where receipt_hash=? and slug is null "
                   "and name=? and status in ('pending','changes')", (now, h, name))
        sid = self.x("insert into submissions(receipt_hash, slug, name, description, credit, source, checks, status, note, "
                     "created_at, updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                     (h, slug or None, name, description, credit, source, json.dumps(r.as_dict()), "pending", note[:200],
                      now, now))
        return self.submission(sid)

    def submission(self, sid: int) -> dict | None:
        row = self.one("select * from submissions where id=?", (sid,))
        if not row:
            return None
        d = dict(row)
        d["checks"] = json.loads(d["checks"])
        return d

    def queue(self) -> list[dict]:
        return [self.submission(r["id"]) for r in
                self.q("select id from submissions where status='pending' order by created_at")]

    def mine(self, receipts: list[str]) -> list[dict]:
        """Status of every submission made with these receipts, newest first, without the source."""
        hashes = [receipt_hash(r) for r in receipts if isinstance(r, str)][:200]
        if not hashes:
            return []
        marks = ",".join("?" * len(hashes))
        rows = self.q(f"select id, receipt_hash, slug, name, status, reviewer_note, created_at, updated_at from submissions "
                      f"where receipt_hash in ({marks}) and status != 'withdrawn' order by created_at desc", hashes)
        out = []
        for r in rows:
            d = dict(r)
            d.pop("receipt_hash")
            f = self.one("select version from feeds where slug=? and listed=1", (d["slug"],)) if d["slug"] else None
            d["version"] = f["version"] if f else None
            d["downloads"] = self.downloads(d["slug"]) if f else 0
            out.append(d)
        return out

    def withdraw(self, sid: int, receipt: str) -> bool:
        s = self.submission(sid)
        if not s or s["receipt_hash"] != receipt_hash(receipt) or s["status"] not in ("pending", "changes"):
            return False
        self.x("update submissions set status='withdrawn', updated_at=? where id=?", (time.time(), sid))
        return True

    def review(self, sid: int, decision: str, reviewer_note: str = "") -> dict:
        s = self.submission(sid)
        if not s or s["status"] != "pending":
            raise Rejected("That submission isn't waiting for review.")
        if decision == "approve":
            r = feedcheck.check(s["source"])
            if not r.ok:
                raise Rejected("It no longer passes the checks.")
            r.name, r.description = s["name"], s["description"]
            slug = s["slug"]
            if not slug:
                base = slugify(s["name"])
                slug, n = base, 2
                while self.one("select 1 from feeds where slug=?", (slug,)):
                    slug, n = f"{base}-{n}", n + 1
            self._list(slug, r, s["source"], s["credit"], s["receipt_hash"], s["note"] or ("First listed" if not s["slug"] else "Updated"))
            if s["slug"]:
                self.x("update feeds set credit=? where slug=?", (s["credit"], slug))
            self.x("update submissions set status='approved', slug=?, reviewer_note=?, updated_at=? where id=?",
                   (slug, reviewer_note[:1000], time.time(), sid))
        elif decision in ("changes", "declined"):
            if decision == "changes" and not reviewer_note.strip():
                raise Rejected("Say what needs changing; the note is shown to the author.")
            self.x("update submissions set status=?, reviewer_note=?, updated_at=? where id=?",
                   (decision, reviewer_note[:1000], time.time(), sid))
        else:
            raise Rejected("Unknown decision.")
        return self.submission(sid)


def _count(v, cap: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise Rejected("Counts must be whole numbers, 0 or more.")
    return min(v, cap)
