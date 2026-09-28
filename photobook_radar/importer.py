"""Repeatable read-only import of repository state and retained candidates."""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .db import transaction
from .store import capture, now


STATE_SOURCES = {
    "state.json": "oxfam_photo",
    "parent_state.json": "oxfam_broad",
    "charity_state.json": "charity",
    "ebay_private_seller_state.json": "ebay_private",
    "ebay_seller_state.json": "ebay_charity",
    "ebay_endgame_state.json": "ebay_endgame",
    "market_state.json": "market",
    "external_state.json": "external",
}


def _read(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path, data: Any) -> Iterator[tuple[str, dict | None, str, str]]:
    """Yield (key, listing payload, source, reason). None means non-listing metadata."""
    name = path.name
    if name in {"state.json", "parent_state.json", "charity_state.json"}:
        source = STATE_SOURCES[name]
        for key, value in data.get("products", {}).items():
            if not isinstance(value, dict):
                yield f"products:{key}", None, source, "invalid product payload"
                continue
            item = dict(value.get("last_snapshot") or value)
            item.setdefault("first_seen", value.get("first_seen"))
            if source.startswith("oxfam"):
                item.setdefault("sku", key)
            else:
                item.setdefault("key", key)
            yield f"products:{key}", item, source, ""
        return
    if name == "ebay_private_seller_state.json":
        for key, value in data.get("seen", {}).items():
            item = dict(value) if isinstance(value, dict) else {}
            item.setdefault("key", key)
            yield f"seen:{key}", item, "ebay_private", ""
        for key, value in data.get("deferred_discovery", {}).items():
            item = dict(value) if isinstance(value, dict) else {}
            item.setdefault("key", key)
            yield f"deferred:{key}", item, "ebay_private", ""
        return
    if name == "ebay_endgame_state.json":
        for key, value in data.get("candidates", {}).items():
            item = dict(value) if isinstance(value, dict) else {}
            item.setdefault("key", key)
            yield f"candidates:{key}", item, "ebay_endgame", ""
        return
    if name == "ebay_seller_state.json":
        for seller, state in data.get("sellers", {}).items():
            if not isinstance(state, dict):
                continue
            for key, value in state.get("seen", {}).items():
                item = dict(value) if isinstance(value, dict) else {}
                item.setdefault("key", key)
                item.setdefault("seller", seller)
                yield f"sellers:{seller}:{key}", item, "ebay_charity", ""
        return
    if name == "market_state.json":
        for group in ("feeds", "queries"):
            for route, state in data.get(group, {}).items():
                if not isinstance(state, dict):
                    continue
                for index, record in enumerate(state.get("seen", [])):
                    if isinstance(record, dict):
                        yield f"{group}:{route}:{index}", record, "market", ""
                    else:
                        yield f"{group}:{route}:{index}", None, "market", f"identity only: {str(record)[:120]}"
        return
    if name == "external_state.json":
        for route, state in data.get("sources", {}).items():
            for index, record in enumerate(state.get("seen", []) if isinstance(state, dict) else []):
                yield f"sources:{route}:{index}", record if isinstance(record, dict) else None, "external", "identity only" if not isinstance(record, dict) else ""
        return
    if isinstance(data, dict):
        for field in ("candidates", "items", "findings", "listings"):
            pool = data.get(field)
            if isinstance(pool, list):
                for index, item in enumerate(pool):
                    yield f"{field}:{index}", item if isinstance(item, dict) else None, _source_for(path, item), ""
                return
            if isinstance(pool, dict):
                for key, item in pool.items():
                    value = dict(item) if isinstance(item, dict) else None
                    if value is not None:
                        value.setdefault("key", key)
                    yield f"{field}:{key}", value, _source_for(path, item), ""
                return
        if "issues" in data and path.parent.name == "photobook_review_queue":
            for issue in data["issues"]:
                number = issue.get("number") if isinstance(issue, dict) else None
                yield f"issue-summary:{number}", None, "legacy_issue_summary", "summary only; full Issue body required"
            return
    if isinstance(data, list):
        for index, item in enumerate(data):
            yield f"row:{index}", item if isinstance(item, dict) else None, _source_for(path, item), ""


