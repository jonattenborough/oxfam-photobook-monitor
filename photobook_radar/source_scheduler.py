"""Durable, bounded Oxfam Photography scan jobs (production gate only)."""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .db import transaction
from .sources.oxfam import capture_photography_page, fetch_photography_page, parse_photography_page
from .store import enqueue_job, now

ROUTE = "oxfam-photography"
FRONTIER_PAGES = 2
DEEP_INTERVAL_HOURS = 24


def _later(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


def schedule_oxfam(db: sqlite3.Connection, config: Config) -> bool:
    if not (config.production and config.allow_marketplace_network and config.source_oxfam_photography):
        return False
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,?,600)", (ROUTE, "oxfam-photography", "SCHEDULED"))
        db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,market) VALUES(?,?,?,?)", (ROUTE, ROUTE, "PHOTOGRAPHY", "GB"))
        route = db.execute("SELECT * FROM source_routes WHERE id=?", (ROUTE,)).fetchone()
        outstanding = db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind='SCAN_OXFAM' AND status IN ('PENDING','RUNNING') LIMIT 1", (ROUTE,)).fetchone()
        if outstanding:
            return False
        unfinished = db.execute("SELECT * FROM search_windows WHERE route_id=? AND complete=0 ORDER BY id DESC LIMIT 1", (ROUTE,)).fetchone()
        if unfinished:
            cursor = json.loads(unfinished["continuation_json"])
            offset = cursor.get("next_offset")
            if not isinstance(offset, int) or offset < 0:
                db.execute("UPDATE sources SET status='DEGRADED',last_error='Unusable saved Oxfam continuation' WHERE id=?", (ROUTE,))
                return False
            page = unfinished["last_durable_page"] + 1
            return enqueue_job(db, f"scan:{unfinished['id']}:{page}", "SCAN_OXFAM", route_id=ROUTE, priority=30, payload={"window_id": unfinished["id"], "page_number": page, "offset": offset, "baseline": route["last_success_at"] is None, "deep": bool(cursor.get("deep"))})
        if route["next_due_at"] and route["next_due_at"] > now():
            return False
        window_id = f"{ROUTE}:{now()}:{secrets.token_hex(4)}"
        last_deep = db.execute("SELECT value FROM health WHERE key='oxfam_photography_last_deep'").fetchone()
        deep = last_deep is None or last_deep[0] <= (datetime.now(timezone.utc) - timedelta(hours=DEEP_INTERVAL_HOURS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        scheduled = enqueue_job(db, f"scan:{window_id}:1", "SCAN_OXFAM", route_id=ROUTE, priority=30, payload={"window_id": window_id, "page_number": 1, "offset": 0, "baseline": route["last_success_at"] is None, "deep": deep})
        if scheduled:
            db.execute("UPDATE source_routes SET next_due_at=? WHERE id=?", (_later(10), ROUTE))
        return scheduled


def run_oxfam_scan_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[Config, int], dict] = fetch_photography_page) -> dict:
    if not (config.production and config.allow_marketplace_network and config.source_oxfam_photography):
        raise RuntimeError("Oxfam scan job is not permitted in this mode")
    payload = json.loads(job["payload_json"])
    page = int(payload["page_number"])
    offset = int(payload["offset"])
    if page < 1 or offset != (page - 1) * 30:
        raise ValueError("Oxfam scan job has an invalid page cursor")
    durable = db.execute("SELECT last_durable_page FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0] >= page:
        return {"page": page, "already_durable": True}
    response = fetch(config, offset)
    parsed = parse_photography_page(response, offset)
    deep = bool(payload.get("deep"))
    frontier_stop = not deep and page >= FRONTIER_PAGES and parsed.next_offset is not None
    coverage_note = f"Frontier scan: first {offset + len(parsed.items)} of {parsed.total} newest records; older stock awaits daily full sweep" if frontier_stop else None
    next_job = None
    if parsed.next_offset is not None and not frontier_stop:
        next_job = {
            "key": f"scan:{payload['window_id']}:{page + 1}", "kind": "SCAN_OXFAM", "route_id": ROUTE, "priority": 30,
            "payload": {"window_id": payload["window_id"], "page_number": page + 1, "offset": parsed.next_offset, "baseline": bool(payload["baseline"]), "deep": deep},
        }
    completed = parsed.next_offset is None or frontier_stop
    capture_photography_page(db, window_id=payload["window_id"], page_number=page, payload=response, baseline=bool(payload["baseline"]), followup_job=next_job, next_due_at=_later(10) if completed else None, stop_after_page=frontier_stop, coverage_note=coverage_note, deep=deep, lease_job_id=job["id"], lease_token=job["lease_token"])
    with transaction(db):
        db.execute("UPDATE sources SET status=?,last_error=NULL,last_success_at=? WHERE id=?", ("ACTIVE" if completed and not frontier_stop else "PARTIAL" if frontier_stop else "BASELINING", now(), ROUTE))
        if completed and deep:
            db.execute("INSERT INTO health(key,value,updated_at) VALUES('oxfam_photography_last_deep',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (now(), now()))
    return {"page": page, "items": len(parsed.items), "complete": completed, "frontier_only": frontier_stop}
