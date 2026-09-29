"""Durable capture, jobs, decisions, and request accounting."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit, urlunsplit

from .db import transaction


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        return None


def money_minor(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith((".local", ".internal", ".localhost")):
        return None
    try:
        if not ipaddress.ip_address(hostname).is_global:
            return None
    except ValueError:
        pass
    if len(value) > 2048:
        return None
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def identity(item: dict, source_id: str) -> tuple[str, str, str, str]:
    key = str(item.get("key") or "")
    rest_id = str(item.get("rest_item_id") or "")
    external = str(item.get("external_id") or item.get("id") or "").strip()
    sku = str(item.get("sku") or "").strip()
    if key.startswith("ebay:") or rest_id.startswith("v1|") or source_id.startswith("ebay") or source_id.startswith("endgame"):
        if not external and key.startswith("ebay:"):
            external = key.split(":", 1)[1]
        match = re.match(r"^v1\|([^|]+)\|([^|]*)", rest_id)
        variation = match.group(2) if match and match.group(2) != "0" else ""
        if match:
            external = match.group(1)
        if not external:
            raise ValueError("eBay record has no item ID")
        return "ebay", f"{external}|{variation}", external, variation
    if sku or source_id.startswith("oxfam"):
        sku = sku or external or key
        if not re.fullmatch(r"HD_\d+", sku):
            raise ValueError("Oxfam record has no stable SKU")
        return "oxfam", sku, sku, ""
    if key and ":" in key:
        platform, external_key = key.split(":", 1)
        if platform in {"shelter", "crisis"}:
            return platform, external_key, external_key, str(item.get("variant_id") or "")
        return platform, external_key, external_key, ""
    url = safe_url(item.get("url"))
    if url:
        return source_id, url, external or url, ""
    raise ValueError("Record lacks a stable listing identity")


def _price(item: dict) -> tuple[int | None, str]:
    currency = str(item.get("price_currency") or "").upper()
    if item.get("price_value") is not None:
        return money_minor(item["price_value"]), currency or "UNKNOWN"
    if item.get("observed_price_gbp") is not None:
        return money_minor(item["observed_price_gbp"]), "GBP"
    return money_minor(item.get("price_gbp")), "GBP" if item.get("price_gbp") is not None else "UNKNOWN"


def capture(
    db: sqlite3.Connection,
    item: dict,
    *,
    source_id: str,
    origin_key: str,
    imported: bool = False,
    baseline: bool = False,
    observed_at: str | None = None,
) -> int:
    """Capture one source record inside the caller's transaction."""
    platform, platform_key, external, variation = identity(item, source_id)
    title = str(item.get("title") or "").strip()
    if not title:
        raise ValueError("Listing title is missing")
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    material_keys = ("title", "description", "author", "publisher", "isbn", "edition", "condition", "price_gbp", "observed_price_gbp", "price_value", "price_currency", "shipping_value", "shipping_currency", "available", "item_end_date", "auction_end_at", "bid_count", "url", "vendor", "seller", "buying_options")
    material = {key: item[key] for key in material_keys if key in item}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()
    first_seen = stamp(item.get("first_seen") or item.get("item_creation_date") or item.get("published_at") or item.get("detected_at"))
    detected = stamp(observed_at) or stamp(item.get("last_seen") or item.get("observed_at")) or first_seen or (now() if not imported else None)
    url = safe_url(item.get("url"))
    seller = str(item.get("vendor") or item.get("seller") or item.get("store_name") or "")[:300]
    options = [str(x).upper() for x in item.get("buying_options", [])] if isinstance(item.get("buying_options"), list) else []
    end = stamp(item.get("item_end_date") or item.get("auction_end_at"))
    listing_type = "AUCTION" if "AUCTION" in options or end else "FIXED_PRICE"
    known_url = db.execute("SELECT listing_id FROM listing_aliases WHERE alias_type='url' AND alias=?", (url,)).fetchone() if url and platform != "ebay" and not variation else None
    if known_url:
        listing_id = int(known_url[0])
        previous = db.execute("SELECT imported,baseline_at FROM listings WHERE id=?", (listing_id,)).fetchone()
    else:
        previous = db.execute("SELECT imported,baseline_at FROM listings WHERE platform=? AND platform_key=?", (platform, platform_key)).fetchone()
        db.execute(
            "INSERT INTO listings(platform,platform_key,external_id,variation_id,seller,canonical_url,title,description,listing_type,first_seen_at,imported,baseline_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,platform_key) DO NOTHING",
            (platform, platform_key, external, variation, seller, url, title, str(item.get("description") or item.get("context") or "")[:5000], listing_type, first_seen, int(imported), now() if baseline else None),
        )
        listing_id = int(db.execute("SELECT id FROM listings WHERE platform=? AND platform_key=?", (platform, platform_key)).fetchone()[0])
    if baseline:
        db.execute("UPDATE listings SET baseline_at=COALESCE(baseline_at,?) WHERE id=?", (now(), listing_id))
    price, currency = _price(item)
    shipping = money_minor(item.get("shipping_value"))
    shipping_currency = str(item.get("shipping_currency") or "").upper() or None
    db.execute(
        "INSERT OR IGNORE INTO observations(listing_id,origin_key,observed_at,captured_at,price_minor,currency,shipping_minor,shipping_currency,auction_end_at,bid_count,available,content_hash,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (listing_id, origin_key, detected, now(), price, currency, shipping, shipping_currency, end, item.get("bid_count"), str(item.get("available")) if item.get("available") is not None else None, digest, raw),
    )
    observation = db.execute("SELECT id FROM observations WHERE origin_key=?", (origin_key,)).fetchone()
    current = db.execute("SELECT o.observed_at,o.id,o.content_hash FROM listings l LEFT JOIN observations o ON o.id=l.current_observation_id WHERE l.id=?", (listing_id,)).fetchone()
    if current["id"] is None or (detected or "", observation[0]) > (current["observed_at"] or "", current["id"]):
        material_changed = current["id"] is None or current["content_hash"] != digest
        fresh_claim = not imported and (previous is None or previous["baseline_at"] is None or material_changed)
        changed = material_changed or (fresh_claim and previous is not None and bool(previous["imported"]))
        db.execute(
            "UPDATE listings SET current_observation_id=?,canonical_url=COALESCE(?,canonical_url),title=?,description=?,seller=CASE WHEN ?!='' THEN ? ELSE seller END,listing_type=?,imported=CASE WHEN ? THEN 0 ELSE imported END,processing=CASE WHEN ? THEN 'CAPTURED' ELSE processing END,triage_score=CASE WHEN ? THEN NULL ELSE triage_score END,triage_reason=CASE WHEN ? THEN NULL ELSE triage_reason END,photographer=CASE WHEN ? THEN NULL ELSE photographer END,core_tier=CASE WHEN ? THEN NULL ELSE core_tier END,verdict=CASE WHEN ? THEN NULL ELSE verdict END WHERE id=?",
            (observation[0], url, title, str(item.get("description") or "")[:5000], seller, seller, listing_type, int(fresh_claim), int(changed), int(changed), int(changed), int(changed), int(changed), int(changed), listing_id),
        )
    for alias_type, alias in (("source_key", key := str(item.get("key") or "")), ("rest_item_id", rest_id := str(item.get("rest_item_id") or "")), ("url", url or "")):
        if alias:
            db.execute("INSERT OR IGNORE INTO listing_aliases(listing_id,alias_type,alias,source_id,provenance) VALUES(?,?,?,?,?)", (listing_id, alias_type, alias, source_id, origin_key))
    return listing_id


