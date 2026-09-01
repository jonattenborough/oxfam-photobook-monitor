#!/usr/bin/env python3
"""Resumable international search for older collectible photobook listings.

European marketplaces use eBay's explicit individual-account filter. Markets
where that filter is unavailable are restricted to exact priority-title and
high-yield collection searches, then screened using seller feedback and live
account details. Listing IDs are shared across every market and with the UK
monitor so the same physical listing cannot be surfaced twice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ebay_api
import ebay_private_seller_backfill as backfill
import ebay_private_seller_monitor as live_monitor
import photobook_recognition as recognition

DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_SLICE_DAYS = 21
DEFAULT_MAX_CALLS = 60
DEFAULT_LIVE_CHECKS = 0
INITIAL_PRIORITY_RECORDS = 18
PLAN_VERSION = 1
INDIVIDUAL_FILTER_MARKETS = {
    "EBAY_AT",
    "EBAY_BE",
    "EBAY_CH",
    "EBAY_DE",
    "EBAY_ES",
    "EBAY_FR",
    "EBAY_IE",
    "EBAY_IT",
    "EBAY_PL",
}


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("International private-seller config must be a JSON object")
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        raise RuntimeError("International private-seller config needs a non-empty markets list")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in markets:
        if not isinstance(raw, dict):
            raise RuntimeError("Each international marketplace must be a JSON object")
        market = dict(raw)
        marketplace = str(market.get("marketplace") or "").upper()
        if not marketplace or marketplace in seen:
            raise RuntimeError(f"Invalid or duplicate marketplace: {marketplace or 'missing'}")
        seen.add(marketplace)
        mode = str(market.get("seller_filter_mode") or "individual").lower()
        if mode not in {"individual", "heuristic"}:
            raise RuntimeError(f"Unsupported seller_filter_mode for {marketplace}: {mode}")
        if mode == "individual" and marketplace not in INDIVIDUAL_FILTER_MARKETS:
            raise RuntimeError(f"The individual-account filter is not supported for {marketplace}")
        currency = str(market.get("currency") or "").upper()
        country = str(market.get("country") or "").upper()
        if len(currency) != 3 or len(country) != 2:
            raise RuntimeError(f"Invalid country or currency for {marketplace}")
        for key in ("broad_queries", "collectible_queries", "collection_queries"):
            values = market.get(key) or []
            if not isinstance(values, list):
                raise RuntimeError(f"{marketplace} {key} must be a list")
            market[key] = [str(value).strip() for value in values if str(value).strip()]
        market["marketplace"] = marketplace
        market["country"] = country
        market["currency"] = currency
        market["seller_filter_mode"] = mode
        market["gbp_rate"] = float(market.get("gbp_rate") or 0)
        market["price_max"] = float(market.get("price_max") or payload.get("max_price_gbp") or 750)
        market["issue_threshold"] = int(
            market.get("issue_threshold")
            or (payload.get("heuristic_issue_threshold") if mode == "heuristic" else payload.get("issue_threshold"))
            or 72
        )
        normalized.append(market)

    payload["markets"] = normalized
    payload["marketplace"] = normalized[0]["marketplace"]
    payload.setdefault("delivery_country", "GB")
    payload.setdefault("max_price_gbp", 750)
    payload.setdefault("issue_threshold", 72)
    payload.setdefault("heuristic_issue_threshold", 80)
    payload.setdefault("heuristic_seller_feedback_max", 1000)
    return payload


def _priority_records() -> list[dict[str, Any]]:
    rows = [
        row
        for row in recognition.load_library()
        if str(row.get("Search priority") or "9").strip() == "0"
    ]
    contemporary = [row for row in rows if live_monitor._is_contemporary_record(row)]
    classics = [row for row in rows if not live_monitor._is_contemporary_record(row)]
    contemporary.sort(key=backfill._curated_sort_key)
    classics.sort(key=backfill._classic_sort_key)
    return backfill._interleave(contemporary, classics)


def build_plan(
    config: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    *,
    slice_days: int = DEFAULT_SLICE_DAYS,
) -> list[dict[str, Any]]:
    start_stamp = live_monitor.utc_stamp(window_start)
    end_stamp = live_monitor.utc_stamp(window_end)
    plan: list[dict[str, Any]] = []

    def add(
        market: dict[str, Any],
        lane: str,
        query: str,
        *,
        start: str = start_stamp,
        end: str = end_stamp,
    ) -> None:
        query = str(query or "").strip()[:100]
        if not query:
            return
        plan.append(
            {
                "marketplace": market["marketplace"],
                "market_country": market["country"],
                "seller_filter_mode": market["seller_filter_mode"],
                "delivery_country": config["delivery_country"],
                "price_currency": market["currency"],
                "price_max": market["price_max"],
                "gbp_rate": market["gbp_rate"],
                "issue_threshold": market["issue_threshold"],
                "lane": lane,
                "query": query,
                "window_start": start,
                "window_end": end,
                "category_ids": None,
                "search_in_description": True,
                "buying_options": list(live_monitor.FIXED_BUYING_OPTIONS),
                "offset": 0,
            }
        )

    individual_markets = [
        market for market in config["markets"] if market["seller_filter_mode"] == "individual"
    ]
    heuristic_markets = [
        market for market in config["markets"] if market["seller_filter_mode"] == "heuristic"
    ]

    # Start with the searches most likely to expose seller ignorance, signed
    # editions, original prints and mixed collections in each local language.
    for market in individual_markets:
        for query in market["collectible_queries"]:
            add(market, "international_collectible", query)
        for query in market["collection_queries"]:
            add(market, "international_collection", query)
    for market in heuristic_markets:
        for query in market["collectible_queries"]:
            add(market, "international_heuristic_collectible", query)
        for query in market["collection_queries"]:
            add(market, "international_heuristic_collection", query)

    records = _priority_records()
    initial = records[:INITIAL_PRIORITY_RECORDS]
    remaining = records[INITIAL_PRIORITY_RECORDS:]
    for row in initial:
        query = recognition.search_query_for_record(row)
        for market in config["markets"]:
            add(market, "international_priority_exact", query)

    # Broad local-language discovery is limited to markets where eBay can
    # prove the seller is an individual. Short date slices prevent the newest
    # 200 results from hiding older inventory.
    for start, end in backfill.date_slices(window_start, window_end, slice_days):
        for market in individual_markets:
            for query in market["broad_queries"]:
                add(market, "international_broad", query, start=start, end=end)

    for row in remaining:
        query = recognition.search_query_for_record(row)
        for market in config["markets"]:
            add(market, "international_priority_exact", query)

    unique: list[dict[str, Any]] = []
    seen_steps: set[tuple[str, str, str, str, str, int]] = set()
    for step in plan:
        key = (
            str(step["marketplace"]),
            str(step["lane"]),
            " ".join(str(step["query"]).lower().split()),
            str(step["window_start"]),
            str(step["window_end"]),
            int(step["offset"]),
        )
        if key in seen_steps:
            continue
        seen_steps.add(key)
        unique.append(step)
    return unique


def initialize_state(
    state: dict[str, Any],
    config: dict[str, Any],
    *,
    detected_at: str,
    lookback_days: int,
    slice_days: int,
    new_window: bool,
) -> bool:
    if isinstance(state.get("queue"), list) and not new_window:
        state.setdefault("pending_live", {})
        state.setdefault("reviewed", {})
        return False
    detected = live_monitor._parse_stamp(detected_at) or datetime.now(timezone.utc)
    window_end = detected
    window_start = window_end - timedelta(days=max(1, min(int(lookback_days), 365)))
    queue = build_plan(config, window_start, window_end, slice_days=slice_days)
    state.clear()
    state.update(
        {
            "version": 1,
            "plan_version": PLAN_VERSION,
            "created_at": detected_at,
            "window_start": live_monitor.utc_stamp(window_start),
            "window_end": live_monitor.utc_stamp(window_end),
            "lookback_days": int(lookback_days),
            "slice_days": int(slice_days),
            "initial_plan_size": len(queue),
            "queue": queue,
            "pending_live": {},
            "reviewed": {},
            "complete": False,
            "total_calls": 0,
            "total_search_calls": 0,
            "total_live_checks": 0,
            "total_results_inspected": 0,
        }
    )
    return True


def combined_known_state(paths: list[Path]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in backfill.known_live_keys(payload):
            seen[key] = {"source": str(path)}
    return {"seen": seen}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_international_markets.json")
    parser.add_argument("--state", default="data/ebay_private_international_backfill_state.json")
    parser.add_argument("--findings", default="data/ebay_private_international_backfill_findings.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private-international-backfill")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--slice-days", type=int, default=DEFAULT_SLICE_DAYS)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--hard-call-cap", type=int, default=backfill.MAX_CALLS)
    parser.add_argument("--quota-reserve", type=int, default=backfill.QUOTA_RESERVE)
    parser.add_argument("--max-live-checks", type=int, default=DEFAULT_LIVE_CHECKS)
    parser.add_argument("--new-window", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.lookback_days <= 365:
        parser.error("--lookback-days must be between 1 and 365")
    if not 1 <= args.slice_days <= 30:
        parser.error("--slice-days must be between 1 and 30")
    if args.hard_call_cap < 1:
        parser.error("--hard-call-cap must be at least 1")
    if args.quota_reserve < 0:
        parser.error("--quota-reserve cannot be negative")

    config = load_config(Path(args.config))
    state = backfill.load_json(Path(args.state), {"version": 1}, "International backfill state")
    findings = backfill.load_json(
        Path(args.findings),
        {"version": 1, "items": {}},
        "International backfill findings",
    )
    detected_at = live_monitor.utc_now()
    initialized = initialize_state(
        state,
        config,
        detected_at=detected_at,
        lookback_days=args.lookback_days,
        slice_days=args.slice_days,
        new_window=bool(args.new_window),
    )
    known_state = combined_known_state(
        [
            Path("data/ebay_private_seller_state.json"),
            Path("data/ebay_private_seller_backfill_state.json"),
            Path("data/ebay_private_seller_backfill_findings.json"),
            Path("data/ebay_private_seller_review_history.json"),
        ]
    )

    clients = {
        market["marketplace"]: ebay_api.EbayBrowseClient(marketplace=market["marketplace"])
        for market in config["markets"]
    }
    quota_client = next(iter(clients.values()))
    call_budget, quota, quota_warning = backfill.api_call_budget(
        quota_client,
        args.max_calls,
        hard_cap=args.hard_call_cap,
        quota_reserve=args.quota_reserve,
    )
    result = backfill.run_backfill(
        clients,
        config,
        state,
        findings,
        known_state,
        call_budget=call_budget,
        max_live_checks=args.max_live_checks,
        detected_at=detected_at,
    )
    result["quota"] = quota
    result["api_call_budget"] = call_budget
    result["library_stats"] = recognition.library_stats()
    if quota_warning:
        result["failures"].append(quota_warning)

    runtime = Path(args.runtime_dir)
    live_monitor.write_json(runtime / "proposed-state.json", state)
    live_monitor.write_json(runtime / "proposed-findings.json", findings)
    live_monitor.write_json(runtime / "latest-snapshot.json", result)
    if result["new_candidates"]:
        title = (
            f"EBAY_PRIVATE_GLOBAL_BACKFILL: {len(result['new_candidates'])} live international "
            f"photobook candidate{'s' if len(result['new_candidates']) != 1 else ''}"
        )
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            backfill.issue_body(result, state, len(findings["items"])),
            encoding="utf-8",
        )

    live_monitor.set_output("new_count", len(result["new_candidates"]))
    live_monitor.set_output("state_changed", "true" if initialized or result["calls"] else "false")
    live_monitor.set_output("remaining_steps", result["remaining_steps"])
    live_monitor.set_output("complete", "true" if result["complete"] else "false")
    live_monitor.set_output("calls", result["calls"])
    print(
        f"International private backfill: {result['calls']} calls, "
        f"{result['results_inspected']} results, {len(result['new_candidates'])} live candidates, "
        f"{result['remaining_steps']} queued steps."
    )
    if quota_warning:
        print("WARNING:", quota_warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
