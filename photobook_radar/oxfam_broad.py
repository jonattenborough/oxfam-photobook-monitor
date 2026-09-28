"""Newest-first Art & Photography parent-category safety net."""
from __future__ import annotations

import json
import secrets
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

import oxfam_parent_common as parent

from .config import Config
from .db import transaction
from .store import capture_page, enqueue_job, now

SOURCE = "oxfam-broad"
PAGE_SIZE = 90
PAGES = 2
LAST_KNOWN_DIMENSION = "3961531481"


def _later() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")


def schedule_oxfam_broad(db: sqlite3.Connection, config: Config) -> bool:
    if not (config.production and config.allow_marketplace_network and config.source_oxfam_broad):
        return False
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,'SCHEDULED',600)", (SOURCE, "oxfam-parent"))
        db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,query_text) VALUES(?,?,?,?)", (SOURCE, SOURCE, "ART_PHOTOGRAPHY", parent.TARGET_PARENT_ROUTE))
        route = db.execute("SELECT next_due_at,last_success_at FROM source_routes WHERE id=?", (SOURCE,)).fetchone()
        if (route["next_due_at"] and route["next_due_at"] > now()) or db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind='SCAN_OXFAM_BROAD' AND status IN ('PENDING','RUNNING')", (SOURCE,)).fetchone():
            return False
        window = f"{SOURCE}:{now()}:{secrets.token_hex(4)}"
        if enqueue_job(db, f"scan:{window}:1", "SCAN_OXFAM_BROAD", route_id=SOURCE, priority=29,
                       payload={"window_id": window, "page": 1, "baseline": route["last_success_at"] is None}):
            db.execute("UPDATE source_routes SET next_due_at=? WHERE id=?", (_later(), SOURCE))
            return True
    return False


def _request(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PhotobookRadar/1.0", "Referer": parent.BASE_URL + parent.TARGET_PARENT_ROUTE})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Oxfam returned HTTP {response.status}")
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise ValueError("Oxfam response exceeded limit")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Oxfam response is invalid")
    return payload


def _dimension(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value,updated_at FROM health WHERE key='oxfam_parent_dimension'").fetchone()
    if row and row["updated_at"] > (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z"):
        return row["value"]
    url = parent.BASE_URL + "/ccstore/v1/collections/rootCategory?" + urllib.parse.urlencode({"catalogId": parent.CATALOG_ID, "expand": "childCategories", "maxLevel": "20", "disableActiveProdCheck": "true"})
    found = parent._find_target_collection(_request(url))
    if not found:
        if row:
            return row["value"]
        raise RuntimeError("Oxfam parent category could not be discovered")
    dimension = found[0]
    with transaction(db):
        db.execute("INSERT INTO health(key,value,updated_at) VALUES('oxfam_parent_dimension',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (dimension, now()))
    return dimension


def fetch_page(db: sqlite3.Connection, page: int) -> dict:
    dimension = _dimension(db)
    query = urllib.parse.urlencode({"N": dimension, "No": str((page - 1) * PAGE_SIZE),
                                    "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)",
                                    "Nrpp": str(PAGE_SIZE), "Ns": "product.creationDate|1"})
    return _request(parent.SEARCH_URL + "?" + query)


def parse_page(payload: dict) -> tuple[list[dict], int]:
    parent.require_newest_first(payload)
    summary = parent.find_results_summary(payload)
    total = summary.get("totalMatchingRecords")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Oxfam parent total is invalid")
    ordered, product_ids = parent.ordered_skus(payload)
    metadata = parent.collect_metadata(payload, set(ordered))
    items = []
    for sku in ordered:
        item = parent.item_from_meta(sku, product_ids.get(sku), metadata[sku])
        if not item["title"]:
            raise ValueError("Oxfam parent record lacks a title")
        item["url"] = parent.absolute_product_url(item)
        item["first_seen"] = item.get("creation_date")
        items.append(item)
    return items, total


def run_oxfam_broad_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[sqlite3.Connection, int], dict] = fetch_page) -> dict:
    if not (config.production and config.allow_marketplace_network and config.source_oxfam_broad) or job["route_id"] != SOURCE:
        raise RuntimeError("Oxfam broad scan is disabled or invalid")
    payload = json.loads(job["payload_json"])
    page = int(payload.get("page") or 0)
    if not 1 <= page <= PAGES:
        raise ValueError("Oxfam broad page is invalid")
    durable = db.execute("SELECT last_durable_page FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0] >= page:
        return {"already_durable": True}
    with transaction(db):
        db.execute("UPDATE source_routes SET last_attempt_at=? WHERE id=?", (now(), SOURCE))
    items, total = parse_page(fetch(db, page))
    more = len(items) == PAGE_SIZE and page < PAGES and page * PAGE_SIZE < total
    note = f"Frontier covers first {page * PAGE_SIZE} of {total}; older stock outside this lane" if not more and page * PAGE_SIZE < total else None
    followup = {"key": f"scan:{payload['window_id']}:{page + 1}", "kind": "SCAN_OXFAM_BROAD", "route_id": SOURCE,
                "priority": 29, "payload": {**payload, "page": page + 1}} if more else None
    capture_page(db, source_id=SOURCE, route_id=SOURCE, window_id=payload["window_id"], items=items, page_number=page,
                 continuation={"next_page": page + 1 if more else None, "total": total}, complete=not more,
                 imported=bool(payload["baseline"]), followup_job=followup, next_due_at=_later() if not more else None,
                 coverage_note=note, lease_job_id=job["id"], lease_token=job["lease_token"])
    with transaction(db):
        db.execute("UPDATE sources SET status=?,last_error=NULL,last_success_at=? WHERE id=?", ("PARTIAL" if note else "ACTIVE", now(), SOURCE))
    return {"page": page, "items": len(items), "total": total, "complete": not more}
