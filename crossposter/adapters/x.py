"""X (Twitter). X's API is pay-per-use, so every call costs money.

Without an X account, Broadsheet still posts to X by handing the text over to X's own compose window
(see handoff_url), which costs nothing and needs no developer app. Connecting an account ("direct
posting") needs the user's own X app. Posting is on once an account is connected. Reading is off until
you switch it on in Settings, and every call is checked against a monthly spend cap (X_MONTHLY_CAP_USD,
or the limit set in Settings). When the cap is reached, posts go through the handoff instead.

The X app's credentials come from X_CLIENT_ID / X_CLIENT_SECRET, or, where there are no environment
variables (the desktop and Android apps), from Settings, where they are stored encrypted. A "Native
App" (public client) needs only the client ID; a "Web App" also needs the secret.
Prices are from https://docs.x.com/x-api/getting-started/pricing (October 2026).
"""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..config import settings
from ..crypto import decrypt_json, encrypt_json, pkce_pair
from ..models import Account, FeedInfo, Media, Notification, Page, Post
from ..richtext import URL_RE, x_segments, split_for_thread
from .base import AccountRecord, NetworkAdapter, NetworkError, PostResult, Upload, http, parse_time, save_account

API = "https://api.x.com"
SCOPES = "tweet.read tweet.write users.read like.write media.write offline.access"
PRICES = {"post_reads": 0.005, "user_reads": 0.010, "posts": 0.015, "link_posts": 0.200}
TWEET_FIELDS = "created_at,public_metrics,entities,attachments,in_reply_to_user_id,lang,possibly_sensitive,conversation_id"
EXPANSIONS = "author_id,attachments.media_keys"
USER_FIELDS = "profile_image_url,username,name"
MEDIA_FIELDS = "url,preview_image_url,alt_text,width,height,type,variants"

HANDOFF_ID = "x-handoff"
DEVELOPER_CONSOLE = "https://console.x.com"

_lock = asyncio.Lock()


class XUnavailable(NetworkError):
    """X won't take this post right now (spend cap, out of credit, rate limited); the handoff still works."""


def handoff_enabled(db) -> bool:
    return settings.x_enabled and bool(db.get("x_handoff", True))


def handoff_url(text: str) -> str:
    return "https://x.com/intent/post?" + urlencode({"text": text})


class _HandoffRec:
    handle = "X"


class Handoff:
    """Stands in for an X account in the composer when posts go through X's own window."""
    network = "x"
    account_id = HANDOFF_ID
    rec = _HandoffRec()
    char_limit = 280

    async def max_chars(self) -> int:
        return self.char_limit


def callback_url() -> str:
    return f"{settings.base_url}/auth/x/callback"


def month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage(db) -> dict:
    row = db.one("select * from x_usage where month=?", (month(),))
    counts = {k: (row[k] if row else 0) for k in PRICES}
    counts["cost"] = round(sum(counts[k] * PRICES[k] for k in PRICES), 3)
    counts["cap"] = cap(db)
    counts["month"] = month()
    return counts


def record_usage(db, **counts) -> None:
    db.x("insert or ignore into x_usage(month) values(?)", (month(),))
    for k, v in counts.items():
        if v and k in PRICES:
            db.x(f"update x_usage set {k} = {k} + ? where month=?", (int(v), month()))


def cap(db) -> float:
    saved = db.get("x_cap")
    return float(saved) if saved is not None else settings.x_monthly_cap_usd


def check_budget(db, **planned) -> None:
    cost = sum(PRICES[k] * v for k, v in planned.items())
    u = usage(db)
    if u["cost"] + cost > u["cap"]:
        raise XUnavailable(f"X spend cap reached: ${u['cost']:.2f} of ${u['cap']:.2f} used this month.")


def reading_enabled(db) -> bool:
    return bool(db.get("x_read_enabled", False))


def post_by_default(db) -> bool:
    return bool(db.get("x_post_by_default", True))


def client_id(db) -> str:
    return settings.x_client_id or db.get("x_client_id", "") or ""


def client_secret(db) -> str:
    if settings.x_client_secret:
        return settings.x_client_secret
    try:
        return decrypt_json(db.get("x_client_secret")).get("secret", "")
    except Exception:  # SECRET_KEY changed since it was saved
        return ""


def credentials_from_env() -> bool:
    return bool(settings.x_client_id)


def save_credentials(db, cid: str, secret: str) -> None:
    db.set("x_client_id", cid.strip())
    db.set("x_client_secret", encrypt_json({"secret": secret.strip()}) if secret.strip() else "")


def _basic_auth(db):
    secret = client_secret(db)
    return (client_id(db), secret) if secret else None


async def start_login(db) -> str:
    if not settings.x_enabled:
        raise NetworkError("X is switched off on this server (X_ENABLED=false).")
    if not client_id(db):
        raise NetworkError("Add your X app's client ID in Settings first.")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    db.put_state(state, "x", {"verifier": verifier})
    return "https://x.com/i/oauth2/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id(db), "redirect_uri": callback_url(),
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


