#!/usr/bin/env python3
"""Monitor selected eBay charity sellers for newly listed photobooks."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import canon_runner
import ebay_api
import ebay_core_targets as core_targets
import ebay_search_checkpoint as checkpointing
import external_monitor
import photobook_target_books as target_books

BOOKS_CATEGORY_ID = "261186"
PAGE_SIZE = 200
MAX_INCREMENTAL_PAGES = 5
OVERLAP_MINUTES = 10
MAX_SEEN_PER_SELLER = 1000
DEFAULT_SELLERS_PER_RUN = 12
QUOTA_RESERVE = 650
SUPPORTED_MARKETPLACES = {"EBAY_GB", "EBAY_US"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def incremental_start(value: Any) -> str | None:
    parsed = _parse_stamp(value)
    if parsed is None:
        return None
    start = parsed - timedelta(minutes=OVERLAP_MINUTES)
    return start.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seller_key(marketplace: str, seller_id: str) -> str:
    return f"{marketplace}:{seller_id.lower()}"


def seller_url(marketplace: str, seller_id: str) -> str:
    domain = "www.ebay.com" if marketplace == "EBAY_US" else "www.ebay.co.uk"
    return f"https://{domain}/usr/{seller_id}"


def load_config(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        raise RuntimeError("eBay seller config must contain a groups list")

    sellers: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("Every eBay seller group must be an object")
        marketplace = str(group.get("marketplace") or "").strip().upper()
        if marketplace not in SUPPORTED_MARKETPLACES:
            raise RuntimeError(f"Unsupported eBay marketplace: {marketplace or '(missing)'}")
        delivery_country = str(group.get("delivery_country") or "").strip().upper()
        raw_sellers = group.get("sellers")
        if not isinstance(raw_sellers, list):
            raise RuntimeError(f"{marketplace} sellers must be a list")
        for raw_seller in raw_sellers:
            seller_id = str(raw_seller or "").strip()
            if not seller_id:
                raise RuntimeError(f"{marketplace} contains a blank seller ID")
            key = seller_key(marketplace, seller_id)
            if key in seen:
                raise RuntimeError(f"Duplicate eBay seller: {key}")
            seen.add(key)
            seller = {"id": seller_id, "marketplace": marketplace}
            if delivery_country:
                seller["delivery_country"] = delivery_country
            sellers.append(seller)
    if not sellers:
        raise RuntimeError("eBay seller config contains no sellers")
    return sellers


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sellers": {}, "seller_cursor": 0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("eBay seller monitor state is not a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("sellers", {})
    payload.setdefault("seller_cursor", 0)
    if not isinstance(payload["sellers"], dict):
        raise RuntimeError("eBay seller monitor sellers state is not an object")
    return payload


def select_sellers(
    sellers: list[dict[str, str]],
    cursor: int,
    count: int,
) -> tuple[list[dict[str, str]], int]:
    if not sellers or count <= 0:
        return [], 0
    start = max(0, int(cursor)) % len(sellers)
    selected_count = min(len(sellers), int(count))
    selected = [sellers[(start + offset) % len(sellers)] for offset in range(selected_count)]
    return selected, (start + selected_count) % len(sellers)


def select_sellers_with_pending(
    sellers: list[dict[str, str]],
    state: dict[str, Any],
    cursor: int,
    count: int,
) -> tuple[list[dict[str, str]], int]:
    """Give resumable sellers bounded priority without starving fresh rotation."""
    if not sellers or count <= 0:
        return [], 0
    seller_state = state.get("sellers") if isinstance(state.get("sellers"), dict) else {}
    by_key = {seller_key(row["marketplace"], row["id"]): row for row in sellers}
    pending: list[tuple[str, str, dict[str, str]]] = []
    for key, previous in seller_state.items():
        if key not in by_key or not isinstance(previous, dict):
            continue
        window = previous.get("pending_window")
        if not isinstance(window, dict) or not window.get("pending"):
            continue
        pending.append((str(window.get("last_attempt_at") or ""), key, by_key[key]))
    pending.sort(key=lambda row: (row[0], row[1]))
    pending_limit = min(len(pending), max(1, int(count) // 3))
    chosen_pending = [row[2] for row in pending[:pending_limit]]
    chosen_keys = {seller_key(row["marketplace"], row["id"]) for row in chosen_pending}

    rotating: list[dict[str, str]] = []
    start = max(0, int(cursor)) % len(sellers)
    examined = 0
    wanted_rotation = max(0, min(len(sellers), int(count)) - len(chosen_pending))
    while examined < len(sellers) and len(rotating) < wanted_rotation:
        row = sellers[(start + examined) % len(sellers)]
        examined += 1
        if seller_key(row["marketplace"], row["id"]) in chosen_keys:
            continue
        rotating.append(row)
    next_cursor = (start + examined) % len(sellers)
    return chosen_pending + rotating, next_cursor


def quota_safe_seller_count(usable_calls: int, requested_count: int) -> int:
    """Reserve enough headroom for the worst incremental page count."""
    return min(max(0, int(requested_count)), max(0, int(usable_calls)) // MAX_INCREMENTAL_PAGES)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def set_output(name: str, value: Any) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def _convert_rows(
    rows: list[dict[str, Any]],
    seller: dict[str, str],
) -> list[dict[str, Any]]:
    source = {
        "id": f"ebay_seller_{seller['marketplace'].lower()}_{seller['id'].lower()}",
        "name": f"eBay seller {seller['id']}",
        "marketplace": seller["marketplace"],
    }
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for raw in rows:
        item = ebay_api.listing_from_summary(raw, source)
        if item is None or item["key"] in seen_keys:
            continue
        seen_keys.add(item["key"])
        item["seller_id"] = seller["id"]
        item["marketplace"] = seller["marketplace"]
        item["source_page"] = seller_url(seller["marketplace"], seller["id"])
        items.append(item)
    return items


def scan_seller(
    client: ebay_api.EbayBrowseClient,
    seller: dict[str, str],
    previous: dict[str, Any] | None,
    *,
    detected_at: str | None = None,
    max_calls: int = MAX_INCREMENTAL_PAGES,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool, str]:
    """Scan one seller without losing dense incremental windows."""
    initialized = bool(previous and previous.get("initialized"))
    window_end = str(detected_at or utc_now())
    if not initialized:
        rows = client.search(
            None,
            limit=PAGE_SIZE,
            category_ids=BOOKS_CATEGORY_ID,
            fixed_price_only=True,
            seller_ids=[seller["id"]],
            delivery_country=seller.get("delivery_country"),
        )
        return _convert_rows(rows, seller), None, True, window_end

    prior_window = previous.get("pending_window") if isinstance(previous, dict) else None
    if isinstance(prior_window, dict) and prior_window.get("pending"):
        checkpoint = copy.deepcopy(prior_window)
        window_end = str(checkpoint.get("end") or window_end)
    else:
        start_date = incremental_start(previous.get("last_successful_fetch")) if previous else None
        if not start_date:
            raise RuntimeError("initialized seller is missing a successful-fetch watermark")
        checkpoint = checkpointing.new_checkpoint(start_date, window_end)

    def fetch_window(start_date: str, end_date: str, offset: int) -> dict[str, Any]:
        return client.search_page(
            None,
            limit=PAGE_SIZE,
            offset=offset,
            category_ids=BOOKS_CATEGORY_ID,
            fixed_price_only=True,
            seller_ids=[seller["id"]],
            delivery_country=seller.get("delivery_country"),
            item_start_date=start_date,
            item_end_date=end_date,
        )

    rows, _, complete = checkpointing.drain(
        checkpoint,
        fetch_window,
        client.search_next,
        max_calls=max(0, int(max_calls)),
        page_size=PAGE_SIZE,
        retryable_errors=(ebay_api.EbayApiError, ValueError),
    )
    checkpoint["last_attempt_at"] = str(detected_at or utc_now())
    return _convert_rows(rows, seller), checkpoint, complete, window_end

def qualification(item: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    text = " ".join([str(item.get("title") or ""), str(item.get("context") or "")]).lower()
    signals: list[str] = []
    core_matches = core_targets.matches_for_item(item)
    if core_matches:
        names = list(dict.fromkeys(match["name"] for match in core_matches))
        tier = core_matches[0]["tier"]
        signals.append(f"Tier {tier} core photographer: {', '.join(names)}")
        item["core_target_matches"] = core_matches
        item["core_target_tier"] = tier
        item["matched_core_photographers"] = names
    if any(term in text for term in external_monitor.TARGET_TERMS):
        signals.append("known photographer or title term")
    if any(term in text for term in external_monitor.DIRECT_PHOTO_TERMS):
        signals.append("photography wording")
    if any(term in text for term in external_monitor.PUBLISHER_TERMS):
        signals.append("specialist photobook publisher")
    visual = any(term in text for term in external_monitor.VISUAL_ART_TERMS)
    edition = any(term in text for term in external_monitor.EDITION_TERMS)
    if visual and edition:
        signals.append("visual-art and collectible-edition wording")

    matches = canon_runner.pb.matches_for_item(item)
    if matches:
        signals.append("Parr/Badger or Roth canon match")
    if not external_monitor.plausible(item) and not matches and not core_matches:
        return [], []
    return signals or ["photobook-radar wording"], matches


def _trim_seen(seen: dict[str, Any]) -> dict[str, Any]:
    if len(seen) <= MAX_SEEN_PER_SELLER:
        return seen
    ranked: list[tuple[str, str, Any]] = []
    for key, value in seen.items():
        stamp = str(value.get("first_seen") or "") if isinstance(value, dict) else ""
        ranked.append((stamp, key, value))
    ranked.sort(reverse=True)
    return {key: value for _, key, value in ranked[:MAX_SEEN_PER_SELLER]}


def update_seller_state(
    previous: dict[str, Any] | None,
    items: list[dict[str, Any]],
    detected_at: str,
    *,
    scan_complete: bool = True,
    checkpoint: dict[str, Any] | None = None,
    window_end: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    initialized = bool(previous and previous.get("initialized"))
    if not initialized:
        seen = {
            item["key"]: {
                "first_seen": detected_at,
                "last_seen": detected_at,
                "title": item.get("title"),
                "url": item.get("url"),
            }
            for item in items
        }
        return ({
            "initialized": True,
            "first_successful_fetch": detected_at,
            "last_successful_fetch": detected_at,
            "last_result_count": len(items),
            "seen": _trim_seen(seen),
        }, [], True)

    updated = dict(previous or {})
    seen = updated.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    candidates: list[dict[str, Any]] = []
    for item in items:
        key = item["key"]
        prior_item = seen.get(key)
        if prior_item is None:
            seen[key] = {
                "first_seen": detected_at,
                "last_seen": detected_at,
                "title": item.get("title"),
                "url": item.get("url"),
            }
            signals, matches = qualification(item)
            if signals:
                candidate = dict(item)
                candidate["qualification_signals"] = signals
                candidate["book_judgment"] = target_books.assess_listing(candidate)
                if matches:
                    candidate["parr_badger_matches"] = matches
                candidates.append(candidate)
        elif isinstance(prior_item, dict):
            prior_item["last_seen"] = detected_at

    updated["initialized"] = True
    updated["last_result_count"] = len(items)
    updated["last_scan_complete"] = bool(scan_complete)
    updated["seen"] = _trim_seen(seen)
    if scan_complete:
        updated["last_successful_fetch"] = str(window_end or detected_at)
        updated.pop("pending_window", None)
    else:
        if not isinstance(checkpoint, dict) or not checkpoint.get("pending"):
            raise RuntimeError("incomplete seller scan must retain a resumable checkpoint")
        updated["pending_window"] = copy.deepcopy(checkpoint)
    return updated, candidates, False


def _price_line(item: dict[str, Any]) -> str | None:
    value = item.get("price_value")
    currency = str(item.get("price_currency") or "")
    if value is None:
        return None
    if currency == "GBP":
        return f"- **Observed price:** £{value:.2f}"
    return f"- **Observed price:** {currency} {value:.2f}".rstrip()


MAX_ISSUE_BODY_BYTES = 28_000
MAX_ISSUE_CANDIDATES = 20


def candidate_order(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if (item.get("book_judgment") or {}).get("target_book") else 1,
        0 if item.get("core_target_tier") else 1,
        int(item.get("core_target_tier") or 9),
        0 if item.get("parr_badger_matches") else 1,
        float(item.get("price_value") or 999999),
        str(item.get("key") or item.get("url") or ""),
    )


def make_issue_body(items: list[dict[str, Any]], detected_at: str, failures: list[str]) -> str:
    lines = [
        "## New books from monitored eBay charity sellers",
        "",
        f"Detected at **{detected_at}** by the seller-specific eBay Browse API monitor.",
        "",
        "Only items first seen after each seller's silent baseline are included. These are high-recall photography-book candidates, not automatic buy recommendations.",
        "ChatGPT should verify edition, printing, completeness, condition, delivery cost and market value before sending any purchase alert.",
        "",
    ]
    ordered = sorted(items, key=candidate_order)
    for item in ordered:
        lines.extend([
            f"### {item.get('title') or 'Untitled listing'}",
            "",
            f"- **Seller:** {item['seller_id']}",
            f"- **Marketplace:** {item['marketplace']}",
        ])
        price = _price_line(item)
        if price:
            lines.append(price)
        if item.get("core_target_tier"):
            names = ", ".join(item.get("matched_core_photographers") or [])
            lines.append(
                f"- **Core photographer priority:** Tier {item['core_target_tier']}"
                + (f" - {names}" if names else "")
            )
        judgment = item.get("book_judgment") or {}
        if judgment.get("target_book"):
            lines.extend([
                f"- **Target book:** {judgment['target_book']}",
                f"- **Collectibility:** {judgment['collectibility']}",
                f"- **Identification confidence:** {judgment['identification_confidence']}",
                f"- **Price opportunity:** {judgment['price_opportunity']}",
            ])
        lines.extend([
            f"- **Why it surfaced:** {', '.join(item.get('qualification_signals') or [])}",
            f"- **Listing:** {item['url']}",
            f"- **Seller page:** {item['source_page']}",
        ])
        if item.get("context"):
            lines.append(f"- **API context:** {str(item['context'])[:600]}")
        matches = item.get("parr_badger_matches") or []
        for match in matches[:3]:
            volumes = str(match.get("volumes") or "")
            canon = "Roth 101" if volumes == "R101" else f"Parr/Badger V{volumes}"
            if "Roth 101" in str(match.get("pb_refs") or "") and volumes != "R101":
                canon += " + Roth 101"
            lines.append(
                f"- **Canon match:** {canon}: {match.get('contributor')}, "
                f"*{match.get('title')}* ({match.get('score')}/100)"
            )
        lines.append("")
    if failures:
        lines.extend([
            "### Seller warnings",
            "",
            "These sellers failed temporarily and retained their previous state:",
        ])
        lines.extend(f"- {failure}" for failure in failures)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def split_issue_batches(items: list[dict[str, Any]], detected_at: str,
                        failures: list[str]) -> list[list[dict[str, Any]]]:
    """Keep each issue well inside GitHub's body limit without dropping items."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in sorted(items, key=candidate_order):
        proposed = current + [item]
        if current and (len(proposed) > MAX_ISSUE_CANDIDATES or
                        len(make_issue_body(proposed, detected_at, failures).encode("utf-8"))
                        > MAX_ISSUE_BODY_BYTES):
            batches.append(current)
            proposed = [item]
        if len(make_issue_body(proposed, detected_at, failures).encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
            raise ValueError(f"Single charity candidate exceeds issue body limit: {item.get('key')}")
        current = proposed
    if current:
        batches.append(current)
    return batches


def write_issue_packets(runtime: Path, items: list[dict[str, Any]],
                        detected_at: str, failures: list[str]) -> int:
    alerts = runtime / "alerts"
    if alerts.exists():
        shutil.rmtree(alerts)
    alerts.mkdir(parents=True)
    batches = split_issue_batches(items, detected_at, failures)
    for index, batch in enumerate(batches, start=1):
        keys = sorted(str(item.get("key") or item.get("url") or "") for item in batch)
        fingerprint = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]
        title = (f"CHARITY_NEW: {len(batch)} eBay seller photobook candidates "
                 f"| batch {index}/{len(batches)} | key {fingerprint}")
        stem = alerts / f"issue-{index:03d}"
        stem.with_suffix(".title").write_text(title + "\n", encoding="utf-8")
        stem.with_suffix(".md").write_text(
            make_issue_body(batch, detected_at, failures), encoding="utf-8")
    return len(batches)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_sellers.json")
    parser.add_argument("--state", default="data/ebay_seller_state.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-sellers")
    parser.add_argument("--sellers-per-run", type=int, default=DEFAULT_SELLERS_PER_RUN)
    parser.add_argument("--all-sellers", action="store_true")
    args = parser.parse_args()

    sellers = load_config(Path(args.config))
    state = load_state(Path(args.state))
    sellers_state: dict[str, Any] = state["sellers"]
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    detected_at = utc_now()

    clients = {
        marketplace: ebay_api.EbayBrowseClient(marketplace=marketplace)
        for marketplace in sorted({seller["marketplace"] for seller in sellers})
    }
    requested_count = len(sellers) if args.all_sellers else max(1, args.sellers_per_run)
    cursor = int(state.get("seller_cursor") or 0)
    selected_sellers, next_cursor = select_sellers_with_pending(
        sellers, state, cursor, requested_count
    )
    quota: dict[str, Any] | None = None
    quota_warning: str | None = None
    try:
        quota = next(iter(clients.values())).browse_quota()
        usable = max(0, int(quota.get("remaining") or 0) - QUOTA_RESERVE)
        safe_count = quota_safe_seller_count(usable, len(selected_sellers))
        if safe_count < len(selected_sellers):
            selected_sellers = selected_sellers[:safe_count]
            next_cursor = (cursor + len(selected_sellers)) % len(sellers)
    except Exception as exc:
        quota_warning = f"Browse quota lookup failed; using the scheduled seller batch cap: {exc}"

    if not selected_sellers:
        write_json(
            runtime / "latest-snapshot.json",
            {
                "checked_at": detected_at,
                "configured_sellers": len(sellers),
                "selected_sellers": 0,
                "quota": quota,
                "quota_warning": quota_warning,
                "skipped": "shared Browse API reserve protected",
                "new_candidates": [],
            },
        )
        set_output("new_count", 0)
        set_output("issue_count", 0)
        set_output("state_changed", "false")
        set_output("successful_requests", 0)
        set_output("failed_requests", 0)
        print("Seller sweep skipped to protect the shared Browse API reserve.")
        return 0

    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    if quota_warning:
        failures.append(quota_warning)
    successes = 0
    baselines = 0
    incomplete_sellers: list[str] = []

    for seller in selected_sellers:
        key = seller_key(seller["marketplace"], seller["id"])
        previous = sellers_state.get(key)
        if not isinstance(previous, dict):
            previous = None
        try:
            items, checkpoint, scan_complete, window_end = scan_seller(
                clients[seller["marketplace"]],
                seller,
                previous,
                detected_at=detected_at,
                max_calls=MAX_INCREMENTAL_PAGES,
            )
            updated, seller_candidates, was_baseline = update_seller_state(
                previous,
                items,
                detected_at,
                scan_complete=scan_complete,
                checkpoint=checkpoint,
                window_end=window_end,
            )
        except Exception as exc:
            warning = f"{seller['marketplace']} {seller['id']}: {exc}"
            failures.append(warning)
            print("WARNING:", warning, file=sys.stderr)
            continue

        sellers_state[key] = updated
        candidates.extend(seller_candidates)
        successes += 1
        if not scan_complete:
            incomplete_sellers.append(key)
        if was_baseline:
            baselines += 1
            print(f"{seller['marketplace']} {seller['id']}: silent baseline seeded with {len(items)} books.")
        else:
            print(
                f"{seller['marketplace']} {seller['id']}: {len(items)} recent books checked; "
                f"{len(seller_candidates)} new candidates; "
                f"{'window complete' if scan_complete else 'window checkpointed for resume'}."
            )

    if successes == 0:
        raise RuntimeError("All configured eBay seller searches failed; refusing to update state")

    state["sellers"] = sellers_state
    state["seller_cursor"] = next_cursor
    state["last_run"] = detected_at
    state["last_successful_sellers"] = successes
    state["last_failed_sellers"] = failures
    write_json(runtime / "proposed-state.json", state)
    write_json(runtime / "latest-snapshot.json", {
        "checked_at": detected_at,
        "configured_sellers": len(sellers),
        "selected_sellers": len(selected_sellers),
        "quota": quota,
        "successful_sellers": successes,
        "failed_sellers": failures,
        "baselines_seeded": baselines,
        "incomplete_sellers": incomplete_sellers,
        "new_candidates": candidates,
    })

    issue_count = write_issue_packets(runtime, candidates, detected_at, failures)
    if candidates:
        write_json(runtime / "new-items.json", candidates)

    set_output("new_count", len(candidates))
    set_output("issue_count", issue_count)
    set_output("state_changed", "true")
    set_output("successful_requests", successes)
    set_output("failed_requests", len(failures))
    print(
        f"Seller sweep complete: {successes}/{len(selected_sellers)} selected sellers succeeded "
        f"from {len(sellers)} configured; "
        f"{baselines} baselines seeded; {len(incomplete_sellers)} resumable windows pending; "
        f"{len(candidates)} candidates in {issue_count} issue packets; {len(failures)} failures."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
