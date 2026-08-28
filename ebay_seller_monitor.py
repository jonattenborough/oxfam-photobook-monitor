#!/usr/bin/env python3
"""Monitor selected eBay charity sellers for newly listed photobooks."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import canon_runner
import ebay_api
import external_monitor

BOOKS_CATEGORY_ID = "261186"
PAGE_SIZE = 200
MAX_INCREMENTAL_PAGES = 5
OVERLAP_MINUTES = 10
MAX_SEEN_PER_SELLER = 1000
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
        return {"version": 1, "sellers": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("eBay seller monitor state is not a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("sellers", {})
    if not isinstance(payload["sellers"], dict):
        raise RuntimeError("eBay seller monitor sellers state is not an object")
    return payload


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


def scan_seller(
    client: ebay_api.EbayBrowseClient,
    seller: dict[str, str],
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    initialized = bool(previous and previous.get("initialized"))
    start_date = incremental_start(previous.get("last_successful_fetch")) if initialized and previous else None
    page_limit = MAX_INCREMENTAL_PAGES if initialized else 1
    source = {
        "id": f"ebay_seller_{seller['marketplace'].lower()}_{seller['id'].lower()}",
        "name": f"eBay seller {seller['id']}",
        "marketplace": seller["marketplace"],
    }
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for page in range(page_limit):
        rows = client.search(
            None,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
            category_ids=BOOKS_CATEGORY_ID,
            fixed_price_only=True,
            seller_ids=[seller["id"]],
            delivery_country=seller.get("delivery_country"),
            item_start_date=start_date,
        )
        for row in rows:
            item = ebay_api.listing_from_summary(row, source)
            if item is None or item["key"] in seen_keys:
                continue
            seen_keys.add(item["key"])
            item["seller_id"] = seller["id"]
            item["marketplace"] = seller["marketplace"]
            item["source_page"] = seller_url(seller["marketplace"], seller["id"])
            items.append(item)
        if len(rows) < PAGE_SIZE:
            break
        if page + 1 == page_limit and initialized:
            raise RuntimeError(
                f"more than {PAGE_SIZE * page_limit} new results fell inside the incremental window"
            )
    return items


def qualification(item: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    text = " ".join([str(item.get("title") or ""), str(item.get("context") or "")]).lower()
    signals: list[str] = []
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
    if not external_monitor.plausible(item) and not matches:
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
                if matches:
                    candidate["parr_badger_matches"] = matches
                candidates.append(candidate)
        elif isinstance(prior_item, dict):
            prior_item["last_seen"] = detected_at

    updated["initialized"] = True
    updated["last_successful_fetch"] = detected_at
    updated["last_result_count"] = len(items)
    updated["seen"] = _trim_seen(seen)
    return updated, candidates, False


def _price_line(item: dict[str, Any]) -> str | None:
    value = item.get("price_value")
    currency = str(item.get("price_currency") or "")
    if value is None:
        return None
    if currency == "GBP":
        return f"- **Observed price:** £{value:.2f}"
    return f"- **Observed price:** {currency} {value:.2f}".rstrip()


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
    for item in items:
        lines.extend([
            f"### {item.get('title') or 'Untitled listing'}",
            "",
            f"- **Seller:** {item['seller_id']}",
            f"- **Marketplace:** {item['marketplace']}",
        ])
        price = _price_line(item)
        if price:
            lines.append(price)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_sellers.json")
    parser.add_argument("--state", default="data/ebay_seller_state.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-sellers")
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
    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    successes = 0
    baselines = 0

    for seller in sellers:
        key = seller_key(seller["marketplace"], seller["id"])
        previous = sellers_state.get(key)
        if not isinstance(previous, dict):
            previous = None
        try:
            items = scan_seller(clients[seller["marketplace"]], seller, previous)
            updated, seller_candidates, was_baseline = update_seller_state(previous, items, detected_at)
        except Exception as exc:
            warning = f"{seller['marketplace']} {seller['id']}: {exc}"
            failures.append(warning)
            print("WARNING:", warning, file=sys.stderr)
            continue

        sellers_state[key] = updated
        candidates.extend(seller_candidates)
        successes += 1
        if was_baseline:
            baselines += 1
            print(f"{seller['marketplace']} {seller['id']}: silent baseline seeded with {len(items)} books.")
        else:
            print(
                f"{seller['marketplace']} {seller['id']}: {len(items)} recent books checked; "
                f"{len(seller_candidates)} new candidates."
            )

    if successes == 0:
        raise RuntimeError("All configured eBay seller searches failed; refusing to update state")

    state["sellers"] = sellers_state
    state["last_run"] = detected_at
    state["last_successful_sellers"] = successes
    state["last_failed_sellers"] = failures
    write_json(runtime / "proposed-state.json", state)
    write_json(runtime / "latest-snapshot.json", {
        "checked_at": detected_at,
        "configured_sellers": len(sellers),
        "successful_sellers": successes,
        "failed_sellers": failures,
        "baselines_seeded": baselines,
        "new_candidates": candidates,
    })

    if candidates:
        write_json(runtime / "new-items.json", candidates)
        title = (
            f"CHARITY_NEW: {len(candidates)} eBay seller photobook "
            f"candidate{'s' if len(candidates) != 1 else ''}"
        )
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            make_issue_body(candidates, detected_at, failures), encoding="utf-8"
        )

    set_output("new_count", len(candidates))
    set_output("state_changed", "true")
    set_output("successful_requests", successes)
    set_output("failed_requests", len(failures))
    print(
        f"Seller sweep complete: {successes}/{len(sellers)} sellers succeeded; "
        f"{baselines} baselines seeded; {len(candidates)} candidates; {len(failures)} failures."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