async def _token(db, data: dict) -> dict:
    r = await http().post(f"{API}/2/oauth2/token", data={**data, "client_id": client_id(db)}, auth=_basic_auth(db))
    if r.status_code >= 400:
        raise NetworkError(f"X sign-in failed ({r.status_code}): {r.text[:200]}")
    t = r.json()
    # X rotates refresh tokens; keep the old one if a response ever leaves it out.
    return {"access_token": t["access_token"], "refresh_token": t.get("refresh_token") or data.get("refresh_token"),
            "expires_at": time.time() + int(t.get("expires_in", 7200))}


async def finish_login(db, code: str, state: str) -> AccountRecord:
    st = db.pop_state(state, "x")
    if not st:
        raise NetworkError("The sign-in link expired. Please try again.")
    tokens = await _token(db, {"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url(),
                           "code_verifier": st["verifier"]})
    check_budget(db, user_reads=1)
    r = await http().get(f"{API}/2/users/me", params={"user.fields": USER_FIELDS},
                         headers={"Authorization": f"Bearer {tokens['access_token']}"})
    r.raise_for_status()
    record_usage(db, user_reads=1)
    u = r.json()["data"]
    rec = AccountRecord(id=f"x:{u['id']}", network="x", native_id=u["id"], handle=u["username"],
                        display_name=u.get("name", ""), avatar=u.get("profile_image_url", ""),
                        server=API, tokens=tokens)
    save_account(db, rec)
    return rec


def to_account(u: dict) -> Account:
    return Account(network="x", id=u.get("id", ""), handle=u.get("username", ""), display_name=u.get("name", ""),
                   avatar=u.get("profile_image_url", ""), url=f"https://x.com/{u.get('username', '')}")


def _parse(data: dict, account_id: str) -> list[Post]:
    inc = data.get("includes") or {}
    users = {u["id"]: u for u in inc.get("users") or []}
    media = {m["media_key"]: m for m in inc.get("media") or []}
    out = []
    for t in data.get("data") or []:
        author = to_account(users.get(t.get("author_id"), {"id": t.get("author_id", ""), "username": "unknown"}))
        ms = []
        for key in (t.get("attachments") or {}).get("media_keys") or []:
            m = media.get(key)
            if not m:
                continue
            if m["type"] == "photo":
                ms.append(Media("image", url=m.get("url", ""), thumb=m.get("url", ""), alt=m.get("alt_text") or "",
                                width=m.get("width"), height=m.get("height")))
            else:
                variants = sorted([v for v in m.get("variants") or [] if v.get("content_type") == "video/mp4"],
                                  key=lambda v: v.get("bit_rate", 0))
                ms.append(Media("gif" if m["type"] == "animated_gif" else "video",
                                url=variants[-1]["url"] if variants else "", thumb=m.get("preview_image_url", ""),
                                alt=m.get("alt_text") or ""))
        pm = t.get("public_metrics") or {}
        out.append(Post(network="x", id=t["id"], account_id=account_id, author=author,
                        created_at=parse_time(t.get("created_at")), text=x_segments(t.get("text", ""), t.get("entities")),
                        url=f"https://x.com/{author.handle}/status/{t['id']}", media=ms,
                        is_reply=bool(t.get("in_reply_to_user_id")), likes=pm.get("like_count", 0),
                        reposts=pm.get("retweet_count", 0), replies=pm.get("reply_count", 0),
                        quotes=pm.get("quote_count", 0), sensitive=bool(t.get("possibly_sensitive")),
                        lang=t.get("lang", "")))
    return out


