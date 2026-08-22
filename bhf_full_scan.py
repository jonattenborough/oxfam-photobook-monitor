#!/usr/bin/env python3
"""Exhaustively scan British Heart Foundation's eBay books inventory.

The old storefront HTML scraper is unreliable from GitHub-hosted runners because
eBay often serves a page without parseable listing data. This scanner instead uses
eBay's official Browse API, filtered to seller `bhf_shops` and the UK Books, Comics
& Magazines category, then ranks the complete result set for photobook interest.

Required repository secrets:
  EBAY_CLIENT_ID
  EBAY_CLIENT_SECRET
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import canon_runner
from external_monitor import (
    DIRECT_PHOTO_TERMS,
    EDITION_TERMS,
    PUBLISHER_TERMS,
    TARGET_TERMS,
    VISUAL_ART_TERMS,
)

OUT = Path("data/bhf_full_scan.json")
SELLER = "bhf_shops"
EBAY_GB_BOOKS_CATEGORY = "267"
OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
PAGE_SIZE = 100
MAX_ITEMS = 10000

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
    "philip-lorca dicorcia", "roe ethridge", "terry richardson", "juergen teller",
    "wolfgang tillmans", "andreas gursky", "thomas struth", "thomas ruff",
    "bernd becher", "hilla becher", "new topographics", "john szarkowski",
]

GENERIC_LEAD_TERMS = [
    "photography books", "photo books", "photobook", "photographs", "photo album",
    "photo albums", "art books", "art book bundle", "book bundle", "illustrated books",
    "exhibition catalogue", "exhibition catalog", "portfolio", "contact sheets",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def require_credentials() -> tuple[str, str]:
    client_id = os.getenv("EBAY_CLIENT_ID", "").strip()
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Official eBay API credentials are not configured. Add repository Actions secrets "
            "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET, then run this workflow again."
        )
    return client_id, client_secret


def json_request(req: urllib.request.Request, timeout: int = 45) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"eBay API HTTP {exc.code}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"eBay API request failed: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"eBay API returned invalid JSON: {raw[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("eBay API returned a non-object JSON response")
    return data


def application_token(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    data = json_request(req)
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("eBay OAuth response did not contain an access token")
    return token


def legacy_id(item: dict[str, Any]) -> str:
    direct = str(item.get("legacyItemId") or "").strip()
    if direct:
        return direct
    raw = str(item.get("itemId") or "")
    match = re.search(r"(?:^|\|)(\d{9,15})(?:\||$)", raw)
    return match.group(1) if match else raw


def price_gbp(item: dict[str, Any]) -> float | None:
    price = item.get("price")
    if not isinstance(price, dict):
        return None
    if str(price.get("currency") or "").upper() != "GBP":
        return None
    try:
        return round(float(price.get("value")), 2)
    except (TypeError, ValueError):
        return None


def normalise_item(item: dict[str, Any]) -> dict[str, Any]:
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    context_parts = [
        str(item.get("shortDescription") or ""),
        str(item.get("condition") or ""),
        str(item.get("conditionId") or ""),
        str(seller.get("username") or ""),
        " ".join(str(x) for x in (item.get("buyingOptions") or []) if x),
        str(item.get("itemCreationDate") or ""),
        str(item.get("itemEndDate") or ""),
    ]
    return {
        "external_id": legacy_id(item),
        "title": str(item.get("title") or "Untitled eBay item"),
        "price_gbp": price_gbp(item),
        "url": str(item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or ""),
        "context": " | ".join(x for x in context_parts if x),
        "condition": item.get("condition"),
        "item_creation_date": item.get("itemCreationDate"),
        "item_end_date": item.get("itemEndDate"),
    }


def fetch_all_items(token: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    by_id: dict[str, dict[str, Any]] = {}
    page_stats: list[dict[str, Any]] = []
    offset = 0
    reported_total: int | None = None

    while offset < MAX_ITEMS:
        params = {
            "category_ids": EBAY_GB_BOOKS_CATEGORY,
            "filter": f"sellers:{{{SELLER}}}",
            "limit": str(PAGE_SIZE),
            "offset": str(offset),
            "sort": "newlyListed",
        }
        url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            },
            method="GET",
        )
        data = json_request(req)
        if reported_total is None:
            try:
                reported_total = int(data.get("total"))
            except (TypeError, ValueError):
                reported_total = None

        raw_items = data.get("itemSummaries")
        batch = raw_items if isinstance(raw_items, list) else []
        normalised = [normalise_item(x) for x in batch if isinstance(x, dict)]
        new_count = 0
        for row in normalised:
            key = str(row.get("external_id") or "")
            if not key:
                continue
            if key not in by_id:
                new_count += 1
            by_id[key] = row

        page_stats.append({
            "offset": offset,
            "returned_items": len(batch),
            "new_unique_items": new_count,
        })

        if not data.get("next"):
            break
        if not batch:
            raise RuntimeError("eBay Browse API supplied a next page but returned no items")
        offset += PAGE_SIZE
    else:
        raise RuntimeError("BHF API scan reached eBay's 10,000-result safety boundary")

    if not by_id:
        raise RuntimeError("eBay Browse API returned zero BHF items in category 267")
    return list(by_id.values()), page_stats, reported_total


def text_for(item: dict[str, Any]) -> str:
    return " ".join([str(item.get("title") or ""), str(item.get("context") or "")]).lower()


def canon_matches(item: dict[str, Any]) -> list[dict[str, Any]]:
    return canon_runner.pb.match_listing(
        title=item.get("title"),
        description=item.get("context"),
        limit=3,
    )


def score_item(item: dict[str, Any]) -> tuple[int, list[str], list[dict[str, Any]]]:
    text = text_for(item)
    score = 0
    reasons: list[str] = []
    matches = canon_matches(item)

    if matches:
        best = matches[0]
        score += 65
        label = "Roth 101" if str(best.get("volumes")) == "R101" else f"Parr/Badger V{best.get('volumes')}"
        if "Roth 101" in str(best.get("pb_refs") or "") and str(best.get("volumes")) != "R101":
            label += " + Roth 101"
        reasons.append(f"canon: {label} {best.get('title')} ({best.get('score')}/100)")

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

    if any(t in text for t in VISUAL_ART_TERMS):
        score += 4
        reasons.append("visual-art signal")

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

    return score, reasons, matches


def main() -> int:
    client_id, client_secret = require_credentials()
    token = application_token(client_id, client_secret)
    items, page_stats, reported_total = fetch_all_items(token)

    candidates: list[dict[str, Any]] = []
    for item in items:
        score, reasons, matches = score_item(item)
        if score < 4:
            continue
        candidates.append({
            **item,
            "score": score,
            "score_reasons": reasons,
            "canon_matches": matches,
        })

    candidates.sort(key=lambda x: (
        -int(x.get("score") or 0),
        x.get("price_gbp") if isinstance(x.get("price_gbp"), (int, float)) else 999999,
        str(x.get("title") or "").lower(),
    ))
    items.sort(key=lambda x: str(x.get("title") or "").lower())

    snapshot = {
        "generated_at": now_utc(),
        "source": "British Heart Foundation eBay via official Browse API",
        "seller": SELLER,
        "ebay_marketplace": "EBAY_GB",
        "ebay_category_id": EBAY_GB_BOOKS_CATEGORY,
        "api_reported_total": reported_total,
        "unique_items_scanned": len(items),
        "pages_scanned": len(page_stats),
        "completion_reason": "followed Browse API pagination until no next page",
        "full_scan_complete": True,
        "candidate_count": len(candidates),
        "page_stats": page_stats,
        "candidates": candidates,
        "all_items": items,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"BHF API full scan complete: {len(items)} unique listings across "
        f"{len(page_stats)} API pages; {len(candidates)} broad candidates"
    )
    if reported_total is not None:
        print(f"eBay API reported total: {reported_total}")
    print("Top 40 candidates:")
    for item in candidates[:40]:
        price = f"£{item['price_gbp']:.2f}" if isinstance(item.get("price_gbp"), (int, float)) else "price n/a"
        print(f"{item['score']:>3} | {price:>10} | {item['title']} | {item['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
