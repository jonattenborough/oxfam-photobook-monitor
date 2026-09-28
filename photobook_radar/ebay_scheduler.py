"""Two bounded hourly broad eBay feeds retained from the legacy market monitor."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .db import transaction
from .ebay_gateway import thread_client
from .sources.ebay import capture_browse_page, parse_browse_page
from .store import enqueue_job, now

SOURCE = "ebay-broad"
ROUTES = {
    "ebay-broad:photobook": {"query": "photobook", "name": "eBay UK newest photobooks"},
    "ebay-broad:photography-book": {"query": "photography book", "name": "eBay UK newest photography books"},
}


def _later(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def schedule_ebay_broad(db: sqlite3.Connection, config: Config) -> int:
    if not (config.production and config.allow_marketplace_network and config.source_ebay_broad):
        return 0
    count = 0
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,?,3600)", (SOURCE, "ebay-browse", "SCHEDULED"))
        for route_id, definition in ROUTES.items():
            db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,market,query_text) VALUES(?,?,?,?,?)", (route_id, SOURCE, "BROAD", "EBAY_GB", definition["query"]))
            route = db.execute("SELECT next_due_at,last_success_at FROM source_routes WHERE id=?", (route_id,)).fetchone()
            outstanding = db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind='SCAN_EBAY_BROAD' AND status IN ('PENDING','RUNNING') LIMIT 1", (route_id,)).fetchone()
            if outstanding or (route["next_due_at"] and route["next_due_at"] > now()):
                continue
            window_id = f"{route_id}:{now()}:{secrets.token_hex(4)}"
            if enqueue_job(db, f"scan:{window_id}:1", "SCAN_EBAY_BROAD", route_id=route_id, priority=25, payload={"window_id": window_id, "route_id": route_id, "baseline": route["last_success_at"] is None}):
                db.execute("UPDATE source_routes SET next_due_at=? WHERE id=?", (_later(), route_id))
                count += 1
    return count


def _fetch(db: sqlite3.Connection, config: Config, route_id: str, query: str) -> dict:
    client = thread_client(db, config, route_id)
    return client.search_page(query, category_ids="261186", limit=200, fixed_price_only=True, sort="newlyListed")


def run_ebay_broad_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[sqlite3.Connection, Config, str, str], dict] = _fetch) -> dict:
    if not (config.production and config.allow_marketplace_network and config.source_ebay_broad):
        raise RuntimeError("eBay scanning is disabled in this mode")
    payload = json.loads(job["payload_json"])
    route_id = payload.get("route_id")
    if route_id not in ROUTES or job["route_id"] != route_id or not isinstance(payload.get("window_id"), str):
        raise ValueError("Invalid eBay broad scan job")
    durable = db.execute("SELECT complete FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0]:
        return {"already_durable": True}
    with transaction(db):
        db.execute("UPDATE source_routes SET last_attempt_at=? WHERE id=?", (now(), route_id))
    definition = ROUTES[route_id]
    response = fetch(db, config, route_id, definition["query"])
    parsed = parse_browse_page(response, {"id": SOURCE, "name": definition["name"], "marketplace": "EBAY_GB"})
    coverage_note = None
    if not parsed.complete:
        coverage_note = f"Hourly frontier: first {len(parsed.items)} of {parsed.total} results; later pages are not covered by this route"
    capture_browse_page(
        db, source={"id": SOURCE, "name": definition["name"], "marketplace": "EBAY_GB"},
        route_id=route_id, window_id=payload["window_id"], page_number=1,
        payload=response, baseline=bool(payload["baseline"]), stop_after_page=True,
        coverage_note=coverage_note, next_due_at=_later(),
        lease_job_id=job["id"], lease_token=job["lease_token"],
    )
    with transaction(db):
        db.execute("UPDATE sources SET status=?,last_error=NULL,last_success_at=? WHERE id=?", ("PARTIAL" if coverage_note else "ACTIVE", now(), SOURCE))
    return {"route_id": route_id, "items": len(parsed.items), "total": parsed.total, "frontier_only": bool(coverage_note)}