def capture_page(db: sqlite3.Connection, *, source_id: str, route_id: str, window_id: str, items: list[dict], page_number: int, continuation: object, complete: bool, frozen_end: str | None = None, imported: bool = False, followup_job: dict | None = None, next_due_at: str | None = None, coverage_note: str | None = None, lease_job_id: int | None = None, lease_token: str | None = None) -> list[int]:
    """Commit page contents and search cursor atomically."""
    ids: list[int] = []
    with transaction(db):
        if lease_job_id is not None and not db.execute("SELECT 1 FROM jobs WHERE id=? AND lease_token=? AND status='RUNNING' AND lease_until>=?", (lease_job_id, lease_token, now())).fetchone():
            raise RuntimeError("Stale source lease; page capture rejected")
        db.execute("INSERT OR IGNORE INTO sources(id,adapter,status) VALUES(?,?,'MANUAL_ONLY')", (source_id, source_id))
        db.execute("INSERT OR IGNORE INTO source_routes(id,source_id) VALUES(?,?)", (route_id, source_id))
        db.execute("INSERT OR IGNORE INTO search_windows(id,route_id,continuation_json) VALUES(?,?,'{}')", (window_id, route_id))
        progress = db.execute("SELECT last_durable_page,complete,route_id FROM search_windows WHERE id=?", (window_id,)).fetchone()
        if progress["route_id"] != route_id:
            raise ValueError("Search window belongs to another route")
        if page_number <= progress["last_durable_page"]:
            return []
        if progress["complete"] or page_number != progress["last_durable_page"] + 1:
            raise ValueError("Search page is out of order or window is complete")
        fresh_epoch = db.execute("SELECT value FROM health WHERE key='fresh_start_at'").fetchone()
        cutoff = fresh_epoch[0] if fresh_epoch else None
        for index, item in enumerate(items):
            digest = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
            origin = f"page:{window_id}:{page_number}:{index}:{digest}"
            first_seen = stamp(item.get("first_seen") or item.get("item_creation_date") or item.get("published_at") or item.get("detected_at"))
            baseline_item = imported and not (cutoff and first_seen and first_seen > cutoff)
            ids.append(capture(db, item, source_id=source_id, origin_key=origin, imported=baseline_item, baseline=baseline_item))
        db.execute("UPDATE search_windows SET continuation_json=?,complete=?,last_durable_page=?,frozen_end=COALESCE(frozen_end,?),incomplete_reason=? WHERE id=?", (json.dumps(continuation), int(complete), page_number, frozen_end, coverage_note if complete else "continuation pending", window_id))
        if complete:
            db.execute("UPDATE source_routes SET completed_through=?,incomplete_reason=?,last_success_at=?,next_due_at=COALESCE(?,next_due_at) WHERE id=?", (frozen_end, coverage_note, now(), next_due_at, route_id))
        else:
            db.execute("UPDATE source_routes SET incomplete_reason='continuation pending' WHERE id=?", (route_id,))
            if followup_job is not None:
                enqueue_job(db, **followup_job)
        for listing_id in ids:
            state = db.execute("SELECT current_observation_id,processing,imported FROM listings WHERE id=?", (listing_id,)).fetchone()
            if state["processing"] == "CAPTURED" and not state["imported"]:
                observation_id = state["current_observation_id"]
                enqueue_job(db, f"triage:{listing_id}:{observation_id}", "TRIAGE", listing_id=listing_id, priority=10, payload={"observation_id": observation_id})
    return ids