class XAdapter(NetworkAdapter):
    network = "x"
    label = "X"
    char_limit = 280
    experimental = True  # shown as "costs money" in the composer

    async def _fresh(self) -> str:
        async with _lock:
            if self.rec.tokens.get("expires_at", 0) - 60 < time.time():
                if not self.rec.tokens.get("refresh_token"):
                    raise NetworkError("X session expired; please sign in again.")
                self.save_tokens(await _token(self.db, {"grant_type": "refresh_token",
                                               "refresh_token": self.rec.tokens["refresh_token"]}))
        return self.rec.tokens["access_token"]

    async def _req(self, method: str, path: str, **kw):
        token = await self._fresh()
        r = await http().request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code == 402 or r.status_code == 429:
            raise XUnavailable(f"X refused the request ({r.status_code}): out of credit or rate limited.")
        if r.status_code >= 400:
            raise NetworkError(f"X returned {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def _read(self, path: str, limit: int, **params) -> dict:
        if not reading_enabled(self.db):
            raise NetworkError("Reading from X is switched off (it costs money). Turn it on in Settings.")
        check_budget(self.db, post_reads=limit, user_reads=limit)
        data = await self._req("GET", path, params={"tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS,
                                                    "user.fields": USER_FIELDS, "media.fields": MEDIA_FIELDS,
                                                    **{k: v for k, v in params.items() if v is not None}})
        inc = data.get("includes") or {}
        record_usage(self.db, post_reads=len(data.get("data") or []) + len(inc.get("tweets") or []),
                     user_reads=len(inc.get("users") or []))
        return data

    async def feeds(self) -> list[FeedInfo]:
        return [FeedInfo("x:home", "Home (latest, paid)", self.account_id)]

    async def timeline(self, source: str, cursor: str | None = None, limit: int = 30) -> Page:
        limit = max(5, min(limit, 100))
        data = await self._read(f"/2/users/{self.rec.native_id}/timelines/reverse_chronological", limit,
                                max_results=limit, pagination_token=cursor)
        return Page(_parse(data, self.account_id), (data.get("meta") or {}).get("next_token"))

    async def notifications(self, limit: int = 20) -> list[Notification]:
        data = await self._read(f"/2/users/{self.rec.native_id}/mentions", limit, max_results=max(5, min(limit, 100)))
        return [Notification(network="x", id=p.id, account_id=self.account_id, kind="mention", actor=p.author,
                             created_at=p.created_at, post=p) for p in _parse(data, self.account_id)]

    async def thread(self, post_id: str):
        data = await self._read("/2/tweets", 1, ids=post_id)
        posts = _parse(data, self.account_id)
        if not posts:
            raise NetworkError("Post not found.")
        return [], posts[0], []

    async def profile(self, handle: str) -> Account:
        if not reading_enabled(self.db):
            raise NetworkError("Reading from X is switched off.")
        check_budget(self.db, user_reads=1)
        data = await self._req("GET", f"/2/users/by/username/{handle.lstrip('@')}",
                               params={"user.fields": USER_FIELDS + ",description,public_metrics"})
        record_usage(self.db, user_reads=1)
        u = data["data"]
        a = to_account(u)
        a.bio = u.get("description", "")
        pm = u.get("public_metrics") or {}
        a.followers, a.following, a.posts = pm.get("followers_count"), pm.get("following_count"), pm.get("tweet_count")
        return a

    async def author_feed(self, handle: str, cursor: str | None = None) -> Page:
        a = await self.profile(handle)
        data = await self._read(f"/2/users/{a.id}/tweets", 20, max_results=20, pagination_token=cursor)
        return Page(_parse(data, self.account_id), (data.get("meta") or {}).get("next_token"))

    async def post(self, text, images, reply_to=None, content_warning="", thread=False):
        if content_warning:
            text = f"CW: {content_warning}\n\n{text}"
        chunks = split_for_thread(text, self.char_limit) if thread else [text]
        n_link = sum(1 for c in chunks if URL_RE.search(c))
        check_budget(self.db, posts=len(chunks) - n_link, link_posts=n_link)
        media_ids = []
        for up in images[: self.max_images]:
            res = await self._req("POST", "/2/media/upload", files={"media": (up.filename, up.data, up.mime)},
                                  data={"media_category": "tweet_image"})
            mid = (res.get("data") or {}).get("id") or res.get("media_id_string")
            if up.alt and mid:
                await self._req("POST", "/2/media/metadata", json={"id": mid, "metadata": {"alt_text": {"text": up.alt[:1000]}}})
            media_ids.append(mid)
        results, parent = [], (reply_to.id if reply_to else None)
        for i, chunk in enumerate(chunks):
            body = {"text": chunk}
            if i == 0 and media_ids:
                body["media"] = {"media_ids": media_ids}
            if parent:
                body["reply"] = {"in_reply_to_tweet_id": parent}
            try:
                res = await self._req("POST", "/2/tweets", json=body)
            except XUnavailable as e:
                if results:  # part of the thread is already up, so handing it all over would post it twice
                    raise NetworkError(f"{e} Posted {len(results)} of {len(chunks)} parts.") from None
                raise
            record_usage(self.db, **({"link_posts": 1} if URL_RE.search(chunk) else {"posts": 1}))
            tid = res["data"]["id"]
            results.append(PostResult(id=tid, url=f"https://x.com/{self.rec.handle}/status/{tid}"))
            parent = tid
        return results

    async def like(self, post: Post, on: bool) -> None:
        if on:
            await self._req("POST", f"/2/users/{self.rec.native_id}/likes", json={"tweet_id": post.id})
        else:
            await self._req("DELETE", f"/2/users/{self.rec.native_id}/likes/{post.id}")

    async def repost(self, post: Post, on: bool) -> None:
        if on:
            await self._req("POST", f"/2/users/{self.rec.native_id}/retweets", json={"tweet_id": post.id})
        else:
            await self._req("DELETE", f"/2/users/{self.rec.native_id}/retweets/{post.id}")
