"""The unified model every adapter returns. The UI and custom feeds only see these."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class Segment:
    """A run of rich text: plain text, a link, a mention or a hashtag."""
    kind: str  # "text" | "link" | "mention" | "tag"
    text: str
    href: str = ""    # external URL for links, profile URL for mentions
    target: str = ""  # mention: the account's handle (or DID); tag: the tag name


@dataclass
class Account:
    network: str
    id: str             # DID on Bluesky, numeric id on Mastodon (server-local) or X
    handle: str         # "alice.bsky.social", "alice@mastodon.social", "alice" on X
    display_name: str = ""
    avatar: str = ""
    url: str = ""       # profile on the network's own website
    bio: str = ""
    followers: int | None = None
    following: int | None = None
    posts: int | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.handle

    @property
    def at_handle(self) -> str:
        return "@" + self.handle


@dataclass
class Media:
    kind: str  # "image" | "video" | "gif" | "audio"
    url: str
    thumb: str = ""
    alt: str = ""
    width: int | None = None
    height: int | None = None
    stream: str = ""  # HLS playlist for Bluesky video


@dataclass
class LinkCard:
    url: str
    title: str = ""
    description: str = ""
    image: str = ""


@dataclass
class Post:
    network: str
    id: str                 # native id: at:// URI, Mastodon status id, X tweet id
    account_id: str         # which of your accounts fetched this, e.g. "bluesky:did:plc:..."
    author: Account
    created_at: float       # unix seconds
    text: list[Segment] = field(default_factory=list)
    url: str = ""
    media: list[Media] = field(default_factory=list)
    card: LinkCard | None = None
    quote: Post | None = None
    reposted_by: Account | None = None
    reply_to_handle: str = ""
    is_reply: bool = False
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    liked: bool = False
    reposted: bool = False
    content_warning: str = ""
    sensitive: bool = False
    lang: str = ""
    extra: dict[str, Any] = field(default_factory=dict)   # network-specific bits (cid, like uri, ...)
    also_on: list[dict] = field(default_factory=list)     # same post seen on another network

    @property
    def plain_text(self) -> str:
        return "".join(s.text for s in self.text)

    @property
    def key(self) -> str:
        """Unique across networks and accounts; safe for URLs once quoted."""
        return f"{self.account_id}|{self.id}"

    @property
    def engagement(self) -> int:
        return self.likes + self.reposts + self.replies + self.quotes

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Post:
        return _post_from_dict(d)


@dataclass
class Notification:
    network: str
    id: str
    account_id: str
    kind: str  # "reply" | "mention" | "quote" | "follow" | "like" | "repost" | "other"
    actor: Account
    created_at: float
    post: Post | None = None      # the post the notification is about (your post, or the reply)
    summary: str = ""

    @property
    def key(self) -> str:
        return f"{self.account_id}|{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Notification:
        d = dict(d)
        d["actor"] = _from(Account, d["actor"])
        d["post"] = _post_from_dict(d["post"]) if d.get("post") else None
        return cls(**_known(cls, d))


def _known(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _from(cls, d: dict | None):
    return cls(**_known(cls, d)) if d else None


def _post_from_dict(d: dict) -> Post:
    d = dict(d)
    d["author"] = _from(Account, d["author"])
    d["text"] = [_from(Segment, s) for s in d.get("text") or []]
    d["media"] = [_from(Media, m) for m in d.get("media") or []]
    d["card"] = _from(LinkCard, d.get("card"))
    d["quote"] = _post_from_dict(d["quote"]) if d.get("quote") else None
    d["reposted_by"] = _from(Account, d.get("reposted_by"))
    return Post(**_known(Post, d))


@dataclass
class Page:
    """One page of a timeline plus the cursor for the next."""
    posts: list[Post]
    cursor: str | None = None


@dataclass
class FeedInfo:
    """A native feed an account offers, e.g. Following or a Bluesky feed generator."""
    source: str     # "bluesky:following", "bluesky:feed:at://...", "mastodon:list:42"
    name: str
    account_id: str
    description: str = ""
    pinned: bool = False
