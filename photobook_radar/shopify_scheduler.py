"""Durable charity and specialist Shopify collection scans."""
from __future__ import annotations

import html
import json
import re
import secrets
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .db import transaction
from .store import capture_page, enqueue_job, now

PAGE_SIZE = 250
MAX_PAGES = 20
ROUTES = {
    "charity:shelter-art": ("Shelter Art & Photography", "https://shop.shelter.org.uk", "collections/art-photography-books", 600),
    "charity:shelter-rare": ("Shelter Rare & Collectable", "https://shop.shelter.org.uk", "collections/antiquarian-rare-collectable-books", 600),
    "charity:shelter-books": ("Shelter Second Hand Books", "https://shop.shelter.org.uk", "collections/secondhand-books", 600),
    "charity:crisis-books": ("Crisis Books", "https://shopfromcrisis.org.uk", "collections/books", 600),
    "specialist:tpg": ("The Photographers' Gallery", "https://bookshop.thephotographersgallery.org.uk", "collections/new-arrivals-1", 3600),
    "specialist:photobookstore": ("Photobookstore", "https://photobookstore.co.uk", "", 3600),
    "specialist:village": ("Village Books", "https://villagebooks.co", "", 3600),
    "specialist:setanta": ("Setanta Books", "https://www.setantabooks.com", "", 3600),
    "publisher:mack": ("MACK / SPBH Editions", "https://mackbooks.co.uk", "collections/newly-added", 21600),
    "publisher:stanley": ("STANLEY/BARKER", "https://www.stanleybarker.co.uk", "", 21600),
    "publisher:tbw": ("TBW Books", "https://tbwbooks.com", "", 21600),
    "publisher:loose": ("Loose Joints", "https://loosejoints.biz", "", 21600),
    "publisher:rrb": ("RRB Photobooks", "https://rrbphotobooks.com", "", 21600),
    "publisher:deadbeat": ("Deadbeat Club", "https://deadbeatclubpress.com", "", 21600),
    "publisher:gost": ("GOST Books", "https://gostbooks.com", "", 21600),
    "publisher:setanta": ("Setanta", "https://www.setantabooks.com", "", 21600),
}


def _due(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _allowed(config: Config, source: str) -> bool:
    enabled = {"charity": config.source_charity_shops, "specialist": config.source_specialist_shops,
               "publisher": config.source_publishers}.get(source, False)
    return config.production and config.allow_marketplace_network and enabled


def schedule_shopify(db: sqlite3.Connection, config: Config) -> int:
    count = 0
    with transaction(db):
        for route_id, (name, base, path, cadence) in ROUTES.items():
            source = route_id.split(":", 1)[0]
            if not _allowed(config, source):
                continue
            db.execute("INSERT OR IGNORE INTO sources(id,adapter,status,cadence_seconds) VALUES(?,?,'SCHEDULED',?)", (source, "shopify", cadence))
            db.execute("INSERT OR IGNORE INTO source_routes(id,source_id,lane,query_text) VALUES(?,?,?,?)", (route_id, source, "COLLECTION", name))
            route = db.execute("SELECT next_due_at,last_success_at FROM source_routes WHERE id=?", (route_id,)).fetchone()
            pending = db.execute("SELECT 1 FROM jobs WHERE route_id=? AND kind='SCAN_SHOPIFY' AND status IN ('PENDING','RUNNING')", (route_id,)).fetchone()
            if pending or (route["next_due_at"] and route["next_due_at"] > now()):
                continue
            window = f"{route_id}:{now()}:{secrets.token_hex(4)}"
            payload = {"route_id": route_id, "window_id": window, "page": 1, "baseline": route["last_success_at"] is None}
            if enqueue_job(db, f"shopify:{window}:1", "SCAN_SHOPIFY", route_id=route_id, priority=24, payload=payload):
                db.execute("UPDATE source_routes SET next_due_at=? WHERE id=?", (_due(cadence), route_id))
                count += 1
    return count


def fetch_page(route_id: str, page: int) -> dict:
    _, base, path, _ = ROUTES[route_id]
    endpoint = f"/{path}/products.json" if path else "/products.json"
    query = urllib.parse.urlencode({"limit": PAGE_SIZE, "page": page})
    request = urllib.request.Request(base + endpoint + "?" + query, headers={"Accept": "application/json", "User-Agent": "PhotobookRadar/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"Shopify returned HTTP {response.status}")
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise ValueError("Shopify page exceeded response limit")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise ValueError("Shopify products response is invalid")
    return payload


def parse_products(route_id: str, payload: dict) -> list[dict]:
    products = payload.get("products")
    if not isinstance(products, list) or len(products) > PAGE_SIZE:
        raise ValueError("Shopify product page is invalid")
    name, base, _, _ = ROUTES[route_id]
    source = route_id.split(":", 1)[0]
    rows = []
    seen = set()
    for product in products:
        if not isinstance(product, dict) or product.get("id") is None:
            raise ValueError("Shopify product lacks an ID")
        key = str(product["id"])
        if key in seen:
            raise ValueError("Shopify page repeats a product")
        seen.add(key)
        title = str(product.get("title") or "").strip()
        product_type = str(product.get("product_type") or "").casefold()
        if source == "publisher" and any(word in product_type for word in ("beans", "coffee", "apparel", "clothing", "t-shirt", "tote", "merch")):
            continue
        handle = str(product.get("handle") or "").strip()
        if not title or not handle or len(handle) > 250 or any(ord(char) < 32 or char in "/\\?#" for char in handle):
            raise ValueError("Shopify product lacks a safe title or handle")
        variants = product.get("variants")
        if not isinstance(variants, list):
            raise ValueError("Shopify product variants are invalid")
        prices = []
        available = False
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            available |= bool(variant.get("available"))
            try:
                prices.append(float(variant["price"]))
            except (ValueError, TypeError, KeyError):
                pass
        description = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(product.get("body_html") or "")))).strip()[:5000]
        rows.append({
            "key": f"{source if source == 'charity' else route_id}:{key}",
            "external_id": key, "title": title, "description": description,
            "url": f"{base}/products/{urllib.parse.quote(handle, safe='')}", "price_gbp": min(prices) if prices and source != "publisher" else None,
            "available": available, "published_at": product.get("published_at"),
            "vendor": product.get("vendor") or name, "source_name": name,
        })
    return rows


