"""Validate and durably capture one eBay Browse search page."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from ebay_api import MARKETPLACE_DOMAINS, SEARCH_URL, listing_from_summary

from ..store import capture_page, now


@dataclass(frozen=True)
class BrowsePage:
    items: list[dict]
    total: int
    next_url: str | None
    complete: bool


def parse_browse_page(payload: dict, source: dict) -> BrowsePage:
    rows = payload.get("itemSummaries")
    total = payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
        raise ValueError("eBay Browse search envelope is invalid")
    if (total > 0 and not rows) or len(rows) > 200:
        raise ValueError("eBay Browse result count is inconsistent")
    next_url = payload.get("next")
    if next_url is not None:
        parsed = urlsplit(next_url) if isinstance(next_url, str) else None
        expected = urlsplit(SEARCH_URL)
        if parsed is None or parsed.scheme != "https" or parsed.netloc != expected.netloc or parsed.path != expected.path or parsed.username or parsed.password:
            raise ValueError("eBay Browse continuation URL is invalid")
    items: list[dict] = []
    ebay_hosts = set(MARKETPLACE_DOMAINS.values())
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("eBay Browse item summary is invalid")
        item = listing_from_summary(row, source)
        if item is None:
            raise ValueError("eBay Browse item has no title or item ID")
        url = urlsplit(item["url"])
        if url.scheme != "https" or url.hostname not in ebay_hosts or url.username or url.password:
            raise ValueError("eBay Browse item has an unexpected seller URL")
        items.append(item)
    try:
        offset = int(payload.get("offset") or 0)
    except (ValueError, TypeError):
        raise ValueError("eBay Browse offset is invalid") from None
    if offset < 0 or offset > 9999:
        raise ValueError("eBay Browse offset is outside supported pagination")
    complete = next_url is None and offset + len(items) >= total
    return BrowsePage(items, total, next_url, complete)


def capture_browse_page(db: sqlite3.Connection, *, source: dict, route_id: str, window_id: str, page_number: int, payload: dict, baseline: bool, stop_after_page: bool = False, coverage_note: str | None = None, next_due_at: str | None = None, followup_job: dict | None = None, lease_job_id: int | None = None, lease_token: str | None = None) -> BrowsePage:
    if page_number < 1:
        raise ValueError("Page number must be positive")
    parsed = parse_browse_page(payload, source)
    previous = db.execute("SELECT frozen_end FROM search_windows WHERE id=?", (window_id,)).fetchone()
    frozen_end = previous[0] if previous and previous[0] else now()
    capture_page(
        db, source_id=str(source["id"]), route_id=route_id, window_id=window_id,
        items=parsed.items, page_number=page_number,
        continuation={"next": parsed.next_url, "total": parsed.total, "offset": payload.get("offset"), "dense_unsplittable": not parsed.complete and not parsed.next_url},
        complete=parsed.complete or stop_after_page, frozen_end=frozen_end, imported=baseline,
        coverage_note=coverage_note, next_due_at=next_due_at, followup_job=followup_job,
        lease_job_id=lease_job_id, lease_token=lease_token,
    )
    return parsed
