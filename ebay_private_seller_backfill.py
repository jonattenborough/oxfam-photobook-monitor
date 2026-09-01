#!/usr/bin/env python3
"""Quota-safe historical search of active eBay UK private-seller listings.

This is deliberately isolated from the hourly new-listing monitor. It searches
active stock created inside a fixed historical window, stores its own progress
and findings, and surfaces only candidates freshly confirmed by eBay's live
item endpoint.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ebay_api
import ebay_private_seller_monitor as live_monitor
import parr_badger_runner as pb
import photobook_recognition as recognition

PAGE_SIZE = 200
MAX_OFFSET = 9800
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SLICE_DAYS = 7
DEFAULT_MAX_CALLS = 60
DEFAULT_LIVE_CHECKS = 0
MAX_CALLS = 120
QUOTA_RESERVE = 1000
UNKNOWN_QUOTA_CAP = 20
PENDING_LIMIT = 250
ISSUE_ITEM_LIMIT = 30
INITIAL_PRIORITY_BATCH = 24
PLAN_VERSION = 2


def load_json(path: Path, default: dict[str, Any], label: str) -> dict[str, Any]:
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} is not a JSON object")
    return payload


def api_call_budget(
    client: ebay_api.EbayBrowseClient,
    requested_calls: int,
) -> tuple[int, dict[str, Any] | None, str | None]:
    requested = max(0, min(int(requested_calls), MAX_CALLS))
    try:
        quota = client.browse_quota()
    except Exception as exc:
        fallback = min(requested, UNKNOWN_QUOTA_CAP)
        return fallback, None, f"Browse quota lookup failed; limiting this backfill to {fallback} calls: {exc}"
    usable = max(0, int(quota.get("remaining") or 0) - QUOTA_RESERVE)
    return min(requested, usable), quota, None


def live_check_reserve(
    pending_count: int,
    queue_count: int,
    call_budget: int,
    requested: int,
) -> int:
    """Allocate backfill calls toward the current bottleneck.

    A positive requested value remains an explicit cap. Zero enables the ROI
    allocator, which spends more calls verifying a full candidate queue and
    more calls searching when there is little waiting for verification.
    """
    budget = max(0, int(call_budget))
    if requested > 0:
        return min(int(requested), budget)
    pending = max(0, int(pending_count))
    if pending and queue_count <= 0:
        return budget
    if pending >= 200:
        return min(40, budget)
    if pending >= 100:
        return min(30, budget)
    if pending >= 40:
        return min(20, budget)
    return min(12, max(0, budget // 5))


def date_slices(start: datetime, end: datetime, slice_days: int) -> list[tuple[str, str]]:
    if end <= start:
        raise ValueError("Backfill window end must be after its start")
    days = max(1, int(slice_days))
    slices: list[tuple[str, str]] = []
    cursor = end
    while cursor > start:
        segment_start = max(start, cursor - timedelta(days=days))
        slices.append((live_monitor.utc_stamp(segment_start), live_monitor.utc_stamp(cursor)))
        cursor = segment_start
    return slices


def _curated_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    profile = str(row.get("Collector profile") or "").lower()
    british_fit = any(term in profile for term in ("british", "ireland", "working class", "community"))
    special = str(row.get("Special edition priority") or "").upper()
    tier = str(row.get("Collectibility tier") or "").upper()
    priority = str(row.get("Search priority") or "9")
    return (
        0 if british_fit else 1,
        0 if special == "HIGH" else 1,
        {"S": 0, "A": 1, "B": 2}.get(tier, 3),
        int(priority) if priority.isdigit() else 9,
        pb.normalize(row.get("Contributor")),
        pb.normalize(row.get("Title")),
    )


def _classic_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    tier = str(row.get("Collectibility tier") or "").upper()
    priority = str(row.get("Search priority") or "9")
    return (
        {"S": 0, "A": 1, "B": 2}.get(tier, 3),
        int(priority) if priority.isdigit() else 9,
        pb.normalize(row.get("Contributor")),
        pb.normalize(row.get("Title")),
    )


def _interleave(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for index in range(max(len(left), len(right))):
        if index < len(left):
            combined.append(left[index])
        if index < len(right):
            combined.append(right[index])
    return combined


def build_backfill_plan(
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
        lane: str,
        query: str,
        *,
        start: str = start_stamp,
        end: str = end_stamp,
        category_ids: str | None = live_monitor.BOOKS_CATEGORY_ID,
        description: bool = True,
        offset: int = 0,
    ) -> None:
        query = str(query or "").strip()[:100]
        if not query:
            return
        plan.append(
            {
                "lane": lane,
                "query": query,
                "window_start": start,
                "window_end": end,
                "category_ids": category_ids,
                "search_in_description": description,
                "buying_options": list(live_monitor.FIXED_BUYING_OPTIONS),
                "offset": max(0, int(offset)),
            }
        )

    # Low-volume, high-yield lanes can search the complete window in one call.
    for query in config["collectible_queries"]:
        add("collectible_format", query)
    for query in config["collection_queries"]:
        add("collection", query)
    for query in config["wrong_category_queries"]:
        add("wrong_category", query, category_ids=None)

    curated = [
        row
        for row in recognition.load_library()
        if "curated contemporary documentary" in str(row.get("Canon sources") or "").lower()
    ]
    classics = [
        row
        for row in recognition.load_library()
        if "curated contemporary documentary" not in str(row.get("Canon sources") or "").lower()
        and str(row.get("Search priority") or "9").strip() == "0"
    ]
    curated.sort(key=_curated_sort_key)
    classics.sort(key=_classic_sort_key)
    priority_records = _interleave(curated, classics)
    first_batch = priority_records[:INITIAL_PRIORITY_BATCH]
    remaining_priority = priority_records[INITIAL_PRIORITY_BATCH:]
    for row in first_batch:
        lane = "contemporary_exact" if live_monitor._is_contemporary_record(row) else "classic_exact"
        add(lane, recognition.search_query_for_record(row))

    # Broad queries are split into short creation-date windows so a busy query
    # cannot hide older results behind eBay's 200-result page limit.
    for start, end in date_slices(window_start, window_end, slice_days):
        for query in config["broad_queries"]:
            add("broad", query, start=start, end=end)

    # The first bounded run reaches both recent documentary priorities and
    # classic must-haves before broad slices. The remaining two groups then
    # continue in an interleaved rotation.
    for row in remaining_priority:
        lane = "contemporary_exact" if live_monitor._is_contemporary_record(row) else "classic_exact"
        add(lane, recognition.search_query_for_record(row))

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, int]] = set()
    for step in plan:
        key = (
            str(step["lane"]),
            " ".join(str(step["query"]).lower().split()),
            str(step["window_start"]),
            str(step["window_end"]),
            int(step["offset"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return unique


def migrate_legacy_plan(state: dict[str, Any], config: dict[str, Any]) -> bool:
    """Add classic priority searches to a backfill created by plan version 1."""
    if int(state.get("plan_version") or 1) >= PLAN_VERSION:
        return False
    queue = state.get("queue")
    window_start = live_monitor._parse_stamp(state.get("window_start"))
    window_end = live_monitor._parse_stamp(state.get("window_end"))
    if not isinstance(queue, list) or window_start is None or window_end is None:
        return False
    existing = {
        (
            str(step.get("lane") or ""),
            pb.normalize(step.get("query")),
            str(step.get("window_start") or ""),
            str(step.get("window_end") or ""),
        )
        for step in queue
        if isinstance(step, dict)
    }
    classic_steps = [
        step
        for step in build_backfill_plan(
            config,
            window_start,
            window_end,
            slice_days=int(state.get("slice_days") or DEFAULT_SLICE_DAYS),
        )
        if step["lane"] == "classic_exact"
        and (
            str(step["lane"]),
            pb.normalize(step["query"]),
            str(step["window_start"]),
            str(step["window_end"]),
        ) not in existing
    ]
    state["plan_version"] = PLAN_VERSION
    if not classic_steps:
        return True
    state["queue"] = classic_steps + queue
    state["initial_plan_size"] = int(state.get("initial_plan_size") or len(queue)) + len(classic_steps)
    state["complete"] = False
    state["migration_note"] = f"Added {len(classic_steps)} classic priority searches without restarting the window"
    return True


def initialize_state(
    state: dict[str, Any],
    config: dict[str, Any],
    live_state: dict[str, Any],
    *,
    detected_at: str,
    lookback_days: int,
    slice_days: int,
    new_window: bool,
) -> bool:
    existing_queue = state.get("queue")
    if isinstance(existing_queue, list) and not new_window:
        state.setdefault("pending_live", {})
        return migrate_legacy_plan(state, config)

    detected = live_monitor._parse_stamp(detected_at) or datetime.now(timezone.utc)
    live_boundary = live_monitor._parse_stamp(live_state.get("last_run")) or detected
    window_end = min(detected, live_boundary)
    window_start = window_end - timedelta(days=max(1, min(int(lookback_days), 365)))
    queue = build_backfill_plan(config, window_start, window_end, slice_days=slice_days)
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
            "complete": False,
            "total_calls": 0,
            "total_search_calls": 0,
            "total_live_checks": 0,
            "total_results_inspected": 0,
        }
    )
    return True


def known_live_keys(live_state: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("seen", "pending_live"):
        values = live_state.get(field)
        if isinstance(values, dict):
            keys.update(str(key) for key in values)
    return keys


def search_page(
    client: ebay_api.EbayBrowseClient,
    config: dict[str, Any],
    step: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    rows = client.search(
        str(step["query"]),
        limit=PAGE_SIZE,
        offset=int(step.get("offset") or 0),
        category_ids=step.get("category_ids"),
        fixed_price_only=False,
        buying_options=step.get("buying_options") or live_monitor.FIXED_BUYING_OPTIONS,
        seller_account_type="INDIVIDUAL",
        delivery_country=str(config["delivery_country"]),
        item_start_date=str(step["window_start"]),
        item_end_date=str(step["window_end"]),
        search_in_description=bool(step.get("search_in_description")),
        price_max=float(config["max_price_gbp"]),
        price_currency="GBP",
    )
    source = {
        "id": f"ebay_private_backfill_{step['lane']}",
        "name": f"eBay UK private sellers historical - {step['lane']}",
        "marketplace": config["marketplace"],
    }
    items: list[dict[str, Any]] = []
    for raw in rows:
        item = ebay_api.listing_from_summary(raw, source)
        if item is None:
            continue
        item["private_seller"] = True
        item["seller_account_type"] = item.get("seller_account_type") or "INDIVIDUAL"
        item["search_lane"] = str(step["lane"])
        item["search_query"] = str(step["query"])
        item["backfill_window_start"] = str(step["window_start"])
        item["backfill_window_end"] = str(step["window_end"])
        items.append(item)
    return items, len(rows)


def _merge_discovery(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    lanes = set(str(current.get("search_lane") or "").split("+"))
    lanes.update(str(incoming.get("search_lane") or "").split("+"))
    current["search_lane"] = "+".join(sorted(lane for lane in lanes if lane))


def run_backfill(
    client: ebay_api.EbayBrowseClient,
    config: dict[str, Any],
    state: dict[str, Any],
    findings: dict[str, Any],
    live_state: dict[str, Any],
    *,
    call_budget: int,
    max_live_checks: int,
    detected_at: str,
) -> dict[str, Any]:
    queue = state.get("queue")
    if not isinstance(queue, list):
        raise RuntimeError("Private backfill queue is not a list")
    findings_items = findings.setdefault("items", {})
    if not isinstance(findings_items, dict):
        raise RuntimeError("Private backfill findings items is not an object")
    issue_threshold = int(config["issue_threshold"])
    for key in list(findings_items):
        item = findings_items.get(key)
        if not isinstance(item, dict):
            findings_items.pop(key, None)
            continue
        refreshed = live_monitor.classify(item)
        if int(refreshed.get("opportunity_score") or 0) < issue_threshold:
            findings_items.pop(key, None)
            continue
        findings_items[key] = refreshed
    pending = state.setdefault("pending_live", {})
    if not isinstance(pending, dict):
        raise RuntimeError("Private backfill pending_live is not an object")

    minimum_live_score = max(55, issue_threshold - 12)
    known_keys = known_live_keys(live_state) | {str(key) for key in findings_items}
    for key in list(pending):
        if str(key) in known_keys:
            pending.pop(key, None)
            continue
        item = pending.get(key)
        if not isinstance(item, dict):
            pending.pop(key, None)
            continue
        refreshed = live_monitor.classify(item)
        if int(refreshed.get("opportunity_score") or 0) < minimum_live_score:
            pending.pop(key, None)
            continue
        refreshed["backfill_first_found"] = item.get("backfill_first_found") or detected_at
        pending[key] = refreshed
    reserved_live = live_check_reserve(
        len(pending),
        len(queue),
        call_budget,
        max_live_checks,
    )
    search_allowance = max(0, int(call_budget) - reserved_live)
    search_calls = 0
    live_calls = 0
    successful_queries = 0
    results_inspected = 0
    raw_by_key: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    truncated_pages = 0

    while queue and search_calls < search_allowance:
        step = queue.pop(0)
        search_calls += 1
        try:
            items, raw_count = search_page(client, config, step)
        except Exception as exc:
            failures.append(f"{step.get('lane')} `{step.get('query')}`: {exc}")
            queue.append(step)
            continue
        successful_queries += 1
        results_inspected += raw_count
        for item in items:
            key = str(item.get("key") or "")
            if not key or key in known_keys:
                continue
            if key in raw_by_key:
                _merge_discovery(raw_by_key[key], item)
            else:
                raw_by_key[key] = item

        offset = int(step.get("offset") or 0)
        if raw_count == PAGE_SIZE and offset < MAX_OFFSET:
            continuation = dict(step)
            continuation["offset"] = offset + PAGE_SIZE
            queue.append(continuation)
            truncated_pages += 1

    if search_calls and successful_queries == 0:
        raise RuntimeError("All attempted private-seller backfill searches failed; progress was not advanced")

    for item in raw_by_key.values():
        classified = live_monitor.classify(item)
        if int(classified.get("opportunity_score") or 0) >= minimum_live_score:
            classified["backfill_first_found"] = detected_at
            pending[str(classified["key"])] = classified

    ranked_pending = sorted(
        (item for item in pending.values() if isinstance(item, dict)),
        key=lambda item: int(item.get("opportunity_score") or 0),
        reverse=True,
    )
    available_live_calls = max(0, int(call_budget) - search_calls)
    live_pool = ranked_pending[: min(reserved_live, available_live_calls)]
    new_candidates: list[dict[str, Any]] = []
    for item in live_pool:
        key = str(item.get("key") or "")
        rest_item_id = str(item.get("rest_item_id") or "")
        if not key or not rest_item_id:
            pending.pop(key, None)
            continue
        live_calls += 1
        try:
            is_live, _reason, detail = client.live_status(rest_item_id)
        except Exception as exc:
            failures.append(f"live-check {item.get('external_id')}: {exc}")
            continue
        if not is_live:
            pending.pop(key, None)
            continue
        enriched = live_monitor._merge_live_detail(item, detail)
        refreshed = live_monitor.classify(enriched)
        refreshed["live_verified"] = True
        refreshed["live_verified_at"] = detected_at
        refreshed["backfill_first_found"] = item.get("backfill_first_found") or detected_at
        pending.pop(key, None)
        if (
            int(refreshed.get("opportunity_score") or 0) >= int(config["issue_threshold"])
            and refreshed.get("private_seller") is True
            and str(refreshed.get("seller_account_type") or "").upper() != "BUSINESS"
        ):
            findings_items[key] = refreshed
            new_candidates.append(refreshed)

    ranked_remaining = sorted(
        ((str(key), item) for key, item in pending.items() if isinstance(item, dict)),
        key=lambda pair: int(pair[1].get("opportunity_score") or 0),
        reverse=True,
    )[:PENDING_LIMIT]
    state["pending_live"] = dict(ranked_remaining)
    state["queue"] = queue
    state["complete"] = not queue and not state["pending_live"]
    state["last_run"] = detected_at
    state["last_run_calls"] = search_calls + live_calls
    state["last_run_search_calls"] = search_calls
    state["last_run_live_checks"] = live_calls
    state["last_run_results_inspected"] = results_inspected
    state["last_run_failures"] = failures
    state["total_calls"] = int(state.get("total_calls") or 0) + search_calls + live_calls
    state["total_search_calls"] = int(state.get("total_search_calls") or 0) + search_calls
    state["total_live_checks"] = int(state.get("total_live_checks") or 0) + live_calls
    state["total_results_inspected"] = int(state.get("total_results_inspected") or 0) + results_inspected
    findings["items"] = findings_items
    findings["last_updated"] = detected_at
    return {
        "calls": search_calls + live_calls,
        "search_calls": search_calls,
        "live_checks": live_calls,
        "live_check_budget": reserved_live,
        "successful_queries": successful_queries,
        "results_inspected": results_inspected,
        "unique_unseen_results": len(raw_by_key),
        "new_candidates": new_candidates,
        "pending_live": len(state["pending_live"]),
        "remaining_steps": len(queue),
        "complete": bool(state["complete"]),
        "truncated_pages_requeued": truncated_pages,
        "failures": failures,
    }


def _price(item: dict[str, Any]) -> str:
    value = item.get("price_value")
    currency = str(item.get("price_currency") or "")
    if not isinstance(value, (int, float)):
        return "Not supplied"
    return f"£{value:.2f}" if currency == "GBP" else f"{currency} {value:.2f}".strip()


def issue_body(result: dict[str, Any], state: dict[str, Any], total_findings: int) -> str:
    candidates = sorted(
        result["new_candidates"],
        key=lambda item: int(item.get("opportunity_score") or 0),
        reverse=True,
    )
    shown = candidates[:ISSUE_ITEM_LIMIT]
    lines = [
        "## Historical private-seller photobook review",
        "",
        "These are active older listings from a bounded backfill, not newly listed stock. They are isolated from the normal new-listing alert state.",
        "",
        f"- Historical window: **{state.get('window_start')} to {state.get('window_end')}**",
        f"- Browse calls used: **{result['calls']}**",
        f"- Search calls: **{result['search_calls']}**",
        f"- Live-verification calls: **{result['live_checks']}**",
        f"- Search results inspected: **{result['results_inspected']}**",
        f"- Search steps still queued: **{result['remaining_steps']}**",
        f"- New live-verified candidates: **{len(candidates)}**",
        f"- Total retained historical candidates: **{total_findings}**",
        "",
        "Every item below was re-fetched immediately before this issue was created. Exact edition, completeness, condition, delivery cost and current comparable value still require final verification.",
        "",
    ]
    for item in shown:
        best = item.get("best_recognition") if isinstance(item.get("best_recognition"), dict) else {}
        lines.extend(
            [
                f"### {int(item.get('opportunity_score') or 0)}/100 - {item.get('title') or 'Untitled listing'}",
                "",
                f"- **Observed price:** {_price(item)}",
                f"- **Seller:** {item.get('vendor') or 'eBay individual account'}",
                f"- **Collector lane:** {item.get('collecting_lane') or 'open discovery'}",
                f"- **Opportunity type:** {item.get('opportunity_kind') or 'review lead'}",
                f"- **Discovery lane:** {item.get('search_lane') or 'unknown'}",
                f"- **Why it surfaced:** {', '.join(item.get('opportunity_reasons') or [])}",
                f"- **Listing:** {item.get('url')}",
            ]
        )
        if best:
            lines.append(
                f"- **Recognition:** {best.get('contributor')}, *{best.get('title')}* | "
                f"tier {best.get('collectibility_tier') or '?'} | match {best.get('score')}/100"
            )
            if best.get("collectible_variants"):
                lines.append(f"- **Known collectible variants:** {best.get('collectible_variants')}")
            if best.get("edition_status"):
                detail = "; ".join(str(value) for value in best.get("edition_reasons") or [])
                lines.append(
                    f"- **Edition evidence:** {best.get('edition_status')}"
                    + (f" | {detail}" if detail else "")
                )
        if item.get("description"):
            excerpt = " ".join(str(item.get("description") or "").split())[:700]
            lines.append(f"- **Live description excerpt:** {excerpt}")
        lines.append("")
    if len(candidates) > len(shown):
        lines.append(f"A further **{len(candidates) - len(shown)}** candidates are retained in the findings file.")
        lines.append("")
    if result["failures"]:
        lines.extend(["### Temporary warnings", ""])
        lines.extend(f"- {failure}" for failure in result["failures"][:20])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--live-state", default="data/ebay_private_seller_state.json")
    parser.add_argument("--state", default="data/ebay_private_seller_backfill_state.json")
    parser.add_argument("--findings", default="data/ebay_private_seller_backfill_findings.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private-backfill")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--slice-days", type=int, default=DEFAULT_SLICE_DAYS)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--max-live-checks", type=int, default=DEFAULT_LIVE_CHECKS)
    parser.add_argument("--new-window", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.lookback_days <= 365:
        parser.error("--lookback-days must be between 1 and 365")
    if not 1 <= args.slice_days <= 30:
        parser.error("--slice-days must be between 1 and 30")

    config = live_monitor.load_config(Path(args.config))
    live_state = live_monitor.load_state(Path(args.live_state))
    state = load_json(Path(args.state), {"version": 1}, "Private backfill state")
    findings = load_json(Path(args.findings), {"version": 1, "items": {}}, "Private backfill findings")
    detected_at = live_monitor.utc_now()
    initialized = initialize_state(
        state,
        config,
        live_state,
        detected_at=detected_at,
        lookback_days=args.lookback_days,
        slice_days=args.slice_days,
        new_window=bool(args.new_window),
    )

    client = ebay_api.EbayBrowseClient(marketplace=config["marketplace"])
    call_budget, quota, quota_warning = api_call_budget(client, args.max_calls)
    result = run_backfill(
        client,
        config,
        state,
        findings,
        live_state,
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
            f"EBAY_PRIVATE_BACKFILL: {len(result['new_candidates'])} live historical "
            f"photobook candidate{'s' if len(result['new_candidates']) != 1 else ''}"
        )
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            issue_body(result, state, len(findings["items"])),
            encoding="utf-8",
        )

    live_monitor.set_output("new_count", len(result["new_candidates"]))
    live_monitor.set_output("state_changed", "true" if initialized or result["calls"] else "false")
    live_monitor.set_output("remaining_steps", result["remaining_steps"])
    live_monitor.set_output("complete", "true" if result["complete"] else "false")
    live_monitor.set_output("calls", result["calls"])
    print(
        f"Private backfill complete: {result['calls']} calls, {result['results_inspected']} results, "
        f"{len(result['new_candidates'])} live candidates, {result['remaining_steps']} queued steps."
    )
    if quota_warning:
        print("WARNING:", quota_warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
