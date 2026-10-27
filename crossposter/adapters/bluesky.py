"""Bluesky and anything else on the AT Protocol, talking XRPC to your PDS with DPoP-bound tokens."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from joserfc.jwk import ECKey

from ..media import parse_og, shrink_image
from ..models import Account, FeedInfo, LinkCard, Media, Notification, Page, Post
from ..richtext import MENTION_RE, URL_RE, bsky_segments, build_bsky_facets, split_for_thread
from . import bluesky_oauth as oauth
from .base import AccountRecord, NetworkAdapter, NetworkError, PostResult, Upload, http, parse_time, save_account

DISCOVER = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
MAX_BLOB = 976_560  # Bluesky's image size limit in bytes
SENSITIVE_LABELS = {"porn", "sexual", "nudity", "graphic-media", "gore"}

_locks: dict[str, asyncio.Lock] = {}


async def finish_login(db, code: str, state: str, iss: str) -> AccountRecord:
    data = await oauth.finish_login(db, code, state, iss)
    rec = AccountRecord(id=f"bluesky:{data['did']}", network="bluesky", native_id=data["did"],
                        handle=data["handle"], display_name="", avatar="", server=data["pds"], tokens=data["tokens"])
    save_account(db, rec)
    adapter = BlueskyAdapter(rec, db)
    try:
        prof = await adapter.profile(data["did"])
        rec.handle, rec.display_name, rec.avatar = prof.handle, prof.display_name, prof.avatar
        save_account(db, rec)
    except NetworkError:
        pass
    return rec


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def rkey(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def to_account(p: dict) -> Account:
    handle = p.get("handle", "")
    return Account(network="bluesky", id=p.get("did", ""), handle=handle, display_name=p.get("displayName") or "",
                   avatar=p.get("avatar") or "", url=f"https://bsky.app/profile/{handle or p.get('did', '')}",
                   bio=p.get("description") or "", followers=p.get("followersCount"),
                   following=p.get("followsCount"), posts=p.get("postsCount"))


def _media_from_embed(embed: dict) -> tuple[list[Media], LinkCard | None]:
    t = embed.get("$type", "")
    if t.startswith("app.bsky.embed.images"):
        out = []
        for im in embed.get("images") or []:
            ar = im.get("aspectRatio") or {}
            out.append(Media("image", url=im.get("fullsize", ""), thumb=im.get("thumb", ""), alt=im.get("alt") or "",
                             width=ar.get("width"), height=ar.get("height")))
        return out, None
    if t.startswith("app.bsky.embed.video"):
        ar = embed.get("aspectRatio") or {}
        return [Media("video", url=embed.get("playlist", ""), stream=embed.get("playlist", ""),
                      thumb=embed.get("thumbnail") or "", alt=embed.get("alt") or "",
                      width=ar.get("width"), height=ar.get("height"))], None
    if t.startswith("app.bsky.embed.external"):
        ex = embed.get("external") or {}
        return [], LinkCard(url=ex.get("uri", ""), title=ex.get("title") or "", description=ex.get("description") or "",
                            image=ex.get("thumb") or "")
    return [], None


def _quote_from_record(rec: dict, account_id: str) -> Post | None:
    if not rec.get("$type", "").endswith("#viewRecord"):
        return None
    value = rec.get("value") or {}
    media, card = [], None
    for e in rec.get("embeds") or []:
        m, c = _media_from_embed(e)
        media += m
        card = card or c
        if e.get("$type", "").startswith("app.bsky.embed.recordWithMedia"):
            m, c = _media_from_embed(e.get("media") or {})
            media += m
    author = to_account(rec.get("author") or {})
    return Post(network="bluesky", id=rec["uri"], account_id=account_id, author=author,
                created_at=parse_time(value.get("createdAt")),
                text=bsky_segments(value.get("text", ""), value.get("facets")),
                url=f"https://bsky.app/profile/{author.handle}/post/{rkey(rec['uri'])}", media=media, card=card,
                likes=rec.get("likeCount") or 0, reposts=rec.get("repostCount") or 0,
                replies=rec.get("replyCount") or 0, quotes=rec.get("quoteCount") or 0,
                extra={"uri": rec["uri"], "cid": rec.get("cid")})


def to_post(pv: dict, account_id: str, reason: dict | None = None, reply: dict | None = None) -> Post:
    record = pv.get("record") or {}
    author = to_account(pv.get("author") or {})
    media, card, quote = [], None, None
    embed = pv.get("embed") or {}
    t = embed.get("$type", "")
    if t.startswith("app.bsky.embed.recordWithMedia"):
        media, card = _media_from_embed(embed.get("media") or {})
        quote = _quote_from_record((embed.get("record") or {}).get("record") or {}, account_id)
    elif t.startswith("app.bsky.embed.record"):
        quote = _quote_from_record(embed.get("record") or {}, account_id)
    elif embed:
        media, card = _media_from_embed(embed)
    labels = {lab.get("val") for lab in pv.get("labels") or []}
    sensitive = sorted(labels & SENSITIVE_LABELS)
    viewer = pv.get("viewer") or {}
    rep = record.get("reply") or {}
    reply_handle = ""
    if reply and isinstance(reply.get("parent"), dict):
        reply_handle = ((reply["parent"].get("author") or {}).get("handle")) or ""
    langs = record.get("langs") or []
    return Post(
        network="bluesky", id=pv["uri"], account_id=account_id, author=author,
        created_at=parse_time(record.get("createdAt") or pv.get("indexedAt")),
        text=bsky_segments(record.get("text", ""), record.get("facets")),
        url=f"https://bsky.app/profile/{author.handle}/post/{rkey(pv['uri'])}", media=media, card=card, quote=quote,
        reposted_by=to_account(reason["by"]) if reason and reason.get("$type", "").endswith("reasonRepost") else None,
        is_reply=bool(rep), reply_to_handle=reply_handle,
        likes=pv.get("likeCount") or 0, reposts=pv.get("repostCount") or 0,
        replies=pv.get("replyCount") or 0, quotes=pv.get("quoteCount") or 0,
        liked=bool(viewer.get("like")), reposted=bool(viewer.get("repost")),
        content_warning=", ".join(sensitive), sensitive=bool(sensitive), lang=langs[0] if langs else "",
        extra={"uri": pv["uri"], "cid": pv.get("cid"), "like_uri": viewer.get("like"),
               "repost_uri": viewer.get("repost"), "root": rep.get("root"), "parent": rep.get("parent")})


NOTIF_KINDS = {"like": "like", "repost": "repost", "follow": "follow", "mention": "mention",
               "reply": "reply", "quote": "quote"}


class BlueskyAdapter(NetworkAdapter):
    network = "bluesky"
    label = "Bluesky"
    char_limit = 300

    @property
    def did(self) -> str:
        return self.rec.native_id

    async def _fresh_tokens(self) -> dict:
        lock = _locks.setdefault(self.account_id, asyncio.Lock())
        async with lock:
            if self.rec.tokens.get("expires_at", 0) - 60 < time.time():
                self.save_tokens(await oauth.refresh(self.db, self.rec.tokens))
        return self.rec.tokens

    async def _req(self, method: str, nsid: str, params=None, json=None, content=None, headers=None, _retry=True):
        tokens = await self._fresh_tokens()
        key = ECKey.import_key(tokens["dpop_jwk"])
        url = f"{self.rec.server}/xrpc/{nsid}"
        nonce = tokens.get("pds_nonce")
        for attempt in range(2):
            h = dict(headers or {})
            h["Authorization"] = f"DPoP {tokens['access_token']}"
            h["DPoP"] = oauth.dpop_proof(key, method, url, nonce, tokens["access_token"])
            r = await http().request(method, url, params=params, json=json, content=content, headers=h)
            new_nonce = r.headers.get("DPoP-Nonce")
            if new_nonce and new_nonce != nonce:
                nonce = new_nonce
                self.save_tokens({**self.rec.tokens, "pds_nonce": nonce})
            if r.status_code == 401 and "use_dpop_nonce" in r.headers.get("WWW-Authenticate", ""):
                continue
            break
        if r.status_code == 401 and _retry:
            self.save_tokens({**self.rec.tokens, "expires_at": 0})
            return await self._req(method, nsid, params, json, content, headers, _retry=False)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.json().get("error")
            except Exception:
                msg = r.text[:200]
            raise NetworkError(f"Bluesky: {msg} ({nsid})")
        return r.json() if r.content else {}

    async def _get(self, nsid: str, **params):
        return await self._req("GET", nsid, params={k: v for k, v in params.items() if v is not None})

    async def feeds(self) -> list[FeedInfo]:
        out = [FeedInfo("bluesky:following", "Following", self.account_id, pinned=True),
               FeedInfo("bluesky:discover", "Discover", self.account_id)]
        try:
            prefs = (await self._get("app.bsky.actor.getPreferences")).get("preferences", [])
        except NetworkError:
            return out
        saved = []
        for p in prefs:
            if p.get("$type") == "app.bsky.actor.defs#savedFeedsPrefV2":
                saved = p.get("items") or []
        feed_uris = [i["value"] for i in saved if i.get("type") == "feed" and i["value"] != DISCOVER]
        names = {}
        if feed_uris:
            try:
                gens = await self._get("app.bsky.feed.getFeedGenerators", feeds=feed_uris[:50])
                names = {g["uri"]: g.get("displayName") or "Feed" for g in gens.get("feeds", [])}
            except NetworkError:
                pass
        for item in saved:
            if item.get("type") == "feed" and item["value"] != DISCOVER:
                out.append(FeedInfo(f"bluesky:feed:{item['value']}", names.get(item["value"], "Feed"),
                                    self.account_id, pinned=bool(item.get("pinned"))))
            elif item.get("type") == "list":
                out.append(FeedInfo(f"bluesky:list:{item['value']}", "List", self.account_id,
                                    pinned=bool(item.get("pinned"))))
        return out

    async def timeline(self, source: str, cursor: str | None = None, limit: int = 30) -> Page:
        kind, _, arg = source.removeprefix("bluesky:").partition(":")
        if kind == "following":
            data = await self._get("app.bsky.feed.getTimeline", limit=limit, cursor=cursor)
        elif kind in ("discover", "feed"):
            data = await self._get("app.bsky.feed.getFeed", feed=arg or DISCOVER, limit=limit, cursor=cursor)
        elif kind == "list":
            data = await self._get("app.bsky.feed.getListFeed", list=arg, limit=limit, cursor=cursor)
        else:
            raise NetworkError(f"Unknown Bluesky feed {source}")
        posts = [to_post(i["post"], self.account_id, i.get("reason"), i.get("reply")) for i in data.get("feed", [])]
        return Page(posts, data.get("cursor"))

    async def _posts(self, uris: list[str]) -> dict[str, Post]:
        out = {}
        uris = list(dict.fromkeys(u for u in uris if u))
        for i in range(0, len(uris), 25):
            data = await self._get("app.bsky.feed.getPosts", uris=uris[i:i + 25])
            for pv in data.get("posts", []):
                out[pv["uri"]] = to_post(pv, self.account_id)
        return out

    async def notifications(self, limit: int = 40) -> list[Notification]:
        data = await self._get("app.bsky.notification.listNotifications", limit=limit)
        items = data.get("notifications", [])
        wanted = []
        for n in items:
            if n["reason"] in ("reply", "mention", "quote"):
                wanted.append(n["uri"])
            elif n.get("reasonSubject"):
                wanted.append(n["reasonSubject"])
        posts = await self._posts(wanted)
        out = []
        for n in items:
            subject = n["uri"] if n["reason"] in ("reply", "mention", "quote") else n.get("reasonSubject")
            out.append(Notification(network="bluesky", id=f"{n['uri']}#{n['reason']}", account_id=self.account_id,
                                    kind=NOTIF_KINDS.get(n["reason"], "other"), actor=to_account(n["author"]),
                                    created_at=parse_time(n.get("indexedAt")), post=posts.get(subject)))
        return out

    async def thread(self, post_id: str):
        data = await self._get("app.bsky.feed.getPostThread", uri=post_id, depth=6, parentHeight=30)
        node = data.get("thread") or {}
        if "post" not in node:
            raise NetworkError("That post was deleted or isn't visible.")
        ancestors = []
        parent = node.get("parent")
        while parent and "post" in parent:
            ancestors.insert(0, to_post(parent["post"], self.account_id))
            parent = parent.get("parent")
        replies: list[Post] = []

        def walk(n):
            for child in sorted(n.get("replies") or [], key=lambda c: (c.get("post") or {}).get("indexedAt", "")):
                if "post" in child:
                    replies.append(to_post(child["post"], self.account_id))
                    walk(child)
        walk(node)
        return ancestors, to_post(node["post"], self.account_id), replies

    async def profile(self, handle: str) -> Account:
        return to_account(await self._get("app.bsky.actor.getProfile", actor=handle.lstrip("@")))

    async def author_feed(self, handle: str, cursor: str | None = None) -> Page:
        data = await self._get("app.bsky.feed.getAuthorFeed", actor=handle.lstrip("@"), limit=30, cursor=cursor)
        return Page([to_post(i["post"], self.account_id, i.get("reason"), i.get("reply")) for i in data.get("feed", [])],
                    data.get("cursor"))

    # ---------- writing ----------

    async def _blob(self, data: bytes, mime: str) -> dict:
        r = await self._req("POST", "com.atproto.repo.uploadBlob", content=data, headers={"Content-Type": mime})
        return r["blob"]

    async def _link_card(self, url: str) -> dict | None:
        try:
            r = await http().get(url, headers={"Accept": "text/html"})
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return None
            og = parse_og(r.text)
        except Exception:
            return None
        external = {"uri": url, "title": og["title"][:300], "description": og["description"][:1000]}
        if og["image"]:
            try:
                img = await http().get(urljoin(str(r.url), og["image"]))
                if img.status_code == 200:
                    data, mime, _ = shrink_image(img.content, img.headers.get("content-type", "image/jpeg"), MAX_BLOB, 1200)
                    external["thumb"] = await self._blob(data, mime)
            except Exception:
                pass
        return {"$type": "app.bsky.embed.external", "external": external}

    async def _facets(self, text: str) -> list[dict]:
        handles = {m.group(1) for m in MENTION_RE.finditer(text)}
        dids = {}
        for h in handles:
            dids[h] = await oauth.resolve_handle(h)
        return build_bsky_facets(text, lambda h: dids.get(h))

    async def post(self, text, images, reply_to=None, content_warning="", thread=False):
        if content_warning:
            text = f"CW: {content_warning}\n\n{text}"
        chunks = split_for_thread(text, self.char_limit) if thread else [text]
        embed = None
        if images:
            ims = []
            for up in images[: self.max_images]:
                data, mime, (w, h) = shrink_image(up.data, up.mime, MAX_BLOB)
                ims.append({"image": await self._blob(data, mime), "alt": up.alt or "",
                            "aspectRatio": {"width": w, "height": h}})
            embed = {"$type": "app.bsky.embed.images", "images": ims}
        else:
            link = URL_RE.search(chunks[0])
            if link:
                embed = await self._link_card(link.group(0))
        root = parent = None
        if reply_to:
            parent = {"uri": reply_to.extra["uri"], "cid": reply_to.extra["cid"]}
            root = reply_to.extra.get("root") or parent
        results = []
        for i, chunk in enumerate(chunks):
            record = {"$type": "app.bsky.feed.post", "text": chunk, "createdAt": now_iso(),
                      "facets": await self._facets(chunk)}
            if i == 0 and embed:
                record["embed"] = embed
            if parent:
                record["reply"] = {"root": root, "parent": parent}
            res = await self._req("POST", "com.atproto.repo.createRecord",
                                  json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record})
            ref = {"uri": res["uri"], "cid": res["cid"]}
            root = root or ref
            parent = ref
            results.append(PostResult(id=res["uri"], url=f"https://bsky.app/profile/{self.rec.handle}/post/{rkey(res['uri'])}",
                                      extra=ref))
        return results

    async def _toggle(self, collection: str, post: Post, on: bool, existing: str | None) -> str | None:
        if on:
            res = await self._req("POST", "com.atproto.repo.createRecord", json={
                "repo": self.did, "collection": collection,
                "record": {"$type": collection, "subject": {"uri": post.extra["uri"], "cid": post.extra["cid"]},
                           "createdAt": now_iso()}})
            return res.get("uri")
        if existing:
            await self._req("POST", "com.atproto.repo.deleteRecord",
                            json={"repo": self.did, "collection": collection, "rkey": rkey(existing)})
        return None

    async def like(self, post: Post, on: bool) -> None:
        post.extra["like_uri"] = await self._toggle("app.bsky.feed.like", post, on, post.extra.get("like_uri"))

    async def repost(self, post: Post, on: bool) -> None:
        post.extra["repost_uri"] = await self._toggle("app.bsky.feed.repost", post, on, post.extra.get("repost_uri"))
