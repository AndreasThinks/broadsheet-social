"""Network adapters. Adding a network means one class here; nothing else changes."""
from __future__ import annotations

from ..config import settings
from .base import AccountRecord, NetworkAdapter, NetworkError, PostResult, Upload
from .bluesky import BlueskyAdapter
from .demo import DemoAdapter
from .mastodon import MastodonAdapter
from .x import XAdapter

ADAPTERS: dict[str, type[NetworkAdapter]] = {
    "bluesky": BlueskyAdapter,
    "mastodon": MastodonAdapter,
    "x": XAdapter,
}

NETWORK_LABELS = {"bluesky": "Bluesky", "mastodon": "Mastodon", "x": "X"}


def accounts(db) -> list[NetworkAdapter]:
    out: list[NetworkAdapter] = []
    for row in db.q("select * from accounts order by created_at"):
        cls = ADAPTERS.get(row["network"])
        if cls is None or (row["network"] == "x" and not settings.x_enabled):
            continue
        out.append(cls(AccountRecord.from_row(row), db))
    if settings.demo:
        out += [DemoAdapter("bluesky", db), DemoAdapter("mastodon", db)]
    return out


def account(db, account_id: str) -> NetworkAdapter | None:
    return next((a for a in accounts(db) if a.account_id == account_id), None)


def for_network(db, network: str) -> NetworkAdapter | None:
    return next((a for a in accounts(db) if a.network == network), None)


__all__ = ["ADAPTERS", "NETWORK_LABELS", "AccountRecord", "NetworkAdapter", "NetworkError", "PostResult", "Upload",
           "accounts", "account", "for_network"]