def _source_for(path: Path, item: Any) -> str:
    if isinstance(item, dict):
        source = item.get("source_id") or item.get("store")
        if isinstance(source, str) and source:
            return source
    name = path.name.lower()
    if "ebay" in name:
        return "ebay_historical"
    if "oxfam" in name or "parent" in name or name == "full_candidates.json":
        return "oxfam_historical"
    if "charity" in name:
        return "charity_historical"
    return "historical"


def _ensure_source(db: sqlite3.Connection, source_id: str) -> None:
    db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,credential_status) VALUES(?,?,'MANUAL_ONLY','NOT_REQUIRED')", (source_id, source_id))


def _import_windows(db: sqlite3.Connection, name: str, data: dict) -> None:
    if name == "ebay_endgame_state.json":
        source = "ebay_endgame"
        windows = data.get("search_windows", {})
    elif name == "ebay_private_seller_state.json":
        source = "ebay_private"
        windows = data.get("query_windows", {})
    else:
        return
    _ensure_source(db, source)
    for key, value in windows.items():
        if not isinstance(value, dict):
            continue
        route = f"{source}:{key}"
        db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,incomplete_reason) VALUES(?,?,?)", (route, source, "imported unfinished window" if not value.get("completed") else None))
        db.execute(
            "INSERT INTO search_windows(id,route_id,frozen_start,frozen_end,continuation_json,complete,last_durable_page,incomplete_reason,legacy_origin) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET continuation_json=excluded.continuation_json,complete=excluded.complete,last_durable_page=excluded.last_durable_page,incomplete_reason=excluded.incomplete_reason",
            (route, route, value.get("start"), value.get("end"), json.dumps(value.get("pending") or []), int(bool(value.get("completed"))), int(value.get("successful_pages") or 0), value.get("last_error") or ("pending" if not value.get("completed") else None), name),
        )


def import_file(db: sqlite3.Connection, path: Path, repo: Path, *, batch_size: int = 300) -> dict:
    relative = str(path.relative_to(repo))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    origin = f"repo:{relative}:{digest}"
    data = _read(path)
    rows = list(_rows(path, data))
    if not rows:
        rows = [("document", None, "metadata", "configuration or state without individual listing rows")]
    with transaction(db):
        db.execute("INSERT OR IGNORE INTO imports(origin,sha256,byte_size,rows_total) VALUES(?,?,?,?)", (origin, digest, len(raw), len(rows)))
        imported = db.execute("SELECT * FROM imports WHERE origin=?", (origin,)).fetchone()
        if imported["completed_at"]:
            return dict(imported)
        db.execute("UPDATE imports SET rows_total=? WHERE id=?", (len(rows), imported["id"]))
        if isinstance(data, dict):
            _import_windows(db, path.name, data)
    import_id = imported["id"]
    for offset in range(0, len(rows), batch_size):
        with transaction(db):
            for row_key, item, source, reason in rows[offset:offset + batch_size]:
                object_key = f"{origin}:{row_key}"
                if db.execute("SELECT 1 FROM legacy_objects WHERE object_key=?", (object_key,)).fetchone():
                    continue
                listing_id = None
                if item is None:
                    status = "QUARANTINED" if reason.startswith("identity only") or "full Issue" in reason or reason.startswith("invalid") else "NON_LISTING"
                    error = reason
                else:
                    try:
                        _ensure_source(db, source)
                        listing_id = capture(db, item, source_id=source, origin_key=object_key, imported=True)
                        status, error = "MAPPED", None
                    except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
                        status, error = "QUARANTINED", str(exc)[:300]
                db.execute("INSERT INTO legacy_objects(import_id,object_key,listing_id,object_type,parse_status,source_timestamp,raw_json,error) VALUES(?,?,?,?,?,?,?,?)", (import_id, object_key, listing_id, source, status, item.get("first_seen") if isinstance(item, dict) else None, json.dumps(item, ensure_ascii=False, default=str) if item is not None else None, error))
                column = {"MAPPED": "rows_mapped", "QUARANTINED": "rows_quarantined", "NON_LISTING": "rows_nonlisting"}[status]
                db.execute(f"UPDATE imports SET {column}={column}+1 WHERE id=?", (import_id,))
    with transaction(db):
        tally = db.execute("SELECT rows_mapped+rows_quarantined+rows_nonlisting FROM imports WHERE id=?", (import_id,)).fetchone()[0]
        if tally != len(rows):
            raise RuntimeError(f"Import accounting mismatch for {relative}: {tally} / {len(rows)}")
        db.execute("UPDATE imports SET completed_at=?,error=NULL WHERE id=?", (now(), import_id))
    return dict(db.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone())


