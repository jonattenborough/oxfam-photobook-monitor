#!/usr/bin/env python3
"""Recall-first private-seller eBay discovery.

This wrapper keeps the proven query planning and scoring machinery from
``ebay_private_seller_monitor`` but changes the operating philosophy:

* all available per-run Browse calls are spent on discovery, not mandatory
  item-detail rechecks;
* active-stock coverage receives the three calls previously reserved for live
  checks;
* strong search results are handed straight to the recall-first alert builder;
* cheap unrecognised photobooks can reach AI triage instead of being capped
  below the normal alert threshold;
* previously seen listings can re-alert after a material price drop or newly
  visible collectible/purchase signal.

Final BUY / OFFER recommendations remain the responsibility of the downstream
ChatGPT review, which freshly checks the listing and exact edition.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ebay_private_seller_monitor as legacy
import photobook_recognition as recognition

RECALL_ACTIVE_STOCK_QUERIES_PER_RUN = 4
RECALL_PENDING_LIMIT = 500
CHEAP_UNKNOWN_HARD_LIMIT_GBP = 30.0
CHEAP_UNKNOWN_SOFT_LIMIT_GBP = 50.0
PRICE_DROP_PERCENT = 0.20
PRICE_DROP_ABSOLUTE_GBP = 15.0
PRICE_CROSSINGS_GBP = (100.0, 60.0, 30.0)

COLLECTIBLE_SIGNAL_TERMS = {
    "signed": "signed",
    "autograph": "signed",
    "inscribed": "inscribed",
    "dedicated": "inscribed",
    "first edition": "first edition",
    "1st edition": "first edition",
    "first printing": "first printing",
    "first impression": "first printing",
    "limited edition": "limited edition",
    "numbered": "numbered",
    "artist proof": "artist proof",
    "a/p": "artist proof",
    "original print": "print included",
    "print included": "print included",
    "with print": "print included",
    "slipcase": "slipcase",
    "slip case": "slipcase",
    "dust jacket": "dust jacket",
    "dustjacket": "dust jacket",
    "dj": "dust jacket",
}


def recall_config(config: dict[str, Any]) -> dict[str, Any]:
    adjusted = dict(config)
    adjusted["max_live_checks_per_run"] = 0
    adjusted["active_stock_queries_per_run"] = max(
        RECALL_ACTIVE_STOCK_QUERIES_PER_RUN,
        int(adjusted.get("active_stock_queries_per_run") or 0),
    )
    adjusted["max_pending_live_checks"] = max(
        RECALL_PENDING_LIMIT,
        int(adjusted.get("max_pending_live_checks") or 0),
    )
    return adjusted


def _landed_price(item: dict[str, Any]) -> float | None:
    for field in ("landed_price_gbp", "price_gbp"):
        value = item.get(field)
        try:
            if value is not None:
                return round(float(value), 2)
        except (TypeError, ValueError):
            continue
    if str(item.get("price_currency") or "").upper() == "GBP":
        try:
            return round(float(item.get("price_value")), 2)
        except (TypeError, ValueError):
            return None
    return None


def _normalized_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(field) or "").lower()
        for field in (
            "title",
            "context",
            "description",
            "condition_description",
            "edition",
        )
    )


def collectible_signals(item: dict[str, Any]) -> set[str]:
    text = _normalized_text(item)
    signals = {
        label
        for phrase, label in COLLECTIBLE_SIGNAL_TERMS.items()
        if phrase in text
    }
    options = {str(value).upper() for value in item.get("buying_options") or []}
    if "BEST_OFFER" in options:
        signals.add("best offer")
    return signals


def _score_band(score: int) -> str:
    return (
        "urgent"
        if score >= 90
        else "alert"
        if score >= 72
        else "review"
        if score >= 55
        else "reject"
    )


def recall_classify(item: dict[str, Any], issue_threshold: int) -> dict[str, Any]:
    classified = legacy.classify(item)
    if classified.get("recognized"):
        return classified

    price = _landed_price(classified)
    reasons = [str(value) for value in classified.get("opportunity_reasons") or []]
    reason_text = " | ".join(reasons).lower()
    obvious_noise = "instructional, technical, celebrity or local-history wording" in reason_text
    serious_condition = any(
        marker in reason_text
        for marker in (
            "condition risk: incomplete",
            "condition risk: missing",
            "condition risk: detached",
        )
    )
    photo_or_publisher_signal = any(
        marker in reason_text
        for marker in (
            "photography-book wording",
            "specialist photobook publisher",
            "high-recall discovery lane",
        )
    )

    recall_eligible = False
    if price is not None and not obvious_noise and not serious_condition:
        if price <= CHEAP_UNKNOWN_HARD_LIMIT_GBP:
            recall_eligible = True
        elif price <= CHEAP_UNKNOWN_SOFT_LIMIT_GBP and photo_or_publisher_signal:
            recall_eligible = True

    if recall_eligible:
        score = max(int(classified.get("opportunity_score") or 0), int(issue_threshold))
        classified["opportunity_score"] = score
        classified["score_band"] = _score_band(score)
        classified["collecting_lane"] = "open discovery"
        classified["opportunity_kind"] = "cheap unrecognised photobook lead"
        classified["recall_first_unknown"] = True
        classified["opportunity_reasons"] = reasons + [
            "recall-first cheap unknown lane: AI triage preferred over automatic rejection"
        ]
    return classified


def material_change(previous: Any, item: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(previous, dict):
        return False, []

    # Old state records did not retain these fields. Establish a baseline first
    # instead of treating the schema migration itself as a change.
    has_recall_baseline = "observed_price_gbp" in previous or "collectible_signals" in previous
    if not has_recall_baseline:
        return False, []

    reasons: list[str] = []
    current_price = _landed_price(item)
    try:
        previous_price = (
            float(previous.get("observed_price_gbp"))
            if previous.get("observed_price_gbp") is not None
            else None
        )
    except (TypeError, ValueError):
        previous_price = None

    if current_price is not None and previous_price is not None and current_price < previous_price:
        drop = previous_price - current_price
        drop_fraction = drop / previous_price if previous_price > 0 else 0.0
        crossed = [
            threshold
            for threshold in PRICE_CROSSINGS_GBP
            if previous_price > threshold >= current_price
        ]
        if drop >= PRICE_DROP_ABSOLUTE_GBP or drop_fraction >= PRICE_DROP_PERCENT or crossed:
            reasons.append(
                f"price dropped from £{previous_price:.2f} to £{current_price:.2f}"
            )
            if crossed:
                reasons.append(
                    "price crossed recall threshold "
                    + ", ".join(f"£{value:.0f}" for value in crossed)
                )

    previous_signals = {
        str(value) for value in previous.get("collectible_signals") or []
    }
    new_signals = collectible_signals(item) - previous_signals
    if new_signals:
        reasons.append("new listing signal: " + ", ".join(sorted(new_signals)))

    previous_options = {str(value).upper() for value in previous.get("buying_options") or []}
    current_options = {str(value).upper() for value in item.get("buying_options") or []}
    if "BEST_OFFER" in current_options and "BEST_OFFER" not in previous_options:
        if not any("best offer" in reason for reason in reasons):
            reasons.append("Best Offer has appeared")

    return bool(reasons), reasons


def record_seen_recall(
    seen: dict[str, Any],
    item: dict[str, Any],
    detected_at: str,
) -> None:
    key = str(item.get("key") or "")
    if not key:
        return
    previous = seen.get(key)
    first_seen = (
        str(previous.get("first_seen") or detected_at)
        if isinstance(previous, dict)
        else detected_at
    )
    previous_score = previous.get("score") if isinstance(previous, dict) else None
    score = item.get("opportunity_score")
    seen[key] = {
        "first_seen": first_seen,
        "last_seen": detected_at,
        "title": item.get("title"),
        "url": item.get("url"),
        "score": score if score is not None else previous_score,
        "observed_price_gbp": _landed_price(item),
        "buying_options": [str(value) for value in item.get("buying_options") or []],
        "collectible_signals": sorted(collectible_signals(item)),
    }


def _merge_result(existing: dict[str, Any], item: dict[str, Any]) -> None:
    lanes = {
        str(existing.get("search_lane") or ""),
        str(item.get("search_lane") or ""),
    }
    existing["search_lane"] = "+".join(sorted(value for value in lanes if value))
    # Prefer the cheapest current summary when overlapping queries disagree.
    current_price = _landed_price(existing)
    new_price = _landed_price(item)
    if new_price is not None and (current_price is None or new_price < current_price):
        for field in (
            "price_gbp",
            "price_value",
            "price_currency",
            "shipping_value",
            "shipping_currency",
            "landed_price_gbp",
            "buying_options",
            "tags",
            "url",
            "title",
            "context",
            "image_url",
        ):
            if field in item:
                existing[field] = item[field]


def _candidate_priority(pair: tuple[str, dict[str, Any]]) -> tuple[Any, ...]:
    item = pair[1]
    price = _landed_price(item)
    return (
        1 if item.get("material_change") else 0,
        1 if item.get("recall_first_unknown") else 0,
        int(item.get("opportunity_score") or 0),
        1 if price is not None and price <= 100 else 0,
        -(price if price is not None else 999999.0),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--state", default="data/ebay_private_seller_state.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private")
    args = parser.parse_args()

    config = recall_config(legacy.load_config(Path(args.config)))
    state = legacy.load_state(Path(args.state))
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    detected_at = legacy.utc_now()
    now = legacy._parse_stamp(detected_at) or datetime.now(timezone.utc)
    stats = recognition.library_stats()
    client = legacy.ebay_api.EbayBrowseClient(marketplace=config["marketplace"])

    full_search_plan = legacy.build_search_plan(config, state, now)
    call_budget, quota, quota_warning = legacy.api_call_budget(client, config, now)
    search_plan = legacy.trim_search_plan(full_search_plan, call_budget)

    if not search_plan:
        legacy.write_json(
            runtime / "latest-snapshot.json",
            {
                "checked_at": detected_at,
                "library_stats": stats,
                "planned_queries": 0,
                "quota": quota,
                "quota_warning": quota_warning,
                "skipped": "shared Browse API reserve protected",
                "new_candidates": [],
            },
        )
        legacy.write_json(runtime / "proposed-state.json", state)
        legacy.set_output("new_count", 0)
        legacy.set_output("state_changed", "false")
        legacy.set_output("query_count", 0)
        legacy.set_output("library_records", stats["records"])
        print("Recall-first eBay scan skipped to protect the shared Browse API reserve.")
        return 0

    raw_by_key: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    if quota_warning:
        failures.append(quota_warning)
    successful_queries = 0

    for step in search_plan:
        try:
            items = legacy.run_query(
                client,
                state,
                lane=step["lane"],
                query=step["query"],
                category_ids=step["category_ids"],
                buying_options=step["buying_options"],
                search_in_description=bool(step["search_in_description"]),
                limit=int(config["query_result_limit"]),
                delivery_country=str(config["delivery_country"]),
                max_price_gbp=float(config["max_price_gbp"]),
                detected_at=detected_at,
                incremental=bool(step.get("incremental", True)),
                ending_start_date=step.get("ending_start_date"),
                ending_end_date=step.get("ending_end_date"),
                offset=int(step.get("offset") or 0),
            )
            successful_queries += 1
        except Exception as exc:
            failures.append(f"{step['lane']} `{step['query']}`: {exc}")
            continue
        for item in items:
            key = str(item.get("key") or "")
            if not key:
                continue
            if key not in raw_by_key:
                raw_by_key[key] = item
            else:
                _merge_result(raw_by_key[key], item)

    if successful_queries == 0:
        raise RuntimeError("All private-seller eBay searches failed; refusing to update state")

    seen: dict[str, Any] = state["seen"]
    previous_pending = (
        state.get("pending_live") if isinstance(state.get("pending_live"), dict) else {}
    )
    review_pool: dict[str, dict[str, Any]] = {}
    unseen_count = 0
    changed_count = 0

    # Carry any unflushed alert handoff forward first.
    for key, raw in previous_pending.items():
        if isinstance(raw, dict):
            review_pool[str(key)] = dict(raw)

    for key, item in raw_by_key.items():
        previous = seen.get(key)
        if previous is None:
            unseen_count += 1
            candidate = dict(item)
            candidate["discovery_reason"] = "new listing"
            review_pool[key] = candidate
            continue

        changed, change_reasons = material_change(previous, item)
        if changed:
            changed_count += 1
            candidate = dict(item)
            candidate["material_change"] = True
            candidate["material_change_reasons"] = change_reasons
            candidate["discovery_reason"] = "materially improved seen listing"
            review_pool[key] = candidate
            continue

        # Even a non-alerted result refreshes the baseline used to detect the
        # next price or listing improvement.
        record_seen_recall(seen, item, detected_at)

    issue_threshold = int(config["issue_threshold"])
    classified: list[dict[str, Any]] = []
    for item in review_pool.values():
        if not item.get("key"):
            continue
        result = recall_classify(item, issue_threshold)
        if item.get("material_change_reasons"):
            result["material_change"] = True
            result["material_change_reasons"] = list(item["material_change_reasons"])
            result["opportunity_reasons"] = [
                *[str(value) for value in result.get("opportunity_reasons") or []],
                *[str(value) for value in item["material_change_reasons"]],
            ]
        classified.append(result)

    alertable: dict[str, dict[str, Any]] = {}
    for item in classified:
        key = str(item.get("key") or "")
        score = int(item.get("opportunity_score") or 0)
        is_private = item.get("private_seller") is True
        not_business = str(item.get("seller_account_type") or "").upper() != "BUSINESS"
        if score >= issue_threshold and is_private and not_business:
            pending_copy = dict(item)
            pending_copy.pop("live_verified", None)
            pending_copy["pending_since"] = str(
                previous_pending.get(key, {}).get("pending_since")
                if isinstance(previous_pending.get(key), dict)
                else detected_at
            ) or detected_at
            alertable[key] = pending_copy
        else:
            record_seen_recall(seen, item, detected_at)

    ranked_alertable = sorted(alertable.items(), key=_candidate_priority, reverse=True)
    state["pending_live"] = dict(ranked_alertable[: int(config["max_pending_live_checks"])])
    state["seen"] = legacy._trim_seen(seen)
    state["last_run"] = detected_at
    state["last_query_count"] = len(search_plan)
    state["last_successful_queries"] = successful_queries
    state["last_live_checks"] = 0
    state["last_failure_count"] = len(failures)
    state["library_records"] = stats["records"]
    state["last_api_call_budget"] = call_budget
    state["last_browse_quota"] = quota
    state["recall_first"] = True
    state["last_material_change_count"] = changed_count

    legacy.write_json(runtime / "proposed-state.json", state)
    legacy.write_json(
        runtime / "latest-snapshot.json",
        {
            "checked_at": detected_at,
            "library_stats": stats,
            "quota": quota,
            "api_call_budget": call_budget,
            "planned_queries": len(search_plan),
            "successful_queries": successful_queries,
            "live_checks": 0,
            "failures": failures,
            "unique_results": len(raw_by_key),
            "unseen_results": unseen_count,
            "materially_changed_results": changed_count,
            "pending_live_verification": len(state["pending_live"]),
            "pending_recall_alerts": len(state["pending_live"]),
            "new_candidates": [],
            "recall_first": True,
        },
    )

    legacy.set_output("new_count", 0)
    legacy.set_output("state_changed", "true")
    legacy.set_output("query_count", len(search_plan))
    legacy.set_output("library_records", stats["records"])
    print(
        f"Recall-first eBay scan: {len(search_plan)} searches planned, "
        f"{successful_queries} succeeded, {len(raw_by_key)} unique results, "
        f"{unseen_count} unseen, {changed_count} materially changed, "
        f"{len(state['pending_live'])} recall alerts, 0 live-check calls."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
