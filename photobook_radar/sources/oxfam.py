"""Oxfam Photography catalogue page adapter using the established parser."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import monitor
import oxfam_parent_common

from ..config import Config
from ..store import capture_page, now


@dataclass(frozen=True)
class ParsedPage:
    items: list[dict]
    total: int
    next_offset: int | None


def parse_photography_page(payload: dict, offset: int) -> ParsedPage:
    """A malformed or blocked response cannot become an empty baseline."""
    if offset < 0 or offset % monitor.PAGE_SIZE:
        raise ValueError("Invalid Oxfam page offset")
    summary = monitor.find_results_summary(payload)
    total = summary.get("totalMatchingRecords")
    if not isinstance(total, int) or total < 1:
        raise ValueError("Oxfam category count missing or unexpectedly empty")
    ordered, product_ids = monitor.ordered_skus(payload)
    if len(ordered) > monitor.PAGE_SIZE or offset + len(ordered) > total:
        raise ValueError("Oxfam page count is inconsistent")
    meta = monitor.collect_metadata(payload, set(ordered))
    items: list[dict] = []
    for sku in ordered:
        item = monitor.item_from_meta(sku, product_ids.get(sku), meta.get(sku, {}))
        if not item.get("title"):
            raise ValueError(f"Oxfam listing {sku} has no title")
        item["url"] = oxfam_parent_common.absolute_product_url(item)
        parsed_url = urlsplit(item["url"])
        if parsed_url.scheme != "https" or parsed_url.hostname != "onlineshop.oxfam.org.uk" or parsed_url.username or parsed_url.password:
            raise ValueError(f"Oxfam listing {sku} has an unexpected seller URL")
        items.append(item)
    next_offset = offset + len(items) if offset + len(items) < total else None
    if next_offset is not None and len(items) != monitor.PAGE_SIZE:
        raise ValueError("Oxfam search page truncated before reported end")
    return ParsedPage(items, total, next_offset)


def fetch_photography_page(config: Config, offset: int, *, client: httpx.Client | None = None) -> dict:
    if not (config.production and config.allow_marketplace_network):
        raise RuntimeError("Live Oxfam scans are disabled")
    params = {"N": monitor.CATEGORY_ID, "No": str(offset), "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)", "Nrpp": str(monitor.PAGE_SIZE), "Ns": "product.creationDate|1"}
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False)
    try:
        response = client.get(monitor.SEARCH_URL, params=params, headers=monitor.HEADERS)
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            raise ValueError("Oxfam search did not return JSON; possible block or error page")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Oxfam search response is not an object")
        return payload
    finally:
        if own:
            client.close()


def capture_photography_page(db: sqlite3.Connection, *, window_id: str, page_number: int, payload: dict, baseline: bool, followup_job: dict | None = None, next_due_at: str | None = None, stop_after_page: bool = False, coverage_note: str | None = None, deep: bool = False, lease_job_id: int | None = None, lease_token: str | None = None) -> ParsedPage:
    if page_number < 1:
        raise ValueError("Page number must be positive")
    parsed = parse_photography_page(payload, (page_number - 1) * monitor.PAGE_SIZE)
    previous = db.execute("SELECT frozen_end FROM search_windows WHERE id=?", (window_id,)).fetchone()
    frozen_end = previous[0] if previous and previous[0] else now()
    capture_page(
        db, source_id="oxfam-photography", route_id="oxfam-photography", window_id=window_id,
        items=parsed.items, page_number=page_number,
        continuation={"next_offset": parsed.next_offset, "total": parsed.total, "deep": deep},
        complete=parsed.next_offset is None or stop_after_page, frozen_end=frozen_end, imported=baseline,
        followup_job=followup_job, next_due_at=next_due_at, coverage_note=coverage_note,
        lease_job_id=lease_job_id, lease_token=lease_token,
    )
    return parsed
