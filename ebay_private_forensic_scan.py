#!/usr/bin/env python3
"""Lossless one-off capture of the full eBay private-seller search library.

Unlike the normal monitor, this pass deliberately does not classify, score, or
discard search hits. Every summary returned by each exact recognition-library
query is stored with the library record that found it. The compressed chunks
can then be audited offline without spending more Browse API calls.
"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

import ebay_api
import ebay_private_full_library_scan as full_scan
import ebay_private_seller_backfill as backfill
import ebay_private_seller_monitor as live_monitor
import photobook_recognition as recognition

PRICE_CAP_GBP = 300.0
DEFAULT_MAX_CALLS = 400
DEFAULT_HARD_CALL_CAP = 400
DEFAULT_QUOTA_RESERVE = 250
PLAN_VERSION = 1
CHUNK_VERSION = 1

TARGET_FIELDS = {
    "Record ID": "record_id",
    "Contributor": "contributor",
    "Contributor aliases": "contributor_aliases",
    "Title": "title",
    "Title aliases": "title_aliases",
    "Year": "year",
    "Publisher": "publisher",
    "ISBN": "isbn",
    "Canon sources": "canon_sources",
    "Collectibility tier": "collectibility_tier",
    "Search priority": "search_priority",
    "Search tier": "search_tier",
    "First edition notes": "first_edition_notes",
    "Strong buy GBP": "strong_buy_gbp",
    "Bargain GBP": "bargain_gbp",
    "Evidence confidence": "evidence_confidence",
    "Documentary relevance": "documentary_relevance",
    "First monograph": "first_monograph",
    "Collector profile": "collector_profile",
    "Special edition priority": "special_edition_priority",
    "Collectible variants": "collectible_variants",
    "Awards and evidence": "awards_and_evidence",
    "Source": "source",
}

LISTING_FIELDS = (
    "key",
    "external_id",
    "rest_item_id",
    "title",
    "url",
    "price_gbp",
    "price_value",
    "price_currency",
    "shipping_value",
    "shipping_currency",
    "landed_price_gbp",
    "context",
    "vendor",
    "seller_feedback_percentage",
    "seller_feedback_score",
    "seller_account_type",
    "private_seller",
    "seller_type_confidence",
    "buying_options",
    "condition",
    "item_creation_date",
    "item_end_date",
    "image_url",
    "category_id",
    "category_path",
    "marketplace",
    "market_country",
)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return backfill.load_json(path, default, path.name)


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def target_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        output: _clean_scalar(row.get(source))
        for source, output in TARGET_FIELDS.items()
    }


def build_plan() -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for row in full_scan.ordered_records():
        query = recognition.search_query_for_record(row).strip()[:100]
        if not query:
            continue
        plan.append(
            {
                "query": query,
                "target": target_snapshot(row),
                "marketplace": "EBAY_GB",
                "market_country": "GB",
                "seller_filter_mode": "individual",
                "delivery_country": "GB",
                "price_currency": "GBP",
                "price_max": PRICE_CAP_GBP,
                "lane": "forensic_full_library_exact",
                "category_ids": None,
                "search_in_description": True,
                "buying_options": ["FIXED_PRICE", "BEST_OFFER", "AUCTION"],
                "offset": 0,
                "max_offset": 0,
                # Lowest landed-price candidates are the purpose of this
                # audit. This also prevents older bargains being hidden by a
                # newest-first page of routine listings.
                "sort": "price",
            }
        )
    return plan


def initialize_state(state: dict[str, Any], detected_at: str, *, restart: bool) -> bool:
    if isinstance(state.get("queue"), list) and not restart:
        state.setdefault("chunks", [])
        return False
    queue = build_plan()
    state.clear()
    state.update(
        {
            "version": 1,
            "plan_version": PLAN_VERSION,
            "created_at": detected_at,
            "initial_plan_size": len(queue),
            "queue": queue,
            "chunks": [],
            "complete": False,
            "total_calls": 0,
            "total_search_calls": 0,
            "total_results_captured": 0,
            "total_truncated_queries": 0,
        }
    )
    return True


def compact_listing(item: dict[str, Any]) -> dict[str, Any]:
    return {
        field: item[field]
        for field in LISTING_FIELDS
        if field in item and item[field] not in (None, "", [], {})
    }


def run_capture(
    client: ebay_api.EbayBrowseClient,
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    call_budget: int,
    detected_at: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    queue = state.get("queue")
    if not isinstance(queue, list):
        raise RuntimeError("Forensic scan queue is not a list")

    search_calls = 0
    successful_queries = 0
    results_captured = 0
    failures: list[str] = []
    searches: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []

    while queue and search_calls < max(0, int(call_budget)):
        step = queue.pop(0)
        search_calls += 1
        try:
            items, raw_count = backfill.search_page(client, config, step)
        except Exception as exc:
            failures.append(f"`{step.get('query')}`: {exc}")
            queue.append(step)
            continue

        successful_queries += 1
        results_captured += raw_count
        target = step.get("target") if isinstance(step.get("target"), dict) else {}
        searches.append(
            {
                "query": str(step.get("query") or ""),
                "target": target,
                "result_count": raw_count,
                "items": [compact_listing(item) for item in items],
            }
        )
        if raw_count == backfill.PAGE_SIZE:
            truncated.append(
                {
                    "query": str(step.get("query") or ""),
                    "record_id": str(target.get("record_id") or ""),
                    "result_count": raw_count,
                }
            )

    if search_calls and successful_queries == 0:
        raise RuntimeError("All attempted forensic eBay searches failed; no progress was saved")

    chunk: dict[str, Any] | None = None
    if searches:
        chunk_number = len(state.get("chunks") or []) + 1
        chunk_name = f"batch-{chunk_number:03d}.json.gz"
        chunk = {
            "version": CHUNK_VERSION,
            "chunk": chunk_number,
            "captured_at": detected_at,
            "query_count": len(searches),
            "result_count": results_captured,
            "truncated_query_count": len(truncated),
            "truncated_queries": truncated,
            "searches": searches,
        }
        state.setdefault("chunks", []).append(
            {
                "file": chunk_name,
                "captured_at": detected_at,
                "query_count": len(searches),
                "result_count": results_captured,
                "truncated_query_count": len(truncated),
            }
        )

    state["queue"] = queue
    state["complete"] = not queue
    state["last_run"] = detected_at
    state["last_run_calls"] = search_calls
    state["last_run_search_calls"] = search_calls
    state["last_run_successful_queries"] = successful_queries
    state["last_run_results_captured"] = results_captured
    state["last_run_failures"] = failures
    state["total_calls"] = int(state.get("total_calls") or 0) + search_calls
    state["total_search_calls"] = int(state.get("total_search_calls") or 0) + search_calls
    state["total_results_captured"] = int(state.get("total_results_captured") or 0) + results_captured
    state["total_truncated_queries"] = int(state.get("total_truncated_queries") or 0) + len(truncated)

    result = {
        "calls": search_calls,
        "search_calls": search_calls,
        "successful_queries": successful_queries,
        "results_captured": results_captured,
        "remaining_steps": len(queue),
        "complete": bool(state["complete"]),
        "chunk_file": str((state.get("chunks") or [{}])[-1].get("file") or "") if chunk else "",
        "truncated_queries": truncated,
        "failures": failures,
    }
    return result, chunk


def write_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--state", default="data/ebay_private_forensic_scan_state.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private-forensic")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--hard-call-cap", type=int, default=DEFAULT_HARD_CALL_CAP)
    parser.add_argument("--quota-reserve", type=int, default=DEFAULT_QUOTA_RESERVE)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if args.max_calls < 1 or args.hard_call_cap < 1:
        parser.error("call caps must be at least 1")
    if args.quota_reserve < 0:
        parser.error("--quota-reserve cannot be negative")

    config = live_monitor.load_config(Path(args.config))
    config["max_price_gbp"] = PRICE_CAP_GBP
    state = load_json(Path(args.state), {"version": 1})
    detected_at = live_monitor.utc_now()
    initialized = initialize_state(state, detected_at, restart=bool(args.restart))

    client = ebay_api.EbayBrowseClient(marketplace="EBAY_GB")
    call_budget, quota, quota_warning = backfill.api_call_budget(
        client,
        args.max_calls,
        hard_cap=args.hard_call_cap,
        quota_reserve=args.quota_reserve,
    )
    result, chunk = run_capture(
        client,
        config,
        state,
        call_budget=call_budget,
        detected_at=detected_at,
    )
    result["quota"] = quota
    result["api_call_budget"] = call_budget
    result["library_stats"] = recognition.library_stats()
    result["price_cap_gbp"] = PRICE_CAP_GBP
    if quota_warning:
        result["failures"].append(quota_warning)

    runtime = Path(args.runtime_dir)
    live_monitor.write_json(runtime / "proposed-state.json", state)
    live_monitor.write_json(runtime / "latest-snapshot.json", result)
    if chunk is not None:
        write_gzip_json(runtime / str(result["chunk_file"]), chunk)

    live_monitor.set_output("state_changed", "true" if initialized or result["calls"] else "false")
    live_monitor.set_output("remaining_steps", result["remaining_steps"])
    live_monitor.set_output("complete", "true" if result["complete"] else "false")
    live_monitor.set_output("calls", result["calls"])
    live_monitor.set_output("chunk_file", result["chunk_file"])
    print(
        f"Forensic capture: {result['calls']} searches, "
        f"{result['results_captured']} raw results preserved, "
        f"{result['remaining_steps']} title searches pending."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
