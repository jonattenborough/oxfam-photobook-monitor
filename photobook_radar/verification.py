"""Exact eBay listing check, isolated from fast lead delivery and AI review."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Callable
from urllib.parse import urlsplit

from ebay_api import MARKETPLACE_DOMAINS, listing_from_summary

from .config import Config
from .db import transaction
from .ebay_gateway import thread_client
from .store import finish_job, money_minor, now, safe_url


def _live_ebay(db: sqlite3.Connection, config: Config, item_id: str) -> tuple[bool, str, dict]:
    client = thread_client(db, config, "ebay-verification")
    return client.live_status(item_id)


def _finish_locked(db: sqlite3.Connection, job: sqlite3.Row) -> None:
    result = db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='RUNNING'", (job["id"], job["lease_token"]))
    if result.rowcount != 1:
        raise RuntimeError("Stale verification lease")


def run_verify(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, lookup: Callable[[sqlite3.Connection, Config, str], tuple[bool, str, dict]] = _live_ebay) -> dict:
    """A stale job does no network work. The result and lease completion commit together."""
    if not (config.production and config.allow_marketplace_network):
        raise RuntimeError("Exact live checks require production marketplace permission")
    payload = json.loads(job["payload_json"])
    observation_id = payload.get("observation_id")
    row = db.execute("SELECT l.id,l.platform,l.external_id,l.current_observation_id,l.canonical_url,o.raw_json FROM listings l JOIN observations o ON o.id=l.current_observation_id WHERE l.id=?", (job["listing_id"],)).fetchone()
    if row is None:
        raise ValueError("Verification listing has no observation")
    if row["current_observation_id"] != observation_id:
        finish_job(db, job["id"], job["lease_token"])
        return {"stale_observation": True}
    if row["platform"] != "ebay":
        # Other adapters need their own exact seller-page implementation.
        with transaction(db):
            db.execute("INSERT INTO health(key,value,updated_at) VALUES('verification_unsupported',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (row["platform"], now()))
            _finish_locked(db, job)
        return {"unsupported_source": row["platform"]}
    original = json.loads(row["raw_json"])
    item_id = str(original.get("rest_item_id") or row["external_id"])
    if not re.fullmatch(r"(?:v1\|)?[0-9]{9,15}(?:\|[^|]*)?", item_id):
        raise ValueError("Invalid eBay item ID for exact check")
    live, reason, detail = lookup(db, config, item_id)
    if not isinstance(detail, dict):
        raise ValueError("eBay detail response is not an object")
    returned_id = str(detail.get("itemId") or "")
    if returned_id != item_id and not (returned_id.startswith("v1|") and returned_id.split("|")[1] == row["external_id"]):
        raise ValueError("eBay detail response does not match the requested listing")
    mapped = listing_from_summary(detail, {"id": "ebay-verification", "name": "eBay exact listing", "marketplace": "EBAY_GB"})
    if mapped is None:
        raise ValueError("eBay detail response lacks identity or title")
    url = safe_url(mapped.get("url"))
    if url and urlsplit(url).hostname not in set(MARKETPLACE_DOMAINS.values()):
        raise ValueError("Unexpected seller URL in eBay detail response")
    availability = "LIVE" if live else "ENDED" if reason == "listing ended" else "UNAVAILABLE"
    checked_at = now()
    result = {"reason": reason, "item_id": returned_id, "title": mapped["title"], "buying_options": mapped.get("buying_options"), "item_end_date": mapped.get("item_end_date"), "estimated_availability": detail.get("estimatedAvailabilityStatus")}
    with transaction(db):
        owned = db.execute("SELECT 1 FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING'", (job["id"], job["lease_token"])).fetchone()
        current = db.execute("SELECT current_observation_id FROM listings WHERE id=?", (job["listing_id"],)).fetchone()
        if not owned:
            raise RuntimeError("Stale verification lease")
        if not current or current[0] != observation_id:
            _finish_locked(db, job)
            return {"stale_observation": True}
        db.execute("INSERT INTO live_checks(listing_id,observation_id,provider,checked_at,availability,price_minor,currency,shipping_minor,shipping_currency,seller_url,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(listing_id,observation_id,provider) DO UPDATE SET checked_at=excluded.checked_at,availability=excluded.availability,price_minor=excluded.price_minor,currency=excluded.currency,shipping_minor=excluded.shipping_minor,shipping_currency=excluded.shipping_currency,seller_url=excluded.seller_url,result_json=excluded.result_json", (job["listing_id"], observation_id, "ebay_browse", checked_at, availability, money_minor(mapped.get("price_value")), mapped.get("price_currency"), money_minor(mapped.get("shipping_value")), mapped.get("shipping_currency"), url, json.dumps(result, sort_keys=True)))
        db.execute("UPDATE listings SET availability=? WHERE id=?", (availability, job["listing_id"]))
        if availability != "LIVE":
            db.execute("UPDATE notification_events SET status='SUPPRESSED',suppression_reason='exact listing check says unavailable' WHERE listing_id=? AND status='QUEUED'", (job["listing_id"],))
        _finish_locked(db, job)
    return {"availability": availability, "checked_at": checked_at, "price_minor": money_minor(mapped.get("price_value"))}
