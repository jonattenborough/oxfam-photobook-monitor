"""Durable private, charity-seller and auction searches on one metered quota."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import ebay_endgame as endgame
import ebay_private_recall_monitor as private_recall
import ebay_private_seller_monitor as private_legacy
import ebay_seller_monitor as seller_legacy

from .config import Config
from .db import transaction
from .ebay_gateway import thread_client
from .sources.ebay import capture_browse_page, parse_browse_page
from .store import enqueue_job, now

ROOT = Path(__file__).resolve().parent.parent
LANES = {"private": ("ebay-private", "SCAN_EBAY_PRIVATE", 3600),
         "charity": ("ebay-charity", "SCAN_EBAY_CHARITY", 3600),
         "endgame": ("ebay-endgame", "SCAN_EBAY_ENDGAME", 900)}
CATEGORY_FALLBACK_QUERY = {
    "EBAY_AT": "Fotobuch", "EBAY_AU": "photography book", "EBAY_BE": "fotoboek",
    "EBAY_CA": "photography book", "EBAY_CH": "Fotobuch", "EBAY_DE": "Fotobuch",
    "EBAY_ES": "fotolibro", "EBAY_FR": "livre photographie", "EBAY_HK": "photography book",
    "EBAY_IE": "photography book", "EBAY_IT": "libro fotografico", "EBAY_NL": "fotoboek",
    "EBAY_PL": "książka fotograficzna", "EBAY_SG": "photography book", "EBAY_US": "photography book",
}
_ENDGAME_TASKS: list[dict] | None = None


def _stamp_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _enabled(config: Config, lane: str) -> bool:
    return config.production and config.allow_marketplace_network and {
        "private": config.source_ebay_private,
        "charity": config.source_ebay_charity,
        "endgame": config.source_ebay_endgame,
    }[lane]


def _health(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM health WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_health(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO health(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, value, now()))


def _route(db: sqlite3.Connection, source: str, lane: str, key: str, *, marketplace: str, query: str | None) -> str:
    safe_key = hashlib.sha256(key.encode()).hexdigest()[:20]
    route_id = f"{source}:{safe_key}"
    db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,market,query_text) VALUES(?,?,?,?,?)", (route_id, source, lane.upper(), marketplace, query))
    return route_id


def _enqueue(db: sqlite3.Connection, lane: str, definition: dict) -> bool:
    source, kind, cadence = LANES[lane]
    marketplace = str(definition.get("marketplace") or "EBAY_GB")
    query = definition.get("query")
    key = str(definition.get("task_key") or json.dumps([lane, marketplace, query, definition.get("seller_id"), definition.get("buying_options")], sort_keys=True))
    route_id = _route(db, source, lane, key, marketplace=marketplace, query=query)
    route = db.execute("SELECT last_success_at FROM source_routes WHERE id=?", (route_id,)).fetchone()
    if db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind=? AND status IN ('PENDING','RUNNING')", (route_id, kind)).fetchone():
        return False
    window = f"{route_id}:{now()}:{secrets.token_hex(4)}"
    payload = {"lane": lane, "route_id": route_id, "window_id": window, "definition": definition,
               "baseline": route["last_success_at"] is None, "page": 1}
    if enqueue_job(db, f"scan:{window}:1", kind, route_id=route_id,
                   priority=55 if lane == "endgame" else 35, payload=payload):
        db.execute("UPDATE source_routes SET next_due_at=? WHERE id=?", (_stamp_after(cadence), route_id))
        return True
    return False


def _schedule_private(db: sqlite3.Connection) -> int:
    config = private_recall.recall_config(private_legacy.load_config(ROOT / "data/ebay_private_searches.json"))
    state = json.loads(_health(db, "ebay_private_cursors") or '{"cursors":{}}')
    state.setdefault("cursors", {})
    steps = private_recall._build_fresh_search_plan(config, state, datetime.now(timezone.utc), 17)
    count = 0
    for step in steps:
        definition = {"query": step["query"], "marketplace": "EBAY_GB", "category_ids": step.get("category_ids"),
                      "buying_options": step.get("buying_options"), "search_in_description": bool(step.get("search_in_description")),
                      "lane_name": step["lane"], "offset": int(step.get("offset") or 0)}
        count += _enqueue(db, "private", definition)
    _set_health(db, "ebay_private_cursors", json.dumps(state, sort_keys=True))
    return count


def _schedule_charity(db: sqlite3.Connection) -> int:
    sellers = seller_legacy.load_config(ROOT / "data/ebay_sellers.json")
    cursor = int(_health(db, "ebay_charity_cursor") or "0")
    selected, following = seller_legacy.select_sellers(sellers, cursor, 12)
    count = 0
    for seller in selected:
        definition = {"marketplace": seller["marketplace"], "seller_id": seller["id"],
                      "delivery_country": seller.get("delivery_country")}
        count += _enqueue(db, "charity", definition)
    _set_health(db, "ebay_charity_cursor", str(following))
    return count


def _endgame_tasks() -> list[dict]:
    global _ENDGAME_TASKS
    if _ENDGAME_TASKS is None:
        settings = endgame.load_config(ROOT / "data/ebay_endgame_targets.json")
        _ENDGAME_TASKS = endgame.build_tasks(settings)
    return _ENDGAME_TASKS


def _endgame_slots(db: sqlite3.Connection, config: Config) -> int:
    latest = db.execute("SELECT provider_remaining,reset_at FROM api_windows WHERE bucket='ebay_browse' AND reset_at>? ORDER BY id DESC LIMIT 1", (now(),)).fetchone()
    if not latest or latest["provider_remaining"] is None:
        return 26
    seconds = (datetime.fromisoformat(latest["reset_at"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    cycles = max(1, int((seconds + 899) // 900))
    usable = max(0, int(latest["provider_remaining"]) - config.ebay_reserve)
    # Broad, private and charity together need about eight first-page calls
    # per quarter hour. Spend the rest where the Endgame task matrix is due.
    return min(35, max(0, usable // cycles - 8))


def _schedule_endgame(db: sqlite3.Connection, config: Config) -> int:
    settings = endgame.load_config(ROOT / "data/ebay_endgame_targets.json")
    tasks = _endgame_tasks()
    schedule = {}
    for row in db.execute("SELECT id,last_success_at FROM source_routes WHERE source_id='ebay-endgame'"):
        schedule[row["id"]] = row["last_success_at"]
    outstanding = {row[0] for row in db.execute("SELECT route_id FROM jobs WHERE kind='SCAN_EBAY_ENDGAME' AND status IN ('PENDING','RUNNING')")}
    # The legacy selector keys by task identity; map its durable route times.
    per_task = {}
    for task in tasks:
        route_id = f"ebay-endgame:{hashlib.sha256(str(task['key']).encode()).hexdigest()[:20]}"
        per_task[task["key"]] = schedule.get(route_id)
    eligible = [task for task in tasks if f"ebay-endgame:{hashlib.sha256(str(task['key']).encode()).hexdigest()[:20]}" not in outstanding]
    selected = endgame.select_due_tasks(eligible, per_task, datetime.now(timezone.utc), settings, _endgame_slots(db, config))
    count = 0
    for task in selected:
        definition = {"task_key": task["key"], "query": task.get("query"), "marketplace": task["marketplace"],
                      "category_ids": task.get("category_ids"), "search_in_description": bool(task.get("search_in_description")),
                      "horizon_hours": float(task["horizon_hours"]), "lane_name": task["lane"], "interval_hours": float(task["interval_hours"])}
        count += _enqueue(db, "endgame", definition)
    return count


def schedule_ebay_lanes(db: sqlite3.Connection, config: Config) -> dict[str, int]:
    counts = {}
    with transaction(db):
        for lane, (source, kind, cadence) in LANES.items():
            if not _enabled(config, lane):
                continue
            db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,'SCHEDULED',?)", (source, "ebay-browse", cadence))
            key = f"ebay_{lane}_last_schedule"
            last = _health(db, key)
            if last and last > now():
                continue
            counts[lane] = {"private": _schedule_private, "charity": _schedule_charity,
                            "endgame": lambda db: _schedule_endgame(db, config)}[lane](db)
            _set_health(db, key, _stamp_after(cadence))
    return counts


def _fetch(db: sqlite3.Connection, config: Config, payload: dict) -> dict:
    definition = payload["definition"]
    lane = payload["lane"]
    client = thread_client(db, config, payload["route_id"], marketplace=definition["marketplace"])
    if payload.get("next_url"):
        return client.search_next(payload["next_url"])
    if lane == "private":
        return client.search_page(definition["query"], limit=200, category_ids=definition.get("category_ids"),
                                  fixed_price_only=False, buying_options=definition.get("buying_options"), seller_account_type="INDIVIDUAL",
                                  delivery_country="GB", search_in_description=definition.get("search_in_description", False),
                                  price_max=750, price_currency="GBP", offset=definition.get("offset", 0), sort="newlyListed")
    if lane == "charity":
        return client.search_page(None, limit=200, category_ids="261186", fixed_price_only=True,
                                  seller_ids=[definition["seller_id"]], delivery_country=definition.get("delivery_country"), sort="newlyListed")
    end = datetime.now(timezone.utc) + timedelta(hours=definition["horizon_hours"])
    query = definition.get("query")
    category_ids = definition.get("category_ids")
    if definition.get("lane_name") == "category" and definition["marketplace"] != "EBAY_GB":
        # The UK photography-book category ID is not valid in every market.
        query = CATEGORY_FALLBACK_QUERY[definition["marketplace"]]
        category_ids = None
    return client.search_page(query, limit=200, category_ids=category_ids,
                              fixed_price_only=False, buying_options=["AUCTION"], ending_start_date=now(),
                              ending_end_date=end.isoformat(timespec="seconds").replace("+00:00", "Z"),
                              search_in_description=definition.get("search_in_description", False), sort="endingSoonest")


def run_ebay_lane_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[sqlite3.Connection, Config, dict], dict] = _fetch) -> dict:
    payload = json.loads(job["payload_json"])
    lane = payload.get("lane")
    if lane not in LANES or not _enabled(config, lane) or job["route_id"] != payload.get("route_id"):
        raise ValueError("eBay lane job is invalid or disabled")
    page = int(payload.get("page") or 0)
    max_pages = 5 if lane == "charity" else 2 if lane == "endgame" else 1
    if not 1 <= page <= max_pages:
        raise ValueError("eBay page cursor is invalid")
    durable = db.execute("SELECT last_durable_page FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0] >= page:
        return {"already_durable": True}
    with transaction(db):
        db.execute("UPDATE source_routes SET last_attempt_at=? WHERE id=?", (now(), payload["route_id"]))
    response = fetch(db, config, payload)
    definition = payload["definition"]
    source = {"id": LANES[lane][0], "name": f"eBay {lane} {definition.get('lane_name') or definition.get('seller_id') or ''}",
              "marketplace": definition["marketplace"]}
    parsed = parse_browse_page(response, source)
    more = bool(parsed.next_url and page < max_pages)
    note = f"First {page * 200} results only; more pages exist" if parsed.next_url and not more else None
    next_job = None
    if more:
        next_job = {"key": f"scan:{payload['window_id']}:{page + 1}", "kind": LANES[lane][1],
                    "route_id": payload["route_id"], "priority": 55 if lane == "endgame" else 35,
                    "payload": {**payload, "page": page + 1, "next_url": parsed.next_url}}
    cadence = int(definition.get("interval_hours", LANES[lane][2] / 3600) * 3600) if lane == "endgame" else LANES[lane][2]
    capture_browse_page(db, source=source, route_id=payload["route_id"], window_id=payload["window_id"],
                        page_number=page, payload=response, baseline=bool(payload["baseline"]), stop_after_page=not more,
                        coverage_note=note, next_due_at=_stamp_after(cadence) if not more else None,
                        followup_job=next_job, lease_job_id=job["id"], lease_token=job["lease_token"])
    with transaction(db):
        db.execute("UPDATE sources SET status=?,last_error=NULL,last_success_at=? WHERE id=?", ("PARTIAL" if note else "ACTIVE" if not more else "BASELINING", now(), LANES[lane][0]))
    return {"lane": lane, "route": payload["route_id"], "items": len(parsed.items), "page": page, "complete": not more}
