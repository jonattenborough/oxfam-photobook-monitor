#!/usr/bin/env python3
"""Read-only grouped charity-seller Browse API probe.

This experiment measures whether several monitored charity sellers can share one
Browse search safely enough to improve revisit frequency. It never writes
production state, creates issues, or changes seller watermarks.

Only initialized sellers are eligible. Sellers are grouped by marketplace and
UK-delivery context. Each group uses the oldest member checkpoint, so no seller
can lose listings merely because another seller was checked more recently.
A full final probe page is reported as DENSE rather than treated as complete.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ebay_api
import ebay_seller_monitor as seller_monitor


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def eligible_groups(
    sellers: list[dict[str, str]],
    state: dict[str, Any],
    *,
    group_size: int,
) -> list[list[dict[str, str]]]:
    if group_size < 2:
        raise ValueError("group_size must be at least 2")
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    seller_state = state.get("sellers") if isinstance(state.get("sellers"), dict) else {}
    for seller in sellers:
        key = seller_monitor.seller_key(seller["marketplace"], seller["id"])
        previous = seller_state.get(key)
        if not isinstance(previous, dict) or not previous.get("initialized"):
            continue
        buckets[(seller["marketplace"], seller.get("delivery_country", ""))].append(seller)

    groups: list[list[dict[str, str]]] = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda row: row["id"].casefold())
        groups.extend(rows[index:index + group_size] for index in range(0, len(rows), group_size))
    return [group for group in groups if group]


def group_incremental_start(group: list[dict[str, str]], state: dict[str, Any]) -> str | None:
    seller_state = state.get("sellers") if isinstance(state.get("sellers"), dict) else {}
    stamps: list[datetime] = []
    for seller in group:
        key = seller_monitor.seller_key(seller["marketplace"], seller["id"])
        previous = seller_state.get(key)
        if not isinstance(previous, dict):
            return None
        parsed = seller_monitor._parse_stamp(previous.get("last_successful_fetch"))
        if parsed is None:
            return None
        stamps.append(parsed)
    if not stamps:
        return None
    # Apply the monitor's overlap to the oldest member checkpoint.
    return seller_monitor.incremental_start(min(stamps).isoformat())


def scan_group(
    group: list[dict[str, str]],
    state: dict[str, Any],
    *,
    max_pages: int,
    page_size: int = seller_monitor.PAGE_SIZE,
) -> dict[str, Any]:
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    marketplace = group[0]["marketplace"]
    delivery_country = group[0].get("delivery_country")
    if any(row["marketplace"] != marketplace or row.get("delivery_country") != delivery_country for row in group):
        raise ValueError("group must share marketplace and delivery context")

    start = group_incremental_start(group, state)
    if start is None:
        raise ValueError("all grouped sellers must have initialized successful checkpoints")

    client = ebay_api.EbayBrowseClient(marketplace=marketplace)
    seller_ids = [row["id"] for row in group]
    counts: dict[str, int] = {seller.casefold(): 0 for seller in seller_ids}
    unknown_seller_rows = 0
    pages = 0
    last_page_count = 0

    for page in range(max_pages):
        rows = client.search(
            None,
            limit=page_size,
            offset=page * page_size,
            category_ids=seller_monitor.BOOKS_CATEGORY_ID,
            fixed_price_only=True,
            seller_ids=seller_ids,
            delivery_country=delivery_country,
            item_start_date=start,
        )
        pages += 1
        last_page_count = len(rows)
        for raw in rows:
            seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
            username = str(seller.get("username") or "").casefold()
            if username in counts:
                counts[username] += 1
            else:
                unknown_seller_rows += 1
        if len(rows) < page_size:
            break

    dense = pages == max_pages and last_page_count == page_size
    return {
        "marketplace": marketplace,
        "delivery_country": delivery_country,
        "seller_ids": seller_ids,
        "seller_count": len(seller_ids),
        "incremental_start": start,
        "pages": pages,
        "browse_calls": client.browse_calls,
        "rows": sum(counts.values()) + unknown_seller_rows,
        "rows_by_seller": counts,
        "unknown_seller_rows": unknown_seller_rows,
        "dense": dense,
        "complete_within_probe": not dense,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("data/ebay_sellers.json"))
    parser.add_argument("--state", type=Path, default=Path("data/ebay_seller_state.json"))
    parser.add_argument("--group-size", type=int, default=8)
    parser.add_argument("--groups", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("runtime/charity-group-probe.json"))
    args = parser.parse_args()

    sellers = seller_monitor.load_config(args.config)
    state = seller_monitor.load_state(args.state)
    groups = eligible_groups(sellers, state, group_size=args.group_size)[: max(0, args.groups)]
    results: list[dict[str, Any]] = []
    for group in groups:
        results.append(scan_group(group, state, max_pages=args.max_pages))

    payload = {
        "checked_at": utc_now(),
        "read_only": True,
        "production_state_changed": False,
        "group_size": args.group_size,
        "max_pages": args.max_pages,
        "groups_available": len(eligible_groups(sellers, state, group_size=args.group_size)),
        "groups_probed": len(results),
        "sellers_probed": sum(row["seller_count"] for row in results),
        "browse_calls": sum(row["browse_calls"] for row in results),
        "dense_groups": sum(1 for row in results if row["dense"]),
        "groups": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
