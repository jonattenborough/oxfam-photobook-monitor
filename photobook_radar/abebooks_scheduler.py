"""Bounded quarter-hour rotation of Parr/Badger exact-title AbeBooks searches."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

import market_monitor

from .config import Config
from .db import transaction
from .store import capture_page, enqueue_job, now

SOURCE = "abebooks"
SEARCHES_PER_CYCLE = 24
CADENCE_SECONDS = 15 * 60


def _later() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=CADENCE_SECONDS)).isoformat(timespec="seconds").replace("+00:00", "Z")


def schedule_abebooks(db: sqlite3.Connection, config: Config) -> int:
    if not (config.production and config.allow_marketplace_network and config.source_abebooks):
        return 0
    with transaction(db):
        db.execute("INSERT INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,'SCHEDULED',?) ON CONFLICT(id) DO UPDATE SET cadence_seconds=excluded.cadence_seconds", (SOURCE, "abebooks-html", CADENCE_SECONDS))
        due = db.execute("SELECT value FROM health WHERE key='abebooks_next_schedule'").fetchone()
        if due and due[0] > now():
            return 0
        records = market_monitor.master_rows()
        if not records:
            raise RuntimeError("Parr/Badger target catalogue is empty")
        position = db.execute("SELECT value FROM health WHERE key='abebooks_cursor'").fetchone()
        cursor = int(position[0]) if position else 0
        count = 0
        for index in range(min(SEARCHES_PER_CYCLE, len(records))):
            record = records[(cursor + index) % len(records)]
            url, query = market_monitor.target_url("abebooks", record)
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "www.abebooks.co.uk" or parsed.path != "/servlet/SearchResults":
                raise ValueError("AbeBooks search URL is invalid")
            route_id = SOURCE + ":" + hashlib.sha256(query.casefold().encode()).hexdigest()[:20]
            db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,market,query_text) VALUES(?,?,?,?,?)", (route_id, SOURCE, "EXACT_TITLE", "UK", query))
            route = db.execute("SELECT last_success_at FROM source_routes WHERE id=?", (route_id,)).fetchone()
            if db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind='SCAN_ABEBOOKS' AND status IN ('PENDING','RUNNING')", (route_id,)).fetchone():
                continue
            window = f"{route_id}:{now()}:{index}"
            if enqueue_job(db, f"abe:{window}", "SCAN_ABEBOOKS", route_id=route_id, priority=18,
                           payload={"route_id": route_id, "window_id": window, "url": url, "query": query,
                                    "baseline": route["last_success_at"] is None}):
                count += 1
        stamp = now()
        for key, value in (("abebooks_cursor", str((cursor + SEARCHES_PER_CYCLE) % len(records))), ("abebooks_next_schedule", _later())):
            db.execute("INSERT INTO health(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, value, stamp))
        return count


def fetch_html(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.abebooks.co.uk" or parsed.path != "/servlet/SearchResults":
        raise ValueError("AbeBooks search URL is invalid")
    request = urllib.request.Request(url, headers={"User-Agent": "PhotobookRadar/1.0", "Accept": "text/html", "Accept-Language": "en-GB,en;q=0.9"})
    with urllib.request.urlopen(request, timeout=18) as response:
        if response.status != 200:
            raise RuntimeError(f"AbeBooks returned HTTP {response.status}")
        body = response.read(4_000_001)
    if len(body) > 4_000_000:
        raise ValueError("AbeBooks search page exceeded limit")
    return body.decode("utf-8", errors="replace")


def run_abebooks_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[str], str] = fetch_html) -> dict:
    if not (config.production and config.allow_marketplace_network and config.source_abebooks):
        raise RuntimeError("AbeBooks scan is disabled")
    payload = json.loads(job["payload_json"])
    if payload.get("route_id") != job["route_id"] or not isinstance(payload.get("window_id"), str):
        raise ValueError("AbeBooks job is invalid")
    durable = db.execute("SELECT complete FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0]:
        return {"already_durable": True}
    with transaction(db):
        db.execute("UPDATE source_routes SET last_attempt_at=? WHERE id=?", (now(), job["route_id"]))
    html = fetch(payload["url"])
    rows = market_monitor.parse_abe_or_biblio(html, {"id": SOURCE, "name": "AbeBooks exact title"}, "abebooks")
    for row in rows:
        row["target_query"] = payload["query"]
    capture_page(db, source_id=SOURCE, route_id=job["route_id"], window_id=payload["window_id"], page_number=1,
                 items=rows, continuation={"one_page_frontier": True}, complete=True,
                 imported=bool(payload["baseline"]), next_due_at=_later(),
                 coverage_note="First results page only; later pages outside hourly frontier" if rows else None,
                 lease_job_id=job["id"], lease_token=job["lease_token"])
    with transaction(db):
        db.execute("UPDATE sources SET status='ACTIVE',last_error=NULL,last_success_at=? WHERE id=?", (now(), SOURCE))
    return {"route_id": job["route_id"], "items": len(rows)}
