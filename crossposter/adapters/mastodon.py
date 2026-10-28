"""Mastodon, and any server that speaks the Mastodon API (GoToSocial, Pleroma, Akkoma, Pixelfed...)."""
from __future__ import annotations

import asyncio
import secrets
import uuid
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from ..config import settings
from ..crypto import pkce_pair
from ..models import Account, FeedInfo, LinkCard, Media, Notification, Page, Post
from ..richtext import mastodon_segments, split_for_thread
from .base import AccountRecord, NetworkAdapter, NetworkError, PostResult, Upload, http, parse_time, save_account

SCOPES = "read write push"


def callback_url() -> str:
    return f"{settings.base_url}/auth/mastodon/callback"


def normalise_instance(value: str) -> str:
    """'mastodon.social', 'https://mastodon.social/', '@me@mastodon.social' -> 'mastodon.social'."""
    v = value.strip().lower()
    if v.count("@") >= 1 and "://" not in v:
        v = v.rsplit("@", 1)[-1]
    if "://" not in v:
        v = "https://" + v
    host = urlparse(v).netloc
    if not host:
        raise NetworkError("That doesn't look like a server name.")
    return host


async def _app_for(db, host: str) -> dict:
    key = f"{host}|{callback_url()}"
    row = db.one("select client_id, client_secret from mastodon_apps where instance=?", (key,))
    if row:
        return {"client_id": row["client_id"], "client_secret": row["client_secret"]}
    r = await http().post(f"https://{host}/api/v1/apps", data={
        "client_name": "Broadsheet", "redirect_uris": callback_url(),
        "scopes": SCOPES, "website": settings.base_url})
    if r.status_code >= 400:
        raise NetworkError(f"{host} refused to register the app ({r.status_code}). Is it a Mastodon-compatible server?")
    app = r.json()
    db.x("insert or replace into mastodon_apps values(?, ?, ?)", (key, app["client_id"], app["client_secret"]))
    if app.get("vapid_key"):
        db.set(f"mastodon_vapid|{host}", app["vapid_key"])
    return app


# Push straight from a Mastodon server to this browser, used by the browser build (see push routes in app.py).
PUSH_ALERTS = {"reply": ["mention"], "mention": ["mention"], "quote": ["quote"], "follow": ["follow", "follow_request"],
               "like": ["favourite"], "repost": ["reblog"]}


async def vapid_key(db, server: str) -> str:
    """The server's Web Push key, which the browser's subscription must be made with."""
    host = normalise_instance(server)
    key = db.get(f"mastodon_vapid|{host}")
    if not key:
        r = await http().get(f"https://{host}/api/v2/instance")
        if r.status_code == 200:
            key = ((r.json().get("configuration") or {}).get("vapid") or {}).get("public_key")
        if not key:
            raise NetworkError(f"{host} doesn't offer push notifications.")
        db.set(f"mastodon_vapid|{host}", key)
    return key


async def subscribe_push(rec: AccountRecord, sub: dict, kinds: list[str]) -> None:
    alerts = {alert: True for k in kinds for alert in PUSH_ALERTS.get(k, [])}
    r = await http().post(f"{rec.server}/api/v1/push/subscription",
                          headers={"Authorization": f"Bearer {rec.tokens['access_token']}"},
                          json={"subscription": {"endpoint": sub["endpoint"], "keys": sub["keys"], "standard": True},
                                "data": {"alerts": alerts, "policy": "all"}})
    if r.status_code >= 400:
        raise NetworkError(f"{rec.handle}: the server refused push notifications ({r.status_code}).")