def import_library(db: sqlite3.Connection) -> dict:
    import photobook_recognition
    import ebay_core_targets

    rows = photobook_recognition.load_library()
    with transaction(db):
        for row in rows:
            key = f"{row.get('Contributor')}|{row.get('Title')}"
            db.execute("INSERT OR REPLACE INTO book_records(record_key,contributor,title,tier,provenance,raw_json) VALUES(?,?,?,?,?,?)", (key, row.get("Contributor"), row.get("Title"), row.get("Collectibility tier"), row.get("Source"), json.dumps(row, ensure_ascii=False, default=str)))
    config = ebay_core_targets.load_targets()
    counts = {tier: len(details["names"]) for tier, details in config["tiers"].items()}
    if counts != {"1": 50, "2": 70, "3": 55}:
        raise RuntimeError(f"Core 175 counts changed: {counts}")
    return {"normalised_records": len(rows), "core_tiers": counts, "sources": photobook_recognition.library_stats()}


def link_identity_only_gaps(db: sqlite3.Connection) -> dict:
    """Attach known eBay IDs to ID-only history without inventing observations."""
    identities = {row["external_id"]: row["id"] for row in db.execute("SELECT id,external_id FROM listings WHERE platform='ebay'")}
    rows = db.execute("SELECT id,error FROM legacy_objects WHERE parse_status='QUARANTINED' AND listing_id IS NULL AND error LIKE 'identity only: ebay:%'").fetchall()
    linked = 0
    with transaction(db):
        for row in rows:
            listing_id = identities.get(row["error"].removeprefix("identity only: ebay:"))
            if listing_id:
                db.execute("UPDATE legacy_objects SET listing_id=? WHERE id=?", (listing_id, row["id"]))
                linked += 1
    return {"identity_only_gaps_checked": len(rows), "linked_to_existing_listing": linked, "still_quarantined": True}


def import_repository(db: sqlite3.Connection, repo: Path) -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    files = sorted((repo / "data").rglob("*.json")) + sorted((repo / "data").rglob("*.json.gz"))
    results = []
    for path in files:
        try:
            results.append(import_file(db, path, repo))
        except Exception as exc:
            results.append({"origin": str(path.relative_to(repo)), "error": str(exc)})
    library = import_library(db)
    counts = dict(db.execute("SELECT COUNT(*) AS listings FROM listings").fetchone())
    totals = dict(db.execute("SELECT SUM(rows_total) raw_rows,SUM(rows_mapped) mapped,SUM(rows_quarantined) quarantined,SUM(rows_nonlisting) nonlisting FROM imports WHERE completed_at IS NOT NULL").fetchone())
    return {"commit": commit, "captured_at": now(), "files": len(files), "results": results, "library": library, "counts": counts, "reconciliation": totals}
