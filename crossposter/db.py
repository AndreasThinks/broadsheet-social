"""SQLite storage. One small file, one connection, guarded by a lock."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists kv (key text primary key, value text not null);
create table if not exists accounts (
    id text primary key, network text not null, native_id text not null,
    handle text not null, display_name text, avatar text, server text,
    tokens text, created_at real not null
);
create table if not exists mastodon_apps (
    instance text primary key, client_id text not null, client_secret text not null
);
create table if not exists oauth_states (
    state text primary key, network text not null, data text not null, created_at real not null
);
create table if not exists posts (
    key text primary key, account_id text not null, network text not null,
    post_id text not null, created_at real not null, fetched_at real not null,
    sources text not null, data text not null
);
create index if not exists posts_created on posts(created_at);
create table if not exists notifications (
    key text primary key, account_id text not null, network text not null,
    created_at real not null, data text not null, pushed integer not null default 0
);
create index if not exists notifications_created on notifications(created_at);
create table if not exists push_subs (endpoint text primary key, data text not null, created_at real not null);
create table if not exists cross_posts (
    id integer primary key autoincrement, created_at real not null, text text not null, results text not null
);
create table if not exists x_usage (
    month text primary key, post_reads integer not null default 0, user_reads integer not null default 0,
    posts integer not null default 0, link_posts integer not null default 0
);
create table if not exists people (
    id integer primary key autoincrement, name text not null default '', created_at real not null
);
create table if not exists person_handles (
    person_id integer not null, network text not null, handle text not null,
    primary key(network, handle)
);
create index if not exists person_handles_person on person_handles(person_id);
create table if not exists seen_accounts (
    network text not null, handle text not null, display_name text not null default '',
    avatar text not null default '', last_seen real not null, primary key(network, handle)
);
"""


class DB:
    def __init__(self, path: Path | str, wal: bool = True):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if wal:  # the browser build's file system has no shared memory for WAL
            self.conn.execute("pragma journal_mode=wal")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def xmany(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    # Small key/value store for generated keys and preferences.
    def get(self, key: str, default=None):
        row = self.one("select value from kv where key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        self.x("insert into kv(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
               (key, json.dumps(value)))

    # OAuth round-trip state, kept for 15 minutes.
    def put_state(self, state: str, network: str, data: dict) -> None:
        self.x("delete from oauth_states where created_at < ?", (time.time() - 900,))
        self.x("insert into oauth_states values(?, ?, ?, ?)", (state, network, json.dumps(data), time.time()))

    def pop_state(self, state: str, network: str) -> dict | None:
        row = self.one("select data from oauth_states where state=? and network=? and created_at > ?",
                       (state, network, time.time() - 900))
        self.x("delete from oauth_states where state=?", (state,))
        return json.loads(row["data"]) if row else None
