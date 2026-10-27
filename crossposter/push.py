"""Web Push to installed PWAs, and the background poller that decides what to push."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

from . import adapters as registry
from . import cache, feeds
from .config import settings
from .crypto import b64url
from .models import Notification

log = logging.getLogger(__name__)

DEFAULT_KINDS = ["reply", "mention", "quote", "follow"]
VERBS = {"reply": "replied to you", "mention": "mentioned you", "quote": "quoted you", "follow": "followed you",
         "like": "liked your post", "repost": "reposted your post", "other": "sent a notification"}


def vapid_keys(db) -> dict:
    keys = db.get("vapid")
    if not keys:
        k = ec.generate_private_key(ec.SECP256R1())
        keys = {"private": b64url(k.private_numbers().private_value.to_bytes(32, "big")),
                "public": b64url(k.public_key().public_bytes(serialization.Encoding.X962,
                                                             serialization.PublicFormat.UncompressedPoint))}
        db.set("vapid", keys)
    return keys


def notify_kinds(db) -> list[str]:
    return db.get("notify_kinds", DEFAULT_KINDS)


def add_subscription(db, sub: dict) -> None:
    db.x("insert or replace into push_subs values(?, ?, ?)", (sub["endpoint"], json.dumps(sub), time.time()))


def remove_subscription(db, endpoint: str) -> None:
    db.x("delete from push_subs where endpoint=?", (endpoint,))


def _send_one(db, sub: dict, payload: str) -> bool:
    keys = vapid_keys(db)
    try:
        webpush(sub, data=payload, vapid_private_key=Vapid01.from_raw(keys["private"].encode()),
                vapid_claims={"sub": f"mailto:{settings.contact_email}"}, ttl=3600, timeout=10)
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # the browser dropped this subscription
            remove_subscription(db, sub["endpoint"])
        log.warning("push failed (%s): %s", status, e)
        return False


async def send(db, title: str, body: str, url: str, tag: str = "", icon: str = "") -> int:
    """Send to every subscribed device. Uses the declarative Web Push format Safari understands
    natively; the service worker unpacks the same JSON on other browsers."""
    payload = json.dumps({"web_push": 8030, "notification": {
        "title": title, "body": body[:300], "navigate": settings.base_url + url, "tag": tag or url,
        "icon": icon or "/static/icons/icon-192.png", "app_badge": "1"}})
    sent = 0
    for row in db.q("select data from push_subs"):
        if await asyncio.to_thread(_send_one, db, json.loads(row["data"]), payload):
            sent += 1
    return sent


def notification_url(n: Notification) -> str:
    if n.post and n.kind in ("reply", "mention", "quote"):
        return "/thread?key=" + quote(n.post.key, safe="")
    if n.kind == "follow":
        return f"/profile/{n.network}/{quote(n.actor.handle)}?acct={quote(n.account_id, safe='')}"
    return "/notifications"


def store_notifications(db, items: list[Notification]) -> list[Notification]:
    """Save notifications and return the ones not seen before."""
    new = []
    for n in items:
        if db.one("select 1 from notifications where key=?", (n.key,)):
            continue
        db.x("insert into notifications(key, account_id, network, created_at, data, pushed) values(?, ?, ?, ?, ?, 0)",
             (n.key, n.account_id, n.network, n.created_at, json.dumps(n.to_dict())))
        new.append(n)
        if n.post:
            cache.save_posts(db, [n.post])
    return new


async def poll_once(db) -> int:
    pushed = 0
    kinds = set(notify_kinds(db))
    for a in registry.accounts(db):
        if a.network == "x":
            from .adapters.x import reading_enabled
            if not reading_enabled(db) or time.time() - (db.get(f"xpoll:{a.account_id}") or 0) < 900:
                continue
            db.set(f"xpoll:{a.account_id}", time.time())
        try:
            items = await a.notifications()
        except Exception as e:
            log.warning("notifications for %s failed: %s", a.account_id, e)
            continue
        first_sync = not db.get(f"notif_synced:{a.account_id}")
        new = store_notifications(db, items)
        db.set(f"notif_synced:{a.account_id}", True)
        if first_sync:
            continue  # don't push a backlog the first time an account is seen
        for n in sorted(new, key=lambda n: n.created_at):
            if n.kind not in kinds or time.time() - n.created_at > 3600:
                continue
            body = n.post.plain_text if n.post else n.actor.at_handle
            label = registry.NETWORK_LABELS.get(n.network, n.network)
            pushed += await send(db, f"{n.actor.name} {VERBS.get(n.kind, '')}", f"{label}: {body}",
                                 notification_url(n), tag=n.key, icon=n.actor.avatar)
            db.x("update notifications set pushed=1 where key=?", (n.key,))
    return pushed


async def poller(db) -> None:
    """Runs for the life of the server: notifications every POLL_SECONDS, feed sources every 5 minutes."""
    last_feeds = 0.0
    while True:
        try:
            await poll_once(db)
            if time.time() - last_feeds > cache.FETCH_EVERY:
                last_feeds = time.time()
                await cache.refresh(db, registry.accounts(db), feeds.all_sources())
                cache.prune(db)
        except Exception:
            log.exception("poller loop failed")
        await asyncio.sleep(settings.poll_seconds)
