#!/usr/bin/env python3
"""Recall-first runtime policy for the private-seller eBay monitor.

This layer keeps the proven search and scoring engine intact while changing four
operational choices:

1. All 38 available calls are used for discovery rather than reserving three
   calls for mandatory live checks.
2. Active-stock paging is increased from one to four searches per run.
3. Cheap unrecognised photobooks can reach human review instead of being capped
   below the normal recognition threshold.
4. Previously seen listings can be re-alerted after a meaningful price drop or
   newly added collector signal.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import ebay_private_seller_monitor as monitor

POLICY_PATH = Path("data/ebay_private_recall_policy.json")
TRACKING_VERSION = 1
CHANGE_KEY_MARKER = ":change:"

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "max_live_checks_per_run": 0,
    "active_stock_queries_per_run": 4,
    "max_pending_candidates": 1000,
    "unknown_bargain_max_price_gbp": 30.0,
    "unknown_bargain_min_score": 35,
    "unknown_bargain_alert_score": 74,
    "material_price_drop_min_percent": 0.12,
    "material_price_drop_min_gbp": 5.0,
    "material_change_max_price_gbp": 300.0,
    "material_change_alert_score": 74,
    "alert_threshold": 72,
    "urgent_threshold": 90,
}

NON_BOOK_BLOCKERS = {
    "camera manual",
    "digital photography handbook",
    "dvd",
    "magazine clippings",
    "photo editing",
    "photography handbook",
    "photography manual",
    "postcard",
    "poster",
    "t shirt",
    "t-shirt",
}

BOOK_EVIDENCE_TERMS = {
    "book",
    "catalogue",
    "hardback",
    "hardcover",
    "monograph",
    "paperback",
    "photo book",
    "photobook",
}

_ORIGINAL_LOAD_CONFIG = monitor.load_config
_ORIGINAL_RUN_QUERY = monitor.run_query
_ORIGINAL_CLASSIFY = monitor.classify
_ORIGINAL_RECORD_SEEN = monitor._record_seen
_INSTALLED = False
_ACTIVE_POLICY: dict[str, Any] = dict(DEFAULT_POLICY)


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"{path} must contain a JSON object")
        policy.update(payload)
    return policy


def apply_runtime_config(config: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    updated = dict(config)
    updated["max_live_checks_per_run"] = max(0, int(policy["max_live_checks_per_run"]))
    updated["active_stock_queries_per_run"] = max(
        int(updated.get("active_stock_queries_per_run") or 0),
        int(policy["active_stock_queries_per_run"]),
    )
    updated["max_pending_live_checks"] = max(
        int(updated.get("max_pending_live_checks") or 0),
        int(policy["max_pending_candidates"]),
    )
    return updated


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def landed_price(item: dict[str, Any]) -> float | None:
    value = item.get("landed_price_gbp")
    if value is None:
        value = item.get("price_gbp")
    if value is None and str(item.get("price_currency") or "").upper() == "GBP":
        value = item.get("price_value")
    return _as_float(value)


def _normalised_text(item: dict[str, Any]) -> str:
    return monitor.pb.normalize(
        " ".join(
            str(item.get(field) or "")
            for field in (
                "title",
                "context",
                "description",
                "condition_description",
                "edition",
            )
        )
    )


def collectible_signals(item: dict[str, Any]) -> list[str]:
    text = _normalised_text(item)
    signals: set[str] = set()

    signed_negated = bool(re.search(r"\bnot signed\b|\bunsigned\b", text))
    if not signed_negated and re.search(r"\b(signed|autographed|autograph|inscribed)\b", text):
        signals.add("signed or inscribed")
    if re.search(r"\b(limited edition|special edition|artist proof|artists proof|a p copy)\b", text):
        signals.add("limited or special edition")
    if re.search(r"\b(numbered|edition of [0-9]+|no[.]? [0-9]+)\b|\b[0-9]+\s*/\s*[0-9]+\b", text):
        signals.add("numbered copy")
    if re.search(
        r"\b(original print|print included|with (an )?original print|signed print|"
        r"tipped in photograph|tipped-in photograph|photograph laid in|photograph laid-in)\b",
        text,
    ):
        signals.add("original print included")
    if re.search(r"\b(first edition|1st edition|first printing|1st printing|first impression)\b", text):
        signals.add("first edition or printing claim")
    if re.search(r"\b(slipcase|slip case|clamshell|presentation box|boxed edition)\b", text):
        signals.add("collector housing")
    return sorted(signals)


def tracking_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracking_version": TRACKING_VERSION,
        "observed_price_gbp": landed_price(item),
        "observed_buying_options": sorted(
            {str(value).upper() for value in item.get("buying_options") or [] if str(value).strip()}
        ),
        "observed_collectible_signals": collectible_signals(item),
        "observed_title": monitor.pb.normalize(item.get("title"))[:350],
    }


def material_change_reasons(
    previous: dict[str, Any],
    current: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    if int(previous.get("tracking_version") or 0) != TRACKING_VERSION:
        return []

    reasons: list[str] = []
    previous_price = _as_float(previous.get("observed_price_gbp"))
    current_price = _as_float(current.get("observed_price_gbp"))
    if previous_price is not None and current_price is not None and current_price < previous_price:
        drop = previous_price - current_price
        drop_fraction = drop / previous_price if previous_price > 0 else 0.0
        minimum_drop = max(
            float(policy["material_price_drop_min_gbp"]),
            previous_price * float(policy["material_price_drop_min_percent"]),
        )
        crossed_budget_line = previous_price > 100 >= current_price and drop >= 3
        if drop >= minimum_drop or crossed_budget_line:
            reasons.append(
                f"price reduced from £{previous_price:.2f} to £{current_price:.2f} "
                f"({drop_fraction:.0%})"
            )

    previous_options = {
        str(value).upper() for value in previous.get("observed_buying_options") or []
    }
    current_options = {
        str(value).upper() for value in current.get("observed_buying_options") or []
    }
    if "BEST_OFFER" in current_options and "BEST_OFFER" not in previous_options:
        reasons.append("Best Offer was added")
    if "FIXED_PRICE" in current_options and "FIXED_PRICE" not in previous_options and previous_options:
        reasons.append("fixed-price purchase was added")

    previous_signals = {
        str(value) for value in previous.get("observed_collectible_signals") or []
    }
    current_signals = {
        str(value) for value in current.get("observed_collectible_signals") or []
    }
    added_signals = sorted(current_signals - previous_signals)
    if added_signals:
        reasons.append("new collector wording: " + ", ".join(added_signals))
    return reasons


def _change_key(base_key: str, snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha1(payload).hexdigest()[:12]
    return f"{base_key}{CHANGE_KEY_MARKER}{digest}"


def prepare_query_items(
    state: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    detected_at: str,
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    seen = state.setdefault("seen", {})
    for item in items:
        base_key = str(item.get("base_key") or item.get("key") or "")
        if not base_key:
            continue
        current = tracking_snapshot(item)
        item["recall_tracking"] = current
        previous = seen.get(base_key)
        if not isinstance(previous, dict):
            continue

        if int(previous.get("tracking_version") or 0) != TRACKING_VERSION:
            previous.update(current)
            previous["last_seen"] = detected_at
            previous["title"] = item.get("title")
            previous["url"] = item.get("url")
            continue

        reasons = material_change_reasons(previous, current, policy)
        if reasons:
            item["base_key"] = base_key
            item["key"] = _change_key(base_key, current)
            item["material_change"] = True
            item["material_change_reasons"] = reasons
            item["previous_observed_price_gbp"] = previous.get("observed_price_gbp")
            continue

        previous.update(current)
        previous["last_seen"] = detected_at
        previous["title"] = item.get("title")
        previous["url"] = item.get("url")
    return items


def _unknown_bargain_eligible(classified: dict[str, Any], policy: dict[str, Any]) -> bool:
    if classified.get("recognized") is True:
        return False
    price = landed_price(classified)
    if price is None or price > float(policy["unknown_bargain_max_price_gbp"]):
        return False
    if int(classified.get("opportunity_score") or 0) < int(policy["unknown_bargain_min_score"]):
        return False

    lanes = {
        value
        for value in str(classified.get("search_lane") or "").split("+")
        if value
    }
    if not lanes.intersection(
        {"active_stock", "broad", "collectible_format", "collection", "wrong_category"}
    ):
        return False

    text = _normalised_text(classified)
    if any(
        monitor.pb.contains_normalized_phrase(text, monitor.pb.normalize(term))
        for term in NON_BOOK_BLOCKERS
    ):
        return False
    if str(classified.get("category_id") or "") != monitor.BOOKS_CATEGORY_ID and not any(
        monitor.pb.contains_normalized_phrase(text, monitor.pb.normalize(term))
        for term in BOOK_EVIDENCE_TERMS
    ):
        return False
    return True


def _score_band(score: int, policy: dict[str, Any]) -> str:
    if score >= int(policy["urgent_threshold"]):
        return "urgent"
    if score >= int(policy["alert_threshold"]):
        return "alert"
    if score >= 55:
        return "review"
    return "reject"


def classify_with_policy(item: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    classified = _ORIGINAL_CLASSIFY(item)
    reasons = list(classified.get("opportunity_reasons") or [])

    if _unknown_bargain_eligible(classified, policy):
        price = landed_price(classified)
        classified["unknown_bargain"] = True
        classified["collecting_lane"] = "open discovery"
        classified["opportunity_kind"] = "cheap unrecognised photobook lead"
        classified["opportunity_score"] = max(
            int(classified.get("opportunity_score") or 0),
            int(policy["unknown_bargain_alert_score"]),
        )
        reasons.append(
            f"cheap unrecognised photobook sent to human review at £{price:.2f}"
        )

    if item.get("material_change"):
        change_reasons = [str(value) for value in item.get("material_change_reasons") or []]
        classified["material_change"] = True
        classified["material_change_reasons"] = change_reasons
        classified["previous_observed_price_gbp"] = item.get("previous_observed_price_gbp")
        classified["base_key"] = item.get("base_key")
        classified["recall_tracking"] = item.get("recall_tracking")
        reasons.extend(f"material change: {value}" for value in change_reasons)

        price = landed_price(classified)
        recognised_or_open = classified.get("recognized") is True or classified.get("unknown_bargain") is True
        has_price_drop = any(value.startswith("price reduced") for value in change_reasons)
        has_new_signal = any(value.startswith("new collector wording") for value in change_reasons)
        has_purchase_change = any(
            value in {"Best Offer was added", "fixed-price purchase was added"}
            for value in change_reasons
        )
        maximum_price = float(policy["material_change_max_price_gbp"])
        if price is not None and price <= maximum_price and recognised_or_open:
            target = int(policy["material_change_alert_score"])
            if has_price_drop and price <= 100:
                target = max(target, 82)
            elif has_price_drop:
                target = max(target, 76)
            if has_new_signal:
                target = max(target, 80)
            if has_purchase_change:
                target = max(target, int(policy["alert_threshold"]))
            classified["opportunity_score"] = max(
                int(classified.get("opportunity_score") or 0),
                target,
            )
            classified["opportunity_kind"] = "materially improved listing"
        elif has_new_signal and int(classified.get("opportunity_score") or 0) >= 50:
            classified["opportunity_score"] = max(
                int(classified.get("opportunity_score") or 0),
                76,
            )
            classified["opportunity_kind"] = "new collectible-object evidence"

    classified["opportunity_reasons"] = list(dict.fromkeys(reasons))
    score = int(classified.get("opportunity_score") or 0)
    classified["score_band"] = _score_band(score, policy)
    return classified


def record_seen_with_tracking(
    seen: dict[str, Any],
    item: dict[str, Any],
    detected_at: str,
) -> None:
    base_key = str(item.get("base_key") or item.get("key") or "")
    if not base_key:
        return
    stored = dict(item)
    stored["key"] = base_key
    _ORIGINAL_RECORD_SEEN(seen, stored, detected_at)
    entry = seen.get(base_key)
    if not isinstance(entry, dict):
        return
    entry.update(tracking_snapshot(item))
    entry["last_alerted_at"] = detected_at
    if item.get("material_change"):
        entry["last_alert_type"] = "material_change"
    elif item.get("unknown_bargain"):
        entry["last_alert_type"] = "unknown_bargain"
    else:
        entry["last_alert_type"] = "new_listing"
    versioned_key = str(item.get("key") or "")
    if versioned_key and versioned_key != base_key:
        seen.pop(versioned_key, None)


def install_policy(policy: dict[str, Any] | None = None) -> None:
    global _INSTALLED, _ACTIVE_POLICY
    if _INSTALLED:
        return
    _ACTIVE_POLICY = dict(DEFAULT_POLICY)
    if policy:
        _ACTIVE_POLICY.update(policy)

    def patched_load_config(path: Path) -> dict[str, Any]:
        return apply_runtime_config(_ORIGINAL_LOAD_CONFIG(path), _ACTIVE_POLICY)

    def patched_run_query(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        items = _ORIGINAL_RUN_QUERY(*args, **kwargs)
        state = args[1] if len(args) > 1 else kwargs.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("private-seller state was not supplied to run_query")
        detected_at = str(kwargs.get("detected_at") or monitor.utc_now())
        return prepare_query_items(
            state,
            items,
            detected_at=detected_at,
            policy=_ACTIVE_POLICY,
        )

    def patched_classify(item: dict[str, Any]) -> dict[str, Any]:
        return classify_with_policy(item, _ACTIVE_POLICY)

    def patched_record_seen(
        seen: dict[str, Any], item: dict[str, Any], detected_at: str
    ) -> None:
        record_seen_with_tracking(seen, item, detected_at)

    monitor.load_config = patched_load_config
    monitor.run_query = patched_run_query
    monitor.classify = patched_classify
    monitor._record_seen = patched_record_seen
    _INSTALLED = True


def uninstall_policy() -> None:
    global _INSTALLED
    monitor.load_config = _ORIGINAL_LOAD_CONFIG
    monitor.run_query = _ORIGINAL_RUN_QUERY
    monitor.classify = _ORIGINAL_CLASSIFY
    monitor._record_seen = _ORIGINAL_RECORD_SEEN
    _INSTALLED = False


def main() -> int:
    policy_path = Path(os.getenv("EBAY_RECALL_POLICY_PATH", str(POLICY_PATH)))
    install_policy(load_policy(policy_path))
    return monitor.main()


if __name__ == "__main__":
    raise SystemExit(main())