async def start_login(db, instance: str) -> str:
    host = normalise_instance(instance)
    app = await _app_for(db, host)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    db.put_state(state, "mastodon", {"host": host, "verifier": verifier})
    return f"https://{host}/oauth/authorize?" + urlencode({
        "client_id": app["client_id"], "redirect_uri": callback_url(), "response_type": "code",
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


async def finish_login(db, code: str, state: str) -> AccountRecord:
    st = db.pop_state(state, "mastodon")
    if not st:
        raise NetworkError("The sign-in link expired. Please try again.")
    host = st["host"]
    app = await _app_for(db, host)
    r = await http().post(f"https://{host}/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": app["client_id"],
        "client_secret": app["client_secret"], "redirect_uri": callback_url(),
        "code_verifier": st["verifier"], "scope": SCOPES})
    if r.status_code >= 400:
        raise NetworkError(f"{host} didn't accept the sign-in ({r.status_code}).")
    token = r.json()["access_token"]
    me = await http().get(f"https://{host}/api/v1/accounts/verify_credentials",
                          headers={"Authorization": f"Bearer {token}"})
    me.raise_for_status()
    a = me.json()
    rec = AccountRecord(id=f"mastodon:{a['id']}@{host}", network="mastodon", native_id=a["id"],
                        handle=f"{a['username']}@{host}", display_name=a.get("display_name") or "",
                        avatar=a.get("avatar") or "", server=f"https://{host}", tokens={"access_token": token})
    save_account(db, rec)
    return rec


def _html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text("\n").strip()


def _full_acct(acct: str, host: str) -> str:
    return acct if "@" in acct else f"{acct}@{host}"


def _bio(a: dict) -> str:
    """The note plus profile fields, which is where people usually list their other accounts."""
    lines = [_html_to_text(a.get("note", ""))]
    for f in a.get("fields") or []:
        name, value = _html_to_text(f.get("name", "")), _html_to_text(f.get("value", "")).replace("\n", "")
        if name or value:
            lines.append(f"{name}: {value}")
    return "\n".join(line for line in lines if line)


def to_account(a: dict, host: str) -> Account:
    return Account(network="mastodon", id=str(a["id"]), handle=_full_acct(a.get("acct", a.get("username", "")), host),
                   display_name=a.get("display_name") or "", avatar=a.get("avatar") or "",
                   url=a.get("url") or "", bio=_bio(a),
                   followers=a.get("followers_count"), following=a.get("following_count"),
                   posts=a.get("statuses_count"))


def _media(m: dict) -> Media:
    kind = {"image": "image", "gifv": "gif", "video": "video", "audio": "audio"}.get(m.get("type"), "image")
    meta = (m.get("meta") or {}).get("original") or (m.get("meta") or {}).get("small") or {}
    return Media(kind=kind, url=m.get("url") or m.get("remote_url") or "", thumb=m.get("preview_url") or "",
                 alt=m.get("description") or "", width=meta.get("width"), height=meta.get("height"))


def to_post(s: dict, account_id: str, host: str) -> Post:
    if s.get("reblog"):
        p = to_post(s["reblog"], account_id, host)
        p.reposted_by = to_account(s["account"], host)
        p.extra["boost_id"] = s["id"]
        return p
    mentions = [{**m, "acct": _full_acct(m.get("acct", ""), host)} for m in s.get("mentions") or []]
    reply_handle = ""
    if s.get("in_reply_to_account_id"):
        for m in mentions:
            if str(m.get("id")) == str(s["in_reply_to_account_id"]):
                reply_handle = m["acct"]
    card = s.get("card")
    quote = None
    q = s.get("quote")
    if isinstance(q, dict) and isinstance(q.get("quoted_status"), dict):
        quote = to_post(q["quoted_status"], account_id, host)
    return Post(
        network="mastodon", id=str(s["id"]), account_id=account_id, author=to_account(s["account"], host),
        created_at=parse_time(s.get("created_at")), text=mastodon_segments(s.get("content", ""), mentions),
        url=s.get("url") or s.get("uri") or "", media=[_media(m) for m in s.get("media_attachments") or []],
        card=LinkCard(url=card["url"], title=card.get("title") or "", description=card.get("description") or "",
                      image=card.get("image") or "") if card else None,
        quote=quote, is_reply=bool(s.get("in_reply_to_id")), reply_to_handle=reply_handle,
        likes=s.get("favourites_count") or 0, reposts=s.get("reblogs_count") or 0,
        replies=s.get("replies_count") or 0, quotes=s.get("quotes_count") or 0,
        liked=bool(s.get("favourited")), reposted=bool(s.get("reblogged")),
        content_warning=s.get("spoiler_text") or "", sensitive=bool(s.get("sensitive")),
        lang=s.get("language") or "", extra={"instance": host, "uri": s.get("uri", "")})


NOTIF_KINDS = {"favourite": "like", "reblog": "repost", "follow": "follow", "quote": "quote",
               "mention": "mention", "status": "other", "poll": "other", "update": "other",
               "follow_request": "follow"}


class MastodonAdapter(NetworkAdapter):
    network = "mastodon"
    label = "Mastodon"
    char_limit = 500

    @property
    def host(self) -> str:
        return urlparse(self.rec.server).netloc

    async def _req(self, method: str, path: str, **kw):
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.rec.tokens['access_token']}"
        r = await http().request(method, f"{self.rec.server}{path}", headers=headers, **kw)
        if r.status_code == 401:
            raise NetworkError(f"{self.rec.handle} needs to sign in again.")
        if r.status_code >= 400:
            raise NetworkError(f"{self.host} returned {r.status_code} for {path}.")
        return r

    async def _get(self, path: str, **params):
        return await self._req("GET", path, params={k: v for k, v in params.items() if v is not None})

    async def max_chars(self) -> int:
        key = f"mastodon_max_chars:{self.host}"
        cached = self.db.get(key)
        if cached:
            return cached
        try:
            r = await self._get("/api/v2/instance")
            n = int(r.json()["configuration"]["statuses"]["max_characters"])
        except Exception:
            n = 500
        self.db.set(key, n)
        return n

    async def feeds(self) -> list[FeedInfo]:
        out = [FeedInfo("mastodon:home", "Home", self.account_id, pinned=True),
               FeedInfo("mastodon:local", f"Local ({self.host})", self.account_id),
               FeedInfo("mastodon:public", "Federated", self.account_id),
               FeedInfo("mastodon:trending", "Trending", self.account_id)]
        try:
            for lst in (await self._get("/api/v1/lists")).json():
                out.append(FeedInfo(f"mastodon:list:{lst['id']}", f"List: {lst['title']}", self.account_id))
            for tag in (await self._get("/api/v1/followed_tags")).json():
                out.append(FeedInfo(f"mastodon:tag:{tag['name']}", f"#{tag['name']}", self.account_id))
        except NetworkError:
            pass
        return out

    async def timeline(self, source: str, cursor: str | None = None, limit: int = 30) -> Page:
        kind, _, arg = source.removeprefix("mastodon:").partition(":")
        params = {"limit": limit, **(parse_qs(cursor) if cursor else {})}
        params = {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}
        path = {"home": "/api/v1/timelines/home", "public": "/api/v1/timelines/public",
                "local": "/api/v1/timelines/public", "trending": "/api/v1/trends/statuses",
                "list": f"/api/v1/timelines/list/{arg}", "tag": f"/api/v1/timelines/tag/{arg}"}.get(kind)
        if not path:
            raise NetworkError(f"Unknown Mastodon feed {source}")
        if kind == "local":
            params["local"] = "true"
        if kind == "trending":
            params["limit"] = min(limit, 40)
        r = await self._get(path, **params)
        posts = [to_post(s, self.account_id, self.host) for s in r.json()]
        if kind == "trending":
            offset = int(params.get("offset", 0)) + len(posts)
            nxt = urlencode({"offset": offset}) if posts else None
        else:
            link = r.links.get("next", {}).get("url")
            nxt = None
            if link:
                q = parse_qs(urlparse(link).query)
                nxt = urlencode({"max_id": q["max_id"][0]}) if "max_id" in q else None
        return Page(posts, nxt)

    async def notifications(self, limit: int = 40) -> list[Notification]:
        r = await self._get("/api/v1/notifications", limit=limit)
        out = []
        for n in r.json():
            kind = NOTIF_KINDS.get(n["type"], "other")
            status = n.get("status")
            post = to_post(status, self.account_id, self.host) if status else None
            if kind == "mention" and status and str(status.get("in_reply_to_account_id")) == str(self.rec.native_id):
                kind = "reply"
            out.append(Notification(network="mastodon", id=str(n["id"]), account_id=self.account_id, kind=kind,
                                    actor=to_account(n["account"], self.host), created_at=parse_time(n.get("created_at")),
                                    post=post))
        return out

    async def thread(self, post_id: str):
        s = (await self._get(f"/api/v1/statuses/{post_id}")).json()
        ctx = (await self._get(f"/api/v1/statuses/{post_id}/context")).json()
        conv = lambda x: to_post(x, self.account_id, self.host)
        return [conv(x) for x in ctx.get("ancestors", [])], conv(s), [conv(x) for x in ctx.get("descendants", [])]

    async def _lookup(self, handle: str) -> dict:
        handle = handle.lstrip("@")
        local = handle.removesuffix("@" + self.host)
        try:
            return (await self._get("/api/v1/accounts/lookup", acct=local)).json()
        except NetworkError:
            r = await self._get("/api/v2/search", q="@" + handle, resolve="true", type="accounts", limit=1)
            accts = r.json().get("accounts") or []
            if not accts:
                raise NetworkError(f"Couldn't find {handle} from {self.host}.")
            return accts[0]

    async def profile(self, handle: str) -> Account:
        return to_account(await self._lookup(handle), self.host)

    async def search_people(self, q: str, limit: int = 6) -> list[Account]:
        # Only ask the server to fetch remote accounts once a full user@server has been typed.
        full = "@" in q.lstrip("@") and "." in q.rsplit("@", 1)[-1]
        r = await self._get("/api/v1/accounts/search", q=q, limit=limit, resolve="true" if full else "false")
        return [to_account(a, self.host) for a in r.json()]

    async def author_feed(self, handle: str, cursor: str | None = None) -> Page:
        a = await self._lookup(handle)
        params = {"limit": 30, **({k: v[0] for k, v in parse_qs(cursor).items()} if cursor else {})}
        r = await self._get(f"/api/v1/accounts/{a['id']}/statuses", **params)
        posts = [to_post(s, self.account_id, self.host) for s in r.json()]
        link = r.links.get("next", {}).get("url")
        nxt = None
        if link:
            q = parse_qs(urlparse(link).query)
            nxt = urlencode({"max_id": q["max_id"][0]}) if "max_id" in q else None
        return Page(posts, nxt)

    async def _upload(self, up: Upload) -> str:
        r = await self._req("POST", "/api/v2/media", files={"file": (up.filename, up.data, up.mime)},
                            data={"description": up.alt} if up.alt else {})
        media = r.json()
        for _ in range(20):  # large files are processed asynchronously
            if media.get("url"):
                break
            await asyncio.sleep(0.5)
            media = (await self._get(f"/api/v1/media/{media['id']}")).json()
        return media["id"]

    async def post(self, text, images, reply_to=None, content_warning="", thread=False):
        limit = await self.max_chars()
        chunks = split_for_thread(text, limit) if thread else [text]
        media_ids = [await self._upload(u) for u in images[: self.max_images]]
        results: list[PostResult] = []
        parent = reply_to.id if reply_to else None
        for i, chunk in enumerate(chunks):
            body = {"status": chunk, "visibility": "public"}
            if i == 0 and media_ids:
                body["media_ids"] = media_ids
            if parent:
                body["in_reply_to_id"] = parent
            if content_warning:
                body["spoiler_text"] = content_warning
            r = await self._req("POST", "/api/v1/statuses", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
            s = r.json()
            results.append(PostResult(id=str(s["id"]), url=s.get("url") or ""))
            parent = s["id"]
        return results

    async def like(self, post: Post, on: bool) -> None:
        await self._req("POST", f"/api/v1/statuses/{post.id}/{'favourite' if on else 'unfavourite'}")

    async def repost(self, post: Post, on: bool) -> None:
        await self._req("POST", f"/api/v1/statuses/{post.id}/{'reblog' if on else 'unreblog'}")
