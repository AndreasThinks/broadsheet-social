"""Offline demo accounts (DEMO=true): sample posts on both networks, so the UI can be tried without signing in."""
from __future__ import annotations

import random
import time

from ..models import Account, FeedInfo, LinkCard, Media, Notification, Page, Post
from ..richtext import plain_segments
from .base import AccountRecord, NetworkAdapter, PostResult

PEOPLE = [
    ("ada.bsky.social", "Ada Lovelace", "bluesky"), ("grace@hachyderm.io", "Grace Hopper", "mastodon"),
    ("alan.bsky.social", "Alan Turing", "bluesky"), ("katherine@mastodon.social", "Katherine Johnson", "mastodon"),
    ("linus.bsky.social", "Linus T.", "bluesky"), ("margaret@fosstodon.org", "Margaret Hamilton", "mastodon"),
]
TEXTS = [
    "Shipped a tiny static site generator this weekend. 200 lines of Python and no build step. https://example.com/blog/tiny-ssg",
    "Hot take: the best feed algorithm is the one you can read.",
    "Reading list for the week: distributed systems papers, a novel, and far too many RFCs.",
    "Anyone else find that cross-posting doubles the replies but halves the arguments? #fediverse",
    "New photo from this morning's walk along the river.",
    "Reminder that alt text is for everyone. Describe your images!",
    "Spent the afternoon debugging DPoP nonces. Learned a lot, enjoyed little.",
    "Our meetup is next Thursday, all welcome. Details: https://example.org/meetup #python",
]


def _people():
    return [Account(network=n, id=h, handle=h, display_name=d, avatar=f"https://api.dicebear.com/9.x/initials/svg?seed={d.replace(' ', '%20')}",
                    url="#") for h, d, n in PEOPLE]


class DemoAdapter(NetworkAdapter):
    label = "Demo"
    char_limit = 300
    _posted: list[Post] = []

    def __init__(self, network: str, db):
        handle = "you.bsky.social" if network == "bluesky" else "you@mastodon.social"
        super().__init__(AccountRecord(id=f"demo-{network}", network=network, native_id=handle, handle=handle,
                                       display_name="You (demo)", avatar="", server="", tokens={}), db)
        self.network = network
        self.label = "Bluesky" if network == "bluesky" else "Mastodon"
        self.char_limit = 300 if network == "bluesky" else 500

    def _posts(self, seed: int, n: int = 20) -> list[Post]:
        rnd = random.Random(seed)
        now = time.time()
        people = [p for p in _people() if p.network == self.network] + _people()[:1]
        out = []
        for i in range(n):
            author = rnd.choice(people)
            text = rnd.choice(TEXTS)
            p = Post(network=self.network, id=f"{self.network}-{seed}-{i}", account_id=self.account_id, author=author,
                     created_at=now - i * rnd.randint(300, 2400), text=plain_segments(text), url="#",
                     likes=rnd.randint(0, 400), reposts=rnd.randint(0, 120), replies=rnd.randint(0, 60))
            if "photo" in text:
                count = rnd.choice([1, 1, 2, 3, 5])
                p.media = [Media("image", url=f"https://picsum.photos/seed/{seed}{i}{k}/1200/800",
                                 thumb=f"https://picsum.photos/seed/{seed}{i}{k}/600/400",
                                 alt="A river at sunrise with mist over the water" if k == 0 else "",
                                 width=1200, height=800) for k in range(count)]
            if "example.com/blog" in text:
                p.card = LinkCard(url="https://example.com/blog/tiny-ssg", title="A tiny static site generator",
                                  description="Two hundred lines of Python, no build step, no regrets.",
                                  image=f"https://picsum.photos/seed/card{seed}/600/315")
            if i % 7 == 3:
                p.reposted_by = rnd.choice(people)
            out.append(p)
        return out

    async def feeds(self):
        if self.network == "bluesky":
            return [FeedInfo("bluesky:following", "Following", self.account_id, pinned=True),
                    FeedInfo("bluesky:discover", "Discover", self.account_id)]
        return [FeedInfo("mastodon:home", "Home", self.account_id, pinned=True),
                FeedInfo("mastodon:trending", "Trending", self.account_id)]

    async def timeline(self, source, cursor=None, limit=30):
        page = int(cursor or 0)
        posts = [p for p in self._posted if p.network == self.network] if page == 0 else []
        return Page(posts + self._posts(hash((source, page)) % 1000), str(page + 1) if page < 3 else None)

    async def notifications(self, limit=40):
        people = _people()
        posts = self._posts(7, 5)
        kinds = ["reply", "like", "repost", "follow", "mention"]
        return [Notification(network=self.network, id=f"n{i}", account_id=self.account_id, kind=kinds[i % 5],
                             actor=people[i % len(people)], created_at=time.time() - i * 900,
                             post=posts[i] if kinds[i % 5] != "follow" else None) for i in range(5)]

    async def thread(self, post_id):
        posts = self._posts(3, 6)
        return posts[:1], posts[1], posts[2:]

    async def profile(self, handle):
        return next((p for p in _people() if p.handle == handle), _people()[0])

    async def search_people(self, q, limit=6):
        q = q.lower().lstrip("@")
        return [p for p in _people() if p.network == self.network
                and (q in p.handle.lower() or q in p.display_name.lower())][:limit]

    async def author_feed(self, handle, cursor=None):
        return Page(self._posts(11, 10), None)

    async def post(self, text, images, reply_to=None, content_warning="", thread=False):
        p = Post(network=self.network, id=f"mine-{time.time()}", account_id=self.account_id, author=self.me(),
                 created_at=time.time(), text=plain_segments(text), url="#", content_warning=content_warning)
        self._posted.insert(0, p)
        return [PostResult(id=p.id, url="#")]

    async def like(self, post, on):
        return None

    async def repost(self, post, on):
        return None
