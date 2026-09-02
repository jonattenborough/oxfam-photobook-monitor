#!/usr/bin/env python3
"""One-off, quota-safe scan of every photobook recognition record on eBay UK.

Each of the 4,318 library records receives one current-stock search against
individual sellers. Results are deduplicated and scored locally before scarce
Browse calls are spent on mandatory live verification. Only the compact,
live-verified review queue is written to GitHub issue payloads.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import ebay_api
import ebay_private_seller_backfill as backfill
import ebay_private_seller_monitor as live_monitor
import parr_badger_runner as pb
import photobook_recognition as recognition

PRICE_CAP_GBP = 300.0
BASE_ISSUE_THRESHOLD = 60
PENDING_LIMIT = 5000
ISSUE_CHUNK_SIZE = 10
DEFAULT_MAX_CALLS = 400
DEFAULT_HARD_CALL_CAP = 400
DEFAULT_QUOTA_RESERVE = 250
PLAN_VERSION = 1


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return backfill.load_json(path, default, path.name)


def _priority(row: dict[str, Any]) -> int:
    value = str(row.get("Search priority") or "9").strip()
    return int(value) if value.isdigit() else 9


def _record_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    tier = str(row.get("Collectibility tier") or "C").upper()
    documentary = str(row.get("Documentary relevance") or "").upper()
    special = str(row.get("Special edition priority") or "").upper()
    first = str(row.get("First monograph") or "").upper()
    return (
        _priority(row),
        {"S": 0, "A": 1, "B": 2, "C": 3}.get(tier, 4),
        0 if documentary == "HIGH" else 1 if documentary == "MEDIUM" else 2,
        0 if special == "HIGH" else 1,
        0 if first == "YES" else 1,
        pb.normalize(row.get("Contributor")),
        pb.normalize(row.get("Title")),
    )


def ordered_records() -> list[dict[str, Any]]:
    rows = list(recognition.load_library())
    recent = sorted(
        (row for row in rows if live_monitor._is_contemporary_record(row)),
        key=_record_sort_key,
    )
    classic = sorted(
        (row for row in rows if not live_monitor._is_contemporary_record(row)),
        key=_record_sort_key,
    )
    return backfill._interleave(recent, classic)


def build_plan() -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for row in ordered_records():
        query = recognition.search_query_for_record(row).strip()[:100]
        if not query:
            continue
        plan.append(
            {
                "marketplace": "EBAY_GB",
                "market_country": "GB",
                "seller_filter_mode": "individual",
                "delivery_country": "GB",
                "price_currency": "GBP",
                "price_max": PRICE_CAP_GBP,
                "issue_threshold": BASE_ISSUE_THRESHOLD,
                "price_review_profile": "jon_hidden_gem",
                "lane": "full_library_exact",
                "query": query,
                # Exact author/title searches can surface books filed outside
                # eBay's Books category. Description search catches weak titles.
                "category_ids": None,
                "search_in_description": True,
                "buying_options": ["FIXED_PRICE", "BEST_OFFER", "AUCTION"],
                "offset": 0,
                # One query per library record is the promised 4,318-book pass.
                # Popular titles are not allowed to consume another title's call.
                "max_offset": 0,
            }
        )
    return plan


def initialize_state(state: dict[str, Any], detected_at: str, *, restart: bool) -> bool:
    if isinstance(state.get("queue"), list) and not restart:
        state.setdefault("pending_live", {})
        state.setdefault("reviewed", {})
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


def _landed_price(item: dict[str, Any]) -> float:
    raw = item.get("landed_price_gbp")
    if raw is None:
        raw = item.get("price_gbp")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 999999.0


def _candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    price = _landed_price(item)
    score = int(item.get("opportunity_score") or 0)
    return (
        0 if price <= 100 else 1 if price <= 200 else 2,
        -score,
        price,
        pb.normalize(item.get("title")),
    )


def write_issue_payloads(
    runtime: Path,
    candidates: list[dict[str, Any]],
    *,
    detected_at: str,
    stats: dict[str, Any],
    result: dict[str, Any],
) -> int:
    issues_dir = runtime / "issues"
    if issues_dir.exists():
        shutil.rmtree(issues_dir)
    issues_dir.mkdir(parents=True, exist_ok=True)
    ranked = sorted(candidates, key=_candidate_sort_key)
    chunks = [ranked[index:index + ISSUE_CHUNK_SIZE] for index in range(0, len(ranked), ISSUE_CHUNK_SIZE)]
    for index, chunk in enumerate(chunks, start=1):
        top_score = max(int(item.get("opportunity_score") or 0) for item in chunk)
        title = (
            f"EBAY_PRIVATE_NEW: FULL LIBRARY {index}/{len(chunks)} | "
            f"{len(chunk)} candidates | top {top_score}"
        )
        intro = "\n".join(
            [
                "## One-off full recognition-library sweep",
                "",
                "This batch comes from the exact 4,318-title UK private-seller scan.",
                "All API results were scored locally. Only live-verified candidates are included here.",
                "Sub-£100 leads use the broadest review gate; £100 to £300 listings require progressively stronger evidence.",
                "",
            ]
        )
        body = intro + live_monitor.make_issue_body(
            chunk,
            detected_at=detected_at,
            stats=stats,
            failures=[],
            urgent_threshold=90,
        )
        stem = f"issue-{index:03d}"
        (issues_dir / f"{stem}.title").write_text(title[:240] + "\n", encoding="utf-8")
        (issues_dir / f"{stem}.md").write_text(body, encoding="utf-8")
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--state", default="data/ebay_private_full_library_scan_state.json")
    parser.add_argument("--findings", default="data/ebay_private_full_library_scan_findings.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private-full-library")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--hard-call-cap", type=int, default=DEFAULT_HARD_CALL_CAP)
    parser.add_argument("--quota-reserve", type=int, default=DEFAULT_QUOTA_RESERVE)
    parser.add_argument("--max-live-checks", type=int, default=0)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if args.max_calls < 1 or args.hard_call_cap < 1:
        parser.error("call caps must be at least 1")
    if args.quota_reserve < 0:
        parser.error("--quota-reserve cannot be negative")

    config = live_monitor.load_config(Path(args.config))
    config["max_price_gbp"] = PRICE_CAP_GBP
    config["issue_threshold"] = BASE_ISSUE_THRESHOLD
    config["pending_limit"] = PENDING_LIMIT
    state = load_json(Path(args.state), {"version": 1})
    findings = load_json(Path(args.findings), {"version": 1, "items": {}})
    detected_at = live_monitor.utc_now()
    initialized = initialize_state(state, detected_at, restart=bool(args.restart))
    known_state = combined_known_state(
        [
            Path("data/ebay_private_seller_state.json"),
            Path("data/ebay_private_seller_backfill_state.json"),
            Path("data/ebay_private_seller_backfill_findings.json"),
            Path("data/ebay_private_international_backfill_state.json"),
            Path("data/ebay_private_international_backfill_findings.json"),
            Path("data/ebay_private_seller_review_history.json"),
        ]
    )

    client = ebay_api.EbayBrowseClient(marketplace="EBAY_GB")
    call_budget, quota, quota_warning = backfill.api_call_budget(
        client,
        args.max_calls,
        hard_cap=args.hard_call_cap,
        quota_reserve=args.quota_reserve,
    )
    result = backfill.run_backfill(
        client,
        config,
        state,
        findings,
        known_state,
        call_budget=call_budget,
        max_live_checks=args.max_live_checks,
        detected_at=detected_at,
    )
    stats = recognition.library_stats()
    result["quota"] = quota
    result["api_call_budget"] = call_budget
    result["library_stats"] = stats
    result["price_cap_gbp"] = PRICE_CAP_GBP
    if quota_warning:
        result["failures"].append(quota_warning)

    runtime = Path(args.runtime_dir)
    live_monitor.write_json(runtime / "proposed-state.json", state)
    live_monitor.write_json(runtime / "proposed-findings.json", findings)
    live_monitor.write_json(runtime / "latest-snapshot.json", result)
    issue_count = write_issue_payloads(
        runtime,
        result["new_candidates"],
        detected_at=detected_at,
        stats=stats,
        result=result,
    )

    live_monitor.set_output("new_count", len(result["new_candidates"]))
    live_monitor.set_output("issue_count", issue_count)
    live_monitor.set_output("state_changed", "true" if initialized or result["calls"] else "false")
    live_monitor.set_output("remaining_steps", result["remaining_steps"])
    live_monitor.set_output("complete", "true" if result["complete"] else "false")
    live_monitor.set_output("calls", result["calls"])
    print(
        f"Full-library private scan: {result['calls']} calls, "
        f"{result['search_calls']} searches, {result['live_checks']} live checks, "
        f"{result['results_inspected']} results, {len(result['new_candidates'])} review candidates, "
        f"{result['remaining_steps']} title searches and {result['pending_live']} live checks pending."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
