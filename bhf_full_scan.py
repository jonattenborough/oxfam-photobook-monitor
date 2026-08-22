#!/usr/bin/env python3
"""Exhaustively scan the public British Heart Foundation eBay Books inventory.

The BHF storefront renders 48 listing cards per page and caps each sort order at
10 pages. A single sort therefore exposes at most 480 books even when the store
contains more than 1,000. This scanner sweeps several independent sort orders,
deduplicates by eBay item ID, and refuses to report completeness unless the union
matches the live catalogue count closely enough to account only for listings that
may change while the scan itself is running.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from external_monitor import (
    DIRECT_PHOTO_TERMS,
    EDITION_TERMS,
    PRICE_RE,
    PUBLISHER_TERMS,
    TARGET_TERMS,
    request_html,
    strip_html,
)

BASE_URL = (
    "https://www.ebay.co.uk/str/britishheartfoundationshop/BOOKS/"
    "_i.html?store_cat=3893944012"
)
OUT = Path("data/bhf_full_scan.json")
PAGES_PER_SORT = 10

# eBay storefront sort codes. Price-low and price-high are especially useful
# because together they expose opposite ends of a catalogue that is larger than
# the 480-item per-sort storefront cap.
SORT_STRATEGIES = [
    ("newly_listed", "10"),
    ("ending_soonest", "1"),
    ("price_low", "15"),
    ("price_high", "16"),
    ("best_match", "12"),
]

COUNT_PATTERNS = [
    re.compile(r"Search\s+all\s+([0-9,]+)\s+items", re.IGNORECASE),
    re.compile(r"([0-9,]+)\s+items\s+found", re.IGNORECASE),
]

CARD_LINK_RE = re.compile(
    r"<a\s+href=(?P<href>https://www\.ebay\.co\.uk/itm/(?P<id>\d{9,15})[^ >]*)"
    r"(?P<attrs>[^>]*)aria-label=\"(?P<title>[^\"]+)\"(?P<tail>[^>]*)>",
    re.IGNORECASE,
)

EXTRA_TARGETS = [
    "kikuji kawada", "tish murtha", "ken grant", "tom wood", "nick waplington",
    "eikoh hosoe", "masahisa fukase", "shomei tomatsu", "daido moriyama",
    "nobuyoshi araki", "anders petersen", "harry gruyaert", "mark power",
    "tony ray-jones", "ray-jones", "bill burke", "gilles peress", "danny lyon",
    "william klein", "ruth orkin", "eugene richards", "david goldblatt",
    "malick sidibe", "malick sidibé", "guy tillim", "pieter hugo", "roger ballen",
    "joel sternfeld", "mitch epstein", "gregory halpern", "vanessa winship",
    "raymond depardon", "rene burri", "rené burri", "bruce davidson",
    "elliott erwitt", "eve arnold", "w. eugene smith", "eugene smith",
    "minor white", "aaron siskind", "harry callahan", "robert heinecken",
    "jo ann callis", "jo spence", "sharon lockhart", "rinko kawauchi",
    "takuma nakahira", "todd hido", "brian finke", "larry fink",
    "philip-lorca dicorcia", "philip lorca dicorcia", "roe ethridge",
    "juergen teller", "wolfgang tillmans", "andreas gursky", "thomas struth",
    "thomas ruff", "bernd becher", "hilla becher", "new topographics",
    "john szarkowski", "parr badger", "photobook: a history", "photobook a history",
    "sebastiao salgado", "sebastião salgado", "larry towell", "alex webb",
    "trent parke", "antoine d'agata", "antoine d’agata", "raghu rai",
    "george rodger", "ian berry", "philip jones griffiths", "moons of saturn",
    "graciela iturbide", "cristina garcia rodero", "cristina garcía rodero",
    "agata grzybowska", "mark cohen", "friedlander", "winogrand",
]

GENERIC_LEAD_TERMS = [
    "photography books", "photo books", "photobook", "photo book", "photographs",
    "photo album", "photo albums", "art books", "art book bundle", "book bundle",
    "illustrated books", "exhibition catalogue", "exhibition catalog", "portfolio",
    "contact sheets", "picture book", "picture books",
]

# Avoid the bare substring "art", which would match the word "heart" in BHF text.
VISUAL_HINTS = [
    "artist", "fine art", "architecture", "architectural", "fashion", "design",
    "portrait", "portraits", "illustrated", "exhibition", "catalogue", "catalog",
    "monograph", "portfolio", "visual culture", "museum", "gallery",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def page_url(page: int, sort_code: str) -> str:
    parsed = urllib.parse.urlsplit(BASE_URL)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k not in {"_pgn", "_sop"}]
    query.append(("_sop", sort_code))
    if page > 1:
        query.append(("_pgn", str(page)))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )


def advertised_count(page_html: str) -> int | None:
    text = html.unescape(page_html)
    for pattern in COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def parse_store_page(page_html: str, source_url: str) -> list[dict[str, Any]]:
    matches = [
        match
        for match in CARD_LINK_RE.finditer(page_html)
        if "str-item-card__link" in (match.group("attrs") + match.group("tail"))
    ]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, match in enumerate(matches):
        item_id = match.group("id")
        if item_id in seen:
            continue
        seen.add(item_id)

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else min(len(page_html), match.start() + 24000)
        )
        raw_card = page_html[match.start():end]
        context = strip_html(raw_card)
        price_match = PRICE_RE.search(context)
        price = None
        if price_match:
            try:
                price = round(float(price_match.group(1).replace(",", "")), 2)
            except ValueError:
                pass

        title = strip_html(html.unescape(match.group("title")))
        items.append(
            {
                "external_id": item_id,
                "title": title,
                "price_gbp": price,
                "url": f"https://www.ebay.co.uk/itm/{item_id}",
                "source_page": source_url,
                "context": context[:1800],
            }
        )

    return items


def text_for(item: dict[str, Any]) -> str:
    return " ".join([str(item.get("title") or ""), str(item.get("context") or "")]).lower()


def score_item(item: dict[str, Any]) -> tuple[int, list[str]]:
    text = text_for(item)
    score = 0
    reasons: list[str] = []

    targets = [term for term in [*TARGET_TERMS, *EXTRA_TARGETS] if term in text]
    if targets:
        score += 30 + min(12, 4 * (len(targets) - 1))
        reasons.append("target: " + ", ".join(targets[:4]))

    photo = [term for term in DIRECT_PHOTO_TERMS if term in text]
    if photo:
        score += 14
        reasons.append("photography: " + ", ".join(photo[:4]))

    editions = [term for term in EDITION_TERMS if term in text]
    if editions:
        score += 10 + min(8, 2 * (len(editions) - 1))
        reasons.append("edition: " + ", ".join(editions[:4]))

    publishers = [term for term in PUBLISHER_TERMS if term in text]
    if publishers:
        score += 8
        reasons.append("publisher: " + ", ".join(publishers[:3]))

    generic = [term for term in GENERIC_LEAD_TERMS if term in text]
    if generic:
        score += 10
        reasons.append("generic lead: " + ", ".join(generic[:3]))

    visual = [term for term in VISUAL_HINTS if term in text]
    if visual:
        score += 4
        reasons.append("visual-art signal: " + ", ".join(visual[:3]))

    price = item.get("price_gbp")
    if isinstance(price, (int, float)):
        if price <= 10:
            score += 6
            reasons.append("£10 or less")
        elif price <= 20:
            score += 4
            reasons.append("£20 or less")
        elif price <= 50:
            score += 2
            reasons.append("£50 or less")

    return score, reasons


def main() -> int:
    by_id: dict[str, dict[str, Any]] = {}
    page_stats: list[dict[str, Any]] = []
    strategy_stats: list[dict[str, Any]] = []
    catalogue_counts_seen: list[int] = []
    completion_reason = ""

    for strategy_name, sort_code in SORT_STRATEGIES:
        strategy_ids: set[str] = set()
        strategy_start_total = len(by_id)
        first_page_size: int | None = None
        pages_scanned = 0

        for page in range(1, PAGES_PER_SORT + 1):
            url = page_url(page, sort_code)
            page_html = request_html(url)
            pages_scanned += 1

            count = advertised_count(page_html)
            if count is not None:
                catalogue_counts_seen.append(count)

            batch = parse_store_page(page_html, url)
            ids = [str(item["external_id"]) for item in batch]
            new_strategy_ids = [item_id for item_id in ids if item_id not in strategy_ids]
            new_global_ids = [item_id for item_id in ids if item_id not in by_id]

            page_stats.append(
                {
                    "strategy": strategy_name,
                    "sort_code": sort_code,
                    "page": page,
                    "url": url,
                    "parsed_items": len(batch),
                    "new_in_strategy": len(new_strategy_ids),
                    "new_global_items": len(new_global_ids),
                    "global_unique_after_page": len(by_id) + len(new_global_ids),
                }
            )
            print(
                f"{strategy_name} page {page}: {len(batch)} listings, "
                f"{len(new_global_ids)} new globally"
            )

            if page == 1:
                if not batch:
                    raise RuntimeError(
                        f"BHF {strategy_name} first page fetched but no storefront cards were parsed"
                    )
                first_page_size = len(batch)
            elif batch and not new_strategy_ids:
                raise RuntimeError(
                    f"BHF {strategy_name} page {page} repeated an earlier page in the same sort; "
                    "refusing to claim a full scan"
                )

            if not batch:
                break

            strategy_ids.update(ids)
            for item in batch:
                item_id = str(item["external_id"])
                existing = by_id.get(item_id)
                if existing is None:
                    by_id[item_id] = item
                else:
                    # Prefer whichever observation contains a price and the longer card context.
                    if existing.get("price_gbp") is None and item.get("price_gbp") is not None:
                        existing["price_gbp"] = item.get("price_gbp")
                    if len(str(item.get("context") or "")) > len(str(existing.get("context") or "")):
                        existing["context"] = item.get("context")

            if first_page_size and len(batch) < first_page_size:
                break

        added = len(by_id) - strategy_start_total
        strategy_stats.append(
            {
                "strategy": strategy_name,
                "sort_code": sort_code,
                "pages_scanned": pages_scanned,
                "unique_items_in_strategy": len(strategy_ids),
                "new_global_items_added": added,
                "global_unique_after_strategy": len(by_id),
            }
        )
        print(
            f"Strategy {strategy_name}: {len(strategy_ids)} unique visible, "
            f"{added} newly added to union, {len(by_id)} union total"
        )

        target_count = max(catalogue_counts_seen) if catalogue_counts_seen else None
        if target_count is not None and len(by_id) >= target_count:
            completion_reason = f"union reached advertised catalogue count after {strategy_name}"
            break

    total = len(by_id)
    catalogue_count = max(catalogue_counts_seen) if catalogue_counts_seen else None

    if total < 100:
        raise RuntimeError(f"Only {total} unique BHF books were parsed; refusing to mark scan complete")

    if catalogue_count is not None:
        # Permit at most five items of catalogue churn during the multi-request scan.
        shortfall = catalogue_count - total
        if shortfall > 5:
            raise RuntimeError(
                f"Multi-sort union parsed {total} unique BHF books versus advertised "
                f"{catalogue_count}, shortfall {shortfall}; refusing to mark scan complete"
            )
        if not completion_reason:
            completion_reason = (
                f"multi-sort union reached {total} of advertised {catalogue_count}; "
                f"shortfall {max(0, shortfall)} within live-catalogue churn tolerance"
            )
    elif not completion_reason:
        raise RuntimeError("BHF catalogue count was not exposed; refusing to claim completeness")

    candidates: list[dict[str, Any]] = []
    for item in by_id.values():
        score, reasons = score_item(item)
        if score < 4:
            continue
        candidates.append(
            {
                "item_id": item["external_id"],
                "title": item.get("title"),
                "price_gbp": item.get("price_gbp"),
                "url": item.get("url"),
                "context": item.get("context"),
                "score": score,
                "score_reasons": reasons,
            }
        )

    candidates.sort(
        key=lambda item: (
            -item["score"],
            item["price_gbp"] if isinstance(item.get("price_gbp"), (int, float)) else 999999,
            str(item.get("title") or "").lower(),
        )
    )

    all_items = [
        {
            "item_id": item["external_id"],
            "title": item.get("title"),
            "price_gbp": item.get("price_gbp"),
            "url": item.get("url"),
            "context": item.get("context"),
        }
        for item in by_id.values()
    ]
    all_items.sort(key=lambda item: str(item.get("title") or "").lower())

    snapshot = {
        "generated_at": now_utc(),
        "source": "British Heart Foundation eBay Books",
        "source_url": BASE_URL,
        "advertised_catalogue_count": catalogue_count,
        "catalogue_counts_seen": sorted(set(catalogue_counts_seen)),
        "unique_items_scanned": total,
        "full_scan_complete": True,
        "completion_reason": completion_reason,
        "strategies_run": strategy_stats,
        "page_stats": page_stats,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "all_items": all_items,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"BHF full scan complete: {total} unique books; advertised count {catalogue_count}; "
        f"{len(candidates)} broad candidates"
    )
    print(f"Completion: {completion_reason}")
    print("Top 80 candidates:")
    for item in candidates[:80]:
        price = (
            f"£{item['price_gbp']:.2f}"
            if isinstance(item.get("price_gbp"), (int, float))
            else "price n/a"
        )
        print(f"{item['score']:>3} | {price:>10} | {item['title']} | {item['url']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