def run_shopify_job(db: sqlite3.Connection, job: sqlite3.Row, config: Config, *, fetch: Callable[[str, int], dict] = fetch_page) -> dict:
    payload = json.loads(job["payload_json"])
    route_id = payload.get("route_id")
    page = payload.get("page")
    if route_id not in ROUTES or not _allowed(config, route_id.split(":", 1)[0]) or job["route_id"] != route_id or not isinstance(page, int) or not 1 <= page <= MAX_PAGES:
        raise ValueError("Shopify job is invalid or disabled")
    durable = db.execute("SELECT last_durable_page FROM search_windows WHERE id=?", (payload["window_id"],)).fetchone()
    if durable and durable[0] >= page:
        return {"already_durable": True}
    with transaction(db):
        db.execute("UPDATE source_routes SET last_attempt_at=? WHERE id=?", (now(), route_id))
    raw = fetch(route_id, page)
    rows = parse_products(route_id, raw)
    full = len(raw["products"]) == PAGE_SIZE
    more = full and page < MAX_PAGES
    note = "Page limit reached; older stock may be missing" if full and not more else None
    next_job = None
    if more:
        next_job = {"key": f"shopify:{payload['window_id']}:{page + 1}", "kind": "SCAN_SHOPIFY", "route_id": route_id, "priority": 24,
                    "payload": {**payload, "page": page + 1}}
    ids = capture_page(db, source_id=route_id.split(":", 1)[0], route_id=route_id, window_id=payload["window_id"],
                 items=rows, page_number=page, continuation={"next_page": page + 1 if more else None}, complete=not more,
                 imported=bool(payload["baseline"]), followup_job=next_job, next_due_at=_due(ROUTES[route_id][3]) if not more else None,
                 coverage_note=note, lease_job_id=job["id"], lease_token=job["lease_token"])
    # Publisher products stay in the dashboard. A new product alone is not a
    # researched collector opportunity and must not trigger a phone alert.
    with transaction(db):
        db.execute("UPDATE sources SET status=?,last_error=NULL,last_success_at=? WHERE id=?", ("PARTIAL" if note else "ACTIVE" if not more else "BASELINING", now(), route_id.split(":", 1)[0]))
    return {"route_id": route_id, "page": page, "count": len(rows), "complete": not more}
