"""The interface every network implements, plus shared helpers."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from ..crypto import decrypt_json, encrypt_json
from ..models import Account, FeedInfo, Notification, Page, Post

USER_AGENT = "Broadsheet/0.1 (+https://github.com/AndreasThinks/broadsheet-social)"
_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    """One shared HTTP client for the whole app."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    return _client


def parse_time(s: str | None) -> float:
    if not s:
        return time.time()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


class NetworkError(Exception):
    """A network call failed in a way worth showing to the user."""


@dataclass
class Upload:
    data: bytes
    mime: str
    alt: str = ""
    filename: str = "image"


@dataclass
class PostResult:
    id: str
    url: str
    extra: dict = field(default_factory=dict)


@dataclass
class AccountRecord:
    """A signed-in account as stored in the accounts table (tokens decrypted)."""
    id: str            # "<network>:<native id>"
    network: str
    native_id: str
    handle: str
    display_name: str
    avatar: str
    server: str        # Mastodon instance URL, Bluesky PDS URL
    tokens: dict

    @classmethod
    def from_row(cls, row) -> AccountRecord:
        return cls(id=row["id"], network=row["network"], native_id=row["native_id"], handle=row["handle"],
                   display_name=row["display_name"] or "", avatar=row["avatar"] or "",
                   server=row["server"] or "", tokens=decrypt_json(row["tokens"]))


def save_account(db, rec: AccountRecord) -> None:
    db.x("""insert into accounts(id, network, native_id, handle, display_name, avatar, server, tokens, created_at)
            values(?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set handle=excluded.handle, display_name=excluded.display_name,
            avatar=excluded.avatar, server=excluded.server, tokens=excluded.tokens""",
         (rec.id, rec.network, rec.native_id, rec.handle, rec.display_name, rec.avatar, rec.server,
          encrypt_json(rec.tokens), time.time()))


class NetworkAdapter(ABC):
    network: str = ""
    label: str = ""
    char_limit: int = 500
    max_images: int = 4
    experimental: bool = False

    def __init__(self, record: AccountRecord, db):
        self.rec = record
        self.db = db

    @property
    def account_id(self) -> str:
        return self.rec.id

    def save_tokens(self, tokens: dict) -> None:
        self.rec.tokens = tokens
        self.db.x("update accounts set tokens=? where id=?", (encrypt_json(tokens), self.rec.id))

    def me(self) -> Account:
        return Account(network=self.network, id=self.rec.native_id, handle=self.rec.handle,
                       display_name=self.rec.display_name, avatar=self.rec.avatar)

    # Reading
    @abstractmethod
    async def feeds(self) -> list[FeedInfo]: ...

    @abstractmethod
    async def timeline(self, source: str, cursor: str | None = None, limit: int = 30) -> Page: ...

    @abstractmethod
    async def notifications(self, limit: int = 40) -> list[Notification]: ...

    @abstractmethod
    async def thread(self, post_id: str) -> tuple[list[Post], Post, list[Post]]: ...

    async def get_post(self, post_id: str) -> Post:
        """One post by id, for the "replying to" preview. Optional."""
        raise NetworkError(f"{self.label} can't fetch single posts here.")

    @abstractmethod
    async def profile(self, handle: str) -> Account: ...

    @abstractmethod
    async def author_feed(self, handle: str, cursor: str | None = None) -> Page: ...

    # Writing
    @abstractmethod
    async def post(self, text: str, images: list[Upload], reply_to: Post | None = None,
                   content_warning: str = "", thread: bool = False) -> list[PostResult]: ...

    @abstractmethod
    async def like(self, post: Post, on: bool) -> None: ...

    @abstractmethod
    async def repost(self, post: Post, on: bool) -> None: ...

    async def vote(self, post: Post, choices: list[int]):
        """Vote in the post's poll; returns the updated Poll. Optional."""
        raise NetworkError(f"Voting in polls isn't possible on {self.label} from Broadsheet.")

    async def max_chars(self) -> int:
        return self.char_limit

    async def search_people(self, q: str, limit: int = 6) -> list[Account]:
        """Accounts matching a partial name or handle, for @mention autocomplete. Optional."""
        return []
