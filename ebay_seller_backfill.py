#!/usr/bin/env python3
"""Resumable scan of current Books inventory from configured eBay sellers."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import canon_runner
import ebay_api
import ebay_seller_monitor as live_monitor
import external_monitor

PAGE_SIZE = 200
DEFAULT_CALL_BUDGET = 300
MAX_OFFSET = 9800
ISSUE_ITEM_LIMIT = 50
EBAY_EPOCH = "1995-01-01T00:00:00Z"


def load_json(path: Path, default: dict[str, Any], label: str) -> dict[str, Any]:
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} is not a JSON object")
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


@lru_cache(maxsize=1)
def _canon_token_index() -> tuple[tuple[dict[str, Any], ...], dict[str, set[int]]]:
    rows = canon_runner.load_canon_master()
    index: dict[str, set[int]] = {}
    for row_index, row in enumerate(rows):
        for token in row.get("_title_tokens") or set():
            index.setdefault(str(token), set()).add(row_index)
    return rows, index


def might_match_canon(item: dict[str, Any]) -> bool:
    """Cheaply shortlist rows before invoking the full fuzzy canon matcher."""
    rows, index = _canon_token_index()
    title_norm = canon_runner.pb.normalize(item.get("title"))
    full_norm = canon_runner.pb.normalize(
        f"{item.get('title') or ''} {item.get('context') or ''} {item.get('vendor') or ''}"
    )
    listing_tokens = set(full_norm.split())
    candidate_indexes: set[int] = set()
    for token in listing_tokens:
        candidate_indexes.update(index.get(token, set()))

    for row_index in candidate_indexes:
        row = rows[row_index]
        title_tokens = set(row.get("_title_tokens") or set())
        if not title_tokens:
            continue
        overlap = len(title_tokens & listing_tokens) / len(title_tokens)
        contributor_hit = bool(set(row.get("_contributor_tokens") or set()) & listing_tokens)
        exact = bool(row.get("_title_norm") and str(row["_title_norm"]) in full_norm)
        if exact:
            return True
        if len(title_tokens) <= 2:
            if overlap == 1.0 and (contributor_hit or str(row.get("_title_norm")) in title_norm):
                return True
        elif overlap >= 0.60 or (contributor_hit and overlap >= 0.45):
            return True
    return False


def classify(item: dict[str, Any]) -> dict[str, Any] | None:
    text = " ".join([str(item.get("title") or ""), str(item.get("context") or "")]).lower()
    plausible = external_monitor.plausible(item)
    matches = canon_runner.pb.matches_for_item(item) if might_match_canon(item) else []
    if not plausible and not matches:
        return None

    signals: list[str] = []
    score = 0
    if matches:
        signals.append("Parr/Badger or Roth canon match")
        score += 1000 + int(matches[0].get("score") or 0)
    if any(term in text for term in external_monitor.TARGET_TERMS):
        signals.append("known photographer or title term")
        score += 100
    if any(term in text for term in external_monitor.PUBLISHER_TERMS):
        signals.append("specialist photobook publisher")
        score += 50
    if any(term in text for term in external_monitor.DIRECT_PHOTO_TERMS):
        signals.append("photography wording")
        score += 20
    visual = any(term in text for term in external_monitor.VISUAL_ART_TERMS)
    edition = any(term in text for term in external_monitor.EDITION_TERMS)
    if visual and edition:
        signals.append("visual-art and collectible-edition wording")
        score += 40
    price_gbp = item.get("price_gbp")
    if isinstance(price_gbp, (int, float)):
        if price_gbp <= 20:
            score += 25
        elif price_gbp <= 50:
            score += 10

    candidate = dict(item)
    candidate["qualification_signals"] = signals or ["photobook-radar wording"]
    candidate["rank_score"] = score
    if matches:
        candidate["parr_badger_matches"] = matches
    return candidate


def scan_page(
    client: ebay_api.EbayBrowseClient,
    seller: dict[str, str],
    offset: int,
    item_end_date: str | None = None,
) -> tuple[list[dict[str, Any]], int, str | None]:
    source = {
        "id": f"ebay_backfill_{seller['marketplace'].lower()}_{seller['id'].lower()}",
        "name": f"eBay seller {seller['id']}",
        "marketplace": seller["marketplace"],
    }
    rows = client.search(
        None,
        limit=PAGE_SIZE,
        offset=offset,
        category_ids=live_monitor.BOOKS_CATEGORY_ID,
        fixed_price_only=True,
        seller_ids=[seller["id"]],
        delivery_country=seller.get("delivery_country"),
        item_start_date=EBAY_EPOCH if item_end_date else None,
        item_end_date=item_end_date,
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        item = ebay_api.listing_from_summary(row, source)
        if item is None:
            continue
        item["seller_id"] = seller["id"]
        item["marketplace"] = seller["marketplace"]
        item["source_page"] = live_monitor.seller_url(seller["marketplace"], seller["id"])
        items.append(item)
    creation_dates = [
        str(row.get("itemCreationDate") or "").strip()
        for row in rows
        if str(row.get("itemCreationDate") or "").strip()
    ]
    oldest_creation = min(creation_dates) if creation_dates else None
    return items, len(rows), oldest_creation


def before_timestamp(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    boundary = parsed.astimezone(timezone.utc) - timedelta(milliseconds=1)
    return boundary.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def progress_entry(state: dict[str, Any], seller: dict[str, str]) -> dict[str, Any]:
    sellers_state = state.setdefault("sellers", {})
    key = live_monitor.seller_key(seller["marketplace"], seller["id"])
    entry = sellers_state.get(key)
    if not isinstance(entry, dict):
        entry = {
            "seller_id": seller["id"],
            "marketplace": seller["marketplace"],
            "next_offset": 0,
            "pages_scanned": 0,
            "books_scanned": 0,
            "complete": False,
        }
        sellers_state[key] = entry
    return entry


def run_backfill(
    sellers: list[dict[str, str]],
    state: dict[str, Any],
    findings: dict[str, Any],
    clients: dict[str, ebay_api.EbayBrowseClient],
    *,
    call_budget: int,
    detected_at: str,
) -> dict[str, Any]:
    findings_items = findings.setdefault("items", {})
    if not isinstance(findings_items, dict):
        raise RuntimeError("eBay seller backfill findings items is not an object")

    cursor = int(state.get("cursor_index") or 0) % len(sellers)
    calls = 0
    successful_pages = 0
    books_scanned = 0
    new_candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    failed_keys: set[str] = set()

    # States written by the first version stopped at eBay's 10,000-result
    # offset ceiling. Reopen them once, recover the oldest page timestamp,
    # then continue in older date-bounded segments.
    for seller in sellers:
        entry = progress_entry(state, seller)
        if entry.get("capped_at_10000") and not entry.get("date_partitions_complete"):
            entry["complete"] = False
            entry["boundary_recovery"] = True

    while calls < call_budget:
        attempted_this_round = 0
        for _ in range(len(sellers)):
            seller = sellers[cursor]
            cursor = (cursor + 1) % len(sellers)
            key = live_monitor.seller_key(seller["marketplace"], seller["id"])
            entry = progress_entry(state, seller)
            if entry.get("complete") or key in failed_keys:
                continue
            attempted_this_round += 1
            calls += 1
            offset = int(entry.get("next_offset") or 0)
            try:
                items, raw_count, oldest_creation = scan_page(
                    clients[seller["marketplace"]],
                    seller,
                    offset,
                    str(entry.get("segment_end") or "") or None,
                )
            except Exception as exc:
                warning = f"{seller['marketplace']} {seller['id']} at offset {offset}: {exc}"
                failures.append(warning)
                failed_keys.add(key)
                print("WARNING:", warning, file=sys.stderr)
                if calls >= call_budget:
                    break
                continue

            successful_pages += 1
            books_scanned += raw_count
            entry["pages_scanned"] = int(entry.get("pages_scanned") or 0) + 1
            entry["books_scanned"] = int(entry.get("books_scanned") or 0) + raw_count
            entry["last_successful_page"] = detected_at

            for item in items:
                candidate = classify(item)
                if candidate is None or candidate["key"] in findings_items:
                    continue
                candidate["backfill_first_found"] = detected_at
                findings_items[candidate["key"]] = candidate
                new_candidates.append(candidate)

            if raw_count < PAGE_SIZE:
                entry["complete"] = True
                entry["date_partitions_complete"] = True
                entry["completed_at"] = detected_at
                entry.pop("boundary_recovery", None)
                print(
                    f"{seller['marketplace']} {seller['id']}: complete after "
                    f"{entry['pages_scanned']} page(s) and {entry['books_scanned']} books."
                )
            elif offset >= MAX_OFFSET:
                next_segment_end = before_timestamp(oldest_creation or "")
                if next_segment_end:
                    entry["capped_at_10000"] = True
                    entry["capped_segments"] = int(entry.get("capped_segments") or 0) + 1
                    entry["segment_end"] = next_segment_end
                    entry["next_offset"] = 0
                    entry["complete"] = False
                    entry.pop("boundary_recovery", None)
                    print(
                        f"{seller['marketplace']} {seller['id']}: opened an older date segment "
                        f"after reaching the 10,000-result window."
                    )
                else:
                    entry["complete"] = True
                    entry["unresolved_10000_cap"] = True
                    entry["completed_at"] = detected_at
                    print(
                        f"{seller['marketplace']} {seller['id']}: could not derive a date boundary "
                        f"beyond the 10,000-result window."
                    )
            else:
                entry["next_offset"] = offset + PAGE_SIZE

            if calls >= call_budget:
                break

        if attempted_this_round == 0:
            break

    if calls and successful_pages == 0:
        raise RuntimeError("All attempted eBay backfill pages failed; refusing to advance state")

    state["cursor_index"] = cursor
    state["last_run"] = detected_at
    state["last_run_calls"] = calls
    state["last_run_successful_pages"] = successful_pages
    state["last_run_books_scanned"] = books_scanned
    state["last_run_failures"] = failures
    state["total_calls"] = int(state.get("total_calls") or 0) + calls
    state["total_books_scanned"] = int(state.get("total_books_scanned") or 0) + books_scanned
    findings["items"] = findings_items
    findings["last_updated"] = detected_at

    completed = sum(1 for seller in sellers if progress_entry(state, seller).get("complete"))
    return {
        "calls": calls,
        "successful_pages": successful_pages,
        "books_scanned": books_scanned,
        "new_candidates": new_candidates,
        "failures": failures,
        "completed_sellers": completed,
        "remaining_sellers": len(sellers) - completed,
    }


def price_text(item: dict[str, Any]) -> str:
    value = item.get("price_value")
    currency = str(item.get("price_currency") or "")
    if not isinstance(value, (int, float)):
        return "Not supplied"
    if currency == "GBP":
        return f"£{value:.2f}"
    return f"{currency} {value:.2f}".strip()


def issue_body(result: dict[str, Any], total_sellers: int, total_findings: int) -> str:
    ranked = sorted(
        result["new_candidates"],
        key=lambda item: (int(item.get("rank_score") or 0), item.get("price_gbp") is not None),
        reverse=True,
    )
    shown = ranked[:ISSUE_ITEM_LIMIT]
    lines = [
        "## Current-inventory eBay seller back-search",
        "",
        "This is historical current stock, not newly listed stock. It is intentionally separate from the live `CHARITY_NEW:` alert stream.",
        "",
        f"- API pages used this run: **{result['calls']}**",
        f"- Books inspected this run: **{result['books_scanned']}**",
        f"- Sellers completed: **{result['completed_sellers']} of {total_sellers}**",
        f"- New candidates stored this run: **{len(ranked)}**",
        f"- Total candidates stored so far: **{total_findings}**",
        "",
        f"The top {len(shown)} newly found candidates from this run are shown below. Verify edition, condition, completeness, delivery cost and value before buying.",
        "",
    ]
    for item in shown:
        lines.extend([
            f"### {item.get('title') or 'Untitled listing'}",
            "",
            f"- **Seller:** {item['seller_id']} ({item['marketplace']})",
            f"- **Observed price:** {price_text(item)}",
            f"- **Why it surfaced:** {', '.join(item.get('qualification_signals') or [])}",
            f"- **Rank score:** {item.get('rank_score') or 0}",
            f"- **Listing:** {item['url']}",
            f"- **Seller page:** {item['source_page']}",
        ])
        for match in (item.get("parr_badger_matches") or [])[:2]:
            volumes = str(match.get("volumes") or "")
            canon = "Roth 101" if volumes == "R101" else f"Parr/Badger V{volumes}"
            lines.append(
                f"- **Canon match:** {canon}: {match.get('contributor')}, "
                f"*{match.get('title')}* ({match.get('score')}/100)"
            )
        lines.append("")
    if len(ranked) > len(shown):
        lines.extend([
            f"A further **{len(ranked) - len(shown)}** candidates from this run are retained in the [complete findings file](https://github.com/jonattenborough/oxfam-photobook-monitor/blob/main/data/ebay_seller_backfill_findings.json).",
            "",
        ])
    if result["failures"]:
        lines.extend(["### Temporary page failures", ""])
        lines.extend(f"- {failure}" for failure in result["failures"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_sellers.json")
    parser.add_argument("--state", default="data/ebay_seller_backfill_state.json")
    parser.add_argument("--findings", default="data/ebay_seller_backfill_findings.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-seller-backfill")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_CALL_BUDGET)
    args = parser.parse_args()

    sellers = live_monitor.load_config(Path(args.config))
    state = load_json(Path(args.state), {"version": 1, "sellers": {}}, "Backfill state")
    findings = load_json(Path(args.findings), {"version": 1, "items": {}}, "Backfill findings")
    detected_at = live_monitor.utc_now()
    clients = {
        marketplace: ebay_api.EbayBrowseClient(marketplace=marketplace)
        for marketplace in sorted({seller["marketplace"] for seller in sellers})
    }

    result = run_backfill(
        sellers,
        state,
        findings,
        clients,
        call_budget=max(1, min(args.max_calls, DEFAULT_CALL_BUDGET)),
        detected_at=detected_at,
    )
    runtime = Path(args.runtime_dir)
    write_json(runtime / "proposed-state.json", state)
    write_json(runtime / "proposed-findings.json", findings)
    write_json(runtime / "latest-snapshot.json", result)

    new_candidates = result["new_candidates"]
    if new_candidates:
        title = (
            f"EBAY_BACKFILL: {len(new_candidates)} current-stock photobook candidates | "
            f"{result['completed_sellers']}/{len(sellers)} sellers complete"
        )
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            issue_body(result, len(sellers), len(findings["items"])), encoding="utf-8"
        )

    set_output("new_count", len(new_candidates))
    set_output("state_changed", "true" if result["calls"] else "false")
    set_output("remaining_sellers", result["remaining_sellers"])
    set_output("complete", "true" if result["remaining_sellers"] == 0 else "false")
    print(
        f"Backfill run complete: {result['calls']} calls, {result['books_scanned']} books, "
        f"{len(new_candidates)} new candidates, {result['completed_sellers']}/{len(sellers)} sellers complete."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
