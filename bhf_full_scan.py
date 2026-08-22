#!/usr/bin/env python3
"""Exhaustively scan the public British Heart Foundation eBay Books inventory.

The BHF storefront currently renders listing links with unquoted href attributes,
which the lightweight external monitor intentionally does not parse. This scanner
uses the storefront card markup directly, walks every page, records every unique
item ID, and refuses to report success if pagination repeats or appears incomplete.
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
    "_i.html?store_cat=3893944012&_sop=10"
)
OUT = Path("data/bhf_full_scan.json")
MAX_PAGES = 60

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
    "sebastiao salgado", "sebastião salgado", "rene burri", "rené burri",
    "larry towelly", "larry towell", "alex webb", "trent parke", "antoine d'agata",
    "antoine d’agata", "raghu rai", "george rodger", "ian berry", "philip jones griffiths",
]

GENERIC_LEAD_TERMS = [
    "photography books", "photo books", "photobook", "photo book", "photographs",
    "photo album", "photo albums", "art books", "art book bundle", "book bundle",
    "illustrated books", "exhibition catalogue", "exhibition catalog", "portfolio",
    "contact sheets", "picture book", "picture books",
]

# Avoid the bare substring "art", which would match the word "heart" in BHF page text.
VISUAL_HINTS = [
    "artist", "fine art", "architecture", "architectural", "fashion", "design",
    "portrait", "portraits", "illustrated", "exhibition", "catalogue", "catalog",
    "monograph", "portfolio", "visual culture", "museum", "gallery",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def page_url(page: int) -> str:
    parsed = urllib.parse.urlsplit(BASE_URL)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != "_pgn"]
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
    matches = [m for m in CARD_LINK_RE.finditer(page_html) if "str-item-card__link" in (m.group("attrs") + m.group("tail"))]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, match in enumerate(matches):
        item_id = match.group("id")
        if item_id in seen:
            continue
        seen.add(item_id)

        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page_html), match.start() + 24000)
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

    targets = [t for t in [*TARGET_TERMS, *EXTRA_TARGETS] if t in text]
    if targets:
        score += 30 + min(12, 4 * (len(targets) - 1))
        reasons.append("target: " + ", ".join(targets[:4]))

    photo = [t for t in DIRECT_PHOTO_TERMS if t in text]
    if photo:
        score += 14
        reasons.append("photography: " + ", ".join(photo[:4]))

    editions = [t for t in EDITION_TERMS if t in text]
    if editions:
        score += 10 + min(8, 2 * (len(editions) - 1))
        reasons.append("edition: " + ", ".join(editions[:4]))

    publishers = [t for t in PUBLISHER_TERMS if t in text]
    if publishers:
        score += 8
        reasons.append("publisher: " + ", ".join(publishers[:3]))

    generic = [t for t in GENERIC_LEAD_TERMS if t in text]
    if generic:
        score += 10
        reasons.append("generic lead: " + ", ".join(generic[:3]))

    visual = [t for t in VISUAL_HINTS if t in text]
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
    first_page_size: int | None = None
    catalogue_count: int | None = None
    completed_reason = ""

    for page in range(1, MAX_PAGES + 1):
        url = page_url(page)
        page_html = request_html(url)
        if page == 1:
            catalogue_count = advertised_count(page_html)

        batch = parse_store_page(page_html, url)
        ids = [str(item["external_id"]) for item in batch]
        new_ids = [item_id for item_id in ids if item_id not in by_id]

        page_stats.append(
            {
                "page": page,
                "url": url,
                "parsed_items": len(batch),
                "new_unique_items": len(new_ids),
            }
        )
        print(f"Page {page}: {len(batch)} listings, {len(new_ids)} new unique")

        if page == 1:
            if not batch:
                raise RuntimeError("BHF first page fetched but no storefront cards were parsed")
            first_page_size = len(batch)
        elif batch and not new_ids:
            raise RuntimeError(
                f"BHF page {page} repeated already-seen inventory; refusing to claim a full scan"
            )

        if not batch:
            completed_reason = f"page {page} contained no listings"
            break

        for item in batch:
            by_id[str(item["external_id"])] = item

        if first_page_size and len(batch) < first_page_size:
            completed_reason = f"page {page} was the final short page"
            break
    else:
        raise RuntimeError(f"BHF scan reached safety limit of {MAX_PAGES} pages")

    total = len(by_id)
    if total < 100:
        raise RuntimeError(f"Only {total} unique BHF books were parsed; refusing to mark scan complete")

    if catalogue_count is not None:
        minimum_expected = max(1, int(catalogue_count * 0.90))
        if total < minimum_expected:
            raise RuntimeError(
                f"Parsed only {total} unique BHF books versus advertised {catalogue_count}; "
                "refusing to mark scan complete"
            )

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
        key=lambda x: (
            -x["score"],
            x["price_gbp"] if isinstance(x.get("price_gbp"), (int, float)) else 999999,
            str(x.get("title") or "").lower(),
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
    all_items.sort(key=lambda x: str(x.get("title") or "").lower())

    snapshot = {
        "generated_at": now_utc(),
        "source": "British Heart Foundation eBay Books",
        "source_url": BASE_URL,
        "advertised_catalogue_count": catalogue_count,
        "unique_items_scanned": total,
        "pages_scanned": len(page_stats),
        "completion_reason": completed_reason,
        "full_scan_complete": True,
        "candidate_count": len(candidates),
        "page_stats": page_stats,
        "candidates": candidates,
        "all_items": all_items,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"BHF full scan complete: {total} unique books across {len(page_stats)} pages; "
        f"{len(candidates)} broad candidates"
    )
    if catalogue_count is not None:
        print(f"Advertised catalogue count: {catalogue_count}")
    print(f"Completion: {completed_reason}")
    print("Top 60 candidates:")
    for item in candidates[:60]:
        price = f"£{item['price_gbp']:.2f}" if isinstance(item.get("price_gbp"), (int, float)) else "price n/a"
        print(f"{item['score']:>3} | {price:>10} | {item['title']} | {item['url']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