def enqueue_job(db: sqlite3.Connection, key: str, kind: str, *, listing_id: int | None = None, route_id: str | None = None, priority: int = 0, due_at: str | None = None, payload: dict | None = None) -> bool:
    cursor = db.execute("INSERT OR IGNORE INTO jobs(job_key,kind,listing_id,route_id,priority,due_at,payload_json) VALUES(?,?,?,?,?,?,?)", (key, kind, listing_id, route_id, priority, due_at or now(), json.dumps(payload or {})))
    return cursor.rowcount == 1


def claim_job(db: sqlite3.Connection, owner: str, *, kinds: tuple[str, ...] | None = None, lease_seconds: int = 120) -> sqlite3.Row | None:
    current = now()
    until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
    with transaction(db):
        db.execute("UPDATE jobs SET status='PENDING',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE status='RUNNING' AND lease_until<?", (current,))
        condition = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
        row = db.execute("SELECT * FROM jobs WHERE status='PENDING' AND due_at<=?" + condition + " ORDER BY priority DESC,due_at,id LIMIT 1", (current, *(kinds or ()))).fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        db.execute("UPDATE jobs SET status='RUNNING',lease_token=?,lease_owner=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, owner, until, row["id"]))
        return db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def finish_job(db: sqlite3.Connection, job_id: int, token: str, *, error: str | None = None, retry_seconds: int = 60) -> None:
    with transaction(db):
        if error:
            due = (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
            result = db.execute("UPDATE jobs SET status=CASE WHEN attempts>=5 THEN 'FAILED' ELSE 'PENDING' END,due_at=?,last_error=?,lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='RUNNING'", (due, error[:500], job_id, token))
        else:
            result = db.execute("UPDATE jobs SET status='DONE',lease_token=NULL,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='RUNNING'", (job_id, token))
        if result.rowcount != 1:
            raise RuntimeError("Stale worker lease: result rejected")


def decide(db: sqlite3.Connection, listing_id: int, action: str, note: str = "") -> int:
    if action not in {"SAVE", "WATCH", "DISMISS", "BOUGHT", "OWNED", "SNOOZE"}:
        raise ValueError("Unsupported decision")
    with transaction(db):
        if not db.execute("SELECT 1 FROM listings WHERE id=?", (listing_id,)).fetchone():
            raise ValueError("Unknown listing")
        cursor = db.execute("INSERT INTO user_decisions(listing_id,action,note,created_at) VALUES(?,?,?,?)", (listing_id, action, note[:1000], now()))
        if action in {"DISMISS", "BOUGHT", "OWNED"}:
            db.execute("UPDATE notification_events SET status='SUPPRESSED',suppression_reason=? WHERE listing_id=? AND status='QUEUED'", (action.lower(), listing_id))
        return int(cursor.lastrowid)


def undo_decision(db: sqlite3.Connection, decision_id: int) -> None:
    with transaction(db):
        db.execute("UPDATE user_decisions SET undone_at=? WHERE id=? AND undone_at IS NULL", (now(), decision_id))


def enqueue_notification(db: sqlite3.Connection, *, listing_id: int, stage: str, material_version: str, channel: str, payload: dict, expires_at: str | None = None) -> bool:
    key = f"{listing_id}:{stage}:{material_version}:{channel}"
    cursor = db.execute("INSERT OR IGNORE INTO notification_events(event_key,listing_id,stage,channel,payload_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?)", (key, listing_id, stage, channel, json.dumps(payload), now(), expires_at))
    return cursor.rowcount == 1


class QuotaDeferred(RuntimeError):
    """A metered request can run after the provider's current window resets."""

    def __init__(self, message: str, reset_at: str):
        super().__init__(message)
        reset = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
        self.retry_seconds = max(30, int((reset - datetime.now(timezone.utc)).total_seconds()) + 15)


def reserve_request(db: sqlite3.Connection, *, bucket: str, window_start: str, reset_at: str, provider_limit: int, reserve: int, lane_cap: int | None, route_id: str, reason: str, provider_remaining: int | None = None, provider_measured_at: str | None = None) -> int:
    """Reserve immediately before one physical attempt; a crash leaves uncertainty."""
    if not stamp(reset_at) or not stamp(window_start) or stamp(reset_at) <= now():
        raise ValueError("A current provider quota window is required")
    if provider_measured_at is not None and not stamp(provider_measured_at):
        raise ValueError("Invalid provider quota reading time")
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO api_windows(bucket,window_start,reset_at,provider_limit,provider_remaining,provider_measured_at) VALUES(?,?,?,?,?,?)", (bucket, window_start, reset_at, provider_limit, provider_remaining, provider_measured_at))
        row = db.execute("SELECT * FROM api_windows WHERE bucket=? AND window_start=?", (bucket, window_start)).fetchone()
        if row["reset_at"] != reset_at:
            raise RuntimeError("Quota window reset mismatch")
        used = row["consumed"] + row["reserved"] + row["uncertain"]
        remaining = row["provider_remaining"]
        if provider_remaining is not None:
            if provider_remaining < 0:
                raise ValueError("Provider remaining cannot be negative")
            if provider_measured_at is not None:
                if row["provider_measured_at"] is None or provider_measured_at > row["provider_measured_at"]:
                    # A newer account reading can correct a stale count just
                    # after reset. Reused cached readings cannot replenish
                    # calls already reserved by this worker.
                    remaining = min(provider_remaining, min(row["provider_limit"], provider_limit) - used)
                # An older or repeated reading must not overwrite reservations.
            else:
                remaining = min(remaining, provider_remaining) if remaining is not None else provider_remaining
        # The provider reading is a remaining balance, not an allowance to add
        # to our local usage. Decrement it at reservation time, including for
        # attempts whose response is lost or whose worker crashes.
        if used + 1 > min(row["provider_limit"], provider_limit) - reserve or (remaining is not None and remaining - 1 < reserve):
            raise QuotaDeferred("Shared API reserve protected", reset_at)
        if lane_cap is not None:
            if route_id.startswith(("endgame", "ebay-endgame:")):
                lane_used = db.execute("SELECT COUNT(*) FROM api_requests WHERE window_id=? AND (route_id LIKE 'endgame%' OR route_id LIKE 'ebay-endgame:%')", (row["id"],)).fetchone()[0]
            else:
                lane_used = db.execute("SELECT COUNT(*) FROM api_requests WHERE window_id=? AND route_id=?", (row["id"], route_id)).fetchone()[0]
            if lane_used + 1 > lane_cap:
                raise QuotaDeferred("Route API cap reached", reset_at)
        measured_at = provider_measured_at if provider_measured_at and (row["provider_measured_at"] is None or provider_measured_at > row["provider_measured_at"]) else row["provider_measured_at"]
        db.execute("UPDATE api_windows SET reserved=reserved+1,provider_remaining=?,provider_measured_at=? WHERE id=?", (remaining - 1 if remaining is not None else None, measured_at, row["id"]))
        request = db.execute("INSERT INTO api_requests(window_id,route_id,reason,reserved_at,status) VALUES(?,?,?,?,?)", (row["id"], route_id, reason, now(), "RESERVED"))
        return int(request.lastrowid)


def settle_request(db: sqlite3.Connection, request_id: int, *, response_class: str, uncertain: bool = False) -> None:
    with transaction(db):
        row = db.execute("SELECT window_id,status FROM api_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["status"] != "RESERVED":
            raise RuntimeError("Request reservation already settled or missing")
        if uncertain:
            db.execute("UPDATE api_windows SET reserved=reserved-1,uncertain=uncertain+1 WHERE id=?", (row["window_id"],))
        else:
            db.execute("UPDATE api_windows SET reserved=reserved-1,consumed=consumed+1 WHERE id=?", (row["window_id"],))
        db.execute("UPDATE api_requests SET status=?,finished_at=?,response_class=? WHERE id=?", ("UNCERTAIN" if uncertain else "CONSUMED", now(), response_class, request_id))
