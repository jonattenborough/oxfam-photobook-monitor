#!/usr/bin/env python3
"""One-off full-catalogue scan of Oxfam Art & Photography Books."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import monitor

PAGE_SIZE = 60

# High-signal terms that often indicate collectability or a miscatalogued special copy.
SIGNALS: list[tuple[str, int]] = [
    (r"\bsigned\b", 16), (r"\binscribed\b", 14), (r"\bsignature\b", 12),
    (r"\bfirst edition\b", 13), (r"\bfirst printing\b", 15), (r"\bfirst impression\b", 15),
    (r"\blimited edition\b", 13), (r"\bnumbered\b", 10), (r"edition (?:size|of)\s+\d+", 8),
    (r"\bartist.?s proof\b", 18), (r"\bap\b", 4), (r"\boriginal print\b", 18),
    (r"\bphotograph signed\b", 18), (r"\bsigned print\b", 18), (r"\bprint included\b", 14),
    (r"\bslipcase\b", 7), (r"\bslip case\b", 7), (r"\bboxed\b", 6), (r"\bclamshell\b", 9),
    (r"\bglassine\b", 7), (r"\bacetate\b", 6), (r"\bbellyband\b", 8), (r"\bbelly band\b", 8),
    (r"\bfirst monograph\b", 12), (r"\bscarce\b", 7), (r"\brare\b", 5),
    (r"\bassociation copy\b", 18), (r"\bpresentation copy\b", 15),
]

# Canonical / collectible photographers and photobook makers. This is deliberately broad:
# it is a screening list, not a valuation claim.
NAMES = {
    "robert frank": 16, "william klein": 14, "henri cartier-bresson": 12,
    "diane arbus": 15, "walker evans": 14, "stephen shore": 15, "william eggleston": 16,
    "nan goldin": 15, "larry clark": 14, "martin parr": 14, "richard billingham": 14,
    "alec soth": 14, "joel sternfeld": 12, "joel meyerowitz": 11, "lee friedlander": 12,
    "garry winogrand": 12, "bruce davidson": 11, "bruce gilden": 9, "saul leiter": 11,
    "daido moriyama": 14, "shomei tomatsu": 15, "nobuyoshi araki": 11, "eikoh hosoe": 13,
    "masahisa fukase": 15, "ikKo narahara": 10, "hiroshi sugimoto": 11,
    "ed van der elsken": 15, "josef koudelka": 15, "chris killip": 15, "paul graham": 14,
    "raymond depardon": 10, "rene burri": 10, "anders petersen": 13,
    "bernd becher": 13, "hilla becher": 13, "lewis baltz": 14, "robert adams": 13,
    "luigi ghirri": 15, "john szarkowski": 8, "peter beard": 13, "irving penn": 12,
    "richard avedon": 13, "helmut newton": 10, "guy bourdin": 11, "bruce weber": 12,
    "mary ellen mark": 13, "susan meiselas": 14, "sally mann": 14, "corinne day": 14,
    "peter hujar": 14, "robert mapplethorpe": 13, "wolfgang tillmans": 12,
    "ryan mcginley": 9, "juergen teller": 11, "vivian maier": 8, "fan ho": 11,
    "gregory crewdson": 10, "philip-lorca dicorcia": 11, "jeff wall": 10,
    "thomas struth": 10, "thomas ruff": 9, "andreas gursky": 10, "rineke dijkstra": 10,
    "larry sultan": 14, "mike mandel": 12, "jim goldberg": 14, "danny lyon": 13,
    "ralph eugene meatyard": 13, "duane michals": 9, "bill brandt": 11,
    "tony ray-jones": 13, "don mccullin": 10, "roger mayne": 12, "jo spence": 10,
    "tom wood": 11, "mark power": 10, "nick waplington": 10, "seamus murphy": 8,
    "taryn simon": 10, "deanna templeton": 9, "larry fink": 10, "helen levitt": 12,
}

TITLES = {
    "the americans": 20, "les americains": 20, "american photographs": 18,
    "uncommon places": 18, "american surfaces": 17, "william eggleston's guide": 20,
    "tulsa": 18, "the ballad of sexual dependency": 20, "immediate family": 18,
    "the last resort": 19, "ray's a laugh": 19, "raised by wolves": 19,
    "sleeping by the mississippi": 18, "observations": 16, "black book": 14,
    "falkland road": 17, "carnival strippers": 18, "saint-germain-des-pres": 19,
    "o rio de janeiro": 16, "bear pond": 15, "diary": 14, "sweet flypaper of life": 19,
    "gitans": 18, "11:02 nagasaki": 20, "anonyme skulpturen": 19, "new topographics": 20,
    "the new west": 17, "evidence": 20, "kodachrome": 20, "new industrial parks": 19,
    "in flagrante": 19, "a1 - the great north road": 20, "moments preserved": 16,
    "the end of the game": 16, "ravens": 19, "solitude of ravens": 19,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_page(offset: int) -> dict[str, Any]:
    params = {
        "N": monitor.CATEGORY_ID,
        "No": str(offset),
        "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)",
        "Nrpp": str(PAGE_SIZE),
        "Ns": "product.creationDate|1",
    }
    return monitor.request_json(monitor.SEARCH_URL, params).payload


def page_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    summary = monitor.find_results_summary(payload)
    skus, product_ids = monitor.ordered_skus(payload)
    meta = monitor.collect_metadata(payload, set(skus))
    items = [monitor.item_from_meta(sku, product_ids.get(sku), meta.get(sku, {})) for sku in skus]
    total = int(summary.get("totalMatchingRecords") or len(items))
    return items, total


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def score_item(item: dict[str, Any]) -> tuple[int, list[str]]:
    title = clean_text(item.get("title"))
    desc = clean_text(item.get("description"))
    hay = (title + " " + desc).lower()
    score = 0
    reasons: list[str] = []

    for pattern, pts in SIGNALS:
        if re.search(pattern, hay, flags=re.I):
            score += pts
            reasons.append(re.sub(r"\\b|\\s\+|\\", "", pattern)[:40])

    for name, pts in NAMES.items():
        if name.lower() in hay:
            score += pts
            reasons.append(name)

    for book_title, pts in TITLES.items():
        if book_title in hay:
            score += pts
            reasons.append(book_title)

    price = item.get("price_gbp")
    if isinstance(price, (int, float)):
        if price <= 5:
            score += 7
        elif price <= 10:
            score += 6
        elif price <= 20:
            score += 4
        elif price <= 35:
            score += 2
        elif price >= 250:
            score -= 4

    # Give a small boost to listings with unusually specific bibliographic clues.
    if re.search(r"\b(19[3-9]\d|200[0-9])\b", hay):
        score += 1
    if "dust jacket" in hay or "dustjacket" in hay or "dj" in hay:
        score += 2
    if "shrink wrap" in hay or "shrinkwrap" in hay:
        score += 2

    return score, list(dict.fromkeys(reasons))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalogue", default="data/full_catalogue.json")
    parser.add_argument("--candidates", default="data/full_candidates.json")
    parser.add_argument("--report", default="runtime/full_scan.md")
    args = parser.parse_args()

    first = fetch_page(0)
    first_items, total = page_items(first)
    all_items: list[dict[str, Any]] = list(first_items)
    pages = math.ceil(total / PAGE_SIZE)
    print(f"Oxfam reports {total} records; scanning {pages} pages")

    for page in range(1, pages):
        offset = page * PAGE_SIZE
        payload = fetch_page(offset)
        items, reported_total = page_items(payload)
        if reported_total != total:
            print(f"warning: total changed during scan {total} -> {reported_total}")
            total = max(total, reported_total)
        all_items.extend(items)
        print(f"page {page + 1}/{pages}: +{len(items)} ({len(all_items)} collected)")
        time.sleep(0.25)

    # Deduplicate by SKU while retaining catalogue order.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in all_items:
        sku = item.get("sku")
        if not isinstance(sku, str) or sku in seen:
            continue
        seen.add(sku)
        score, reasons = score_item(item)
        item["screen_score"] = score
        item["screen_reasons"] = reasons
        if item.get("route") and str(item["route"]).startswith("/"):
            item["url"] = monitor.BASE_URL + str(item["route"])
        unique.append(item)

    ranked = sorted(unique, key=lambda x: (int(x.get("screen_score") or 0), -(float(x.get("price_gbp") or 999999))), reverse=True)
    candidates = [x for x in ranked if int(x.get("screen_score") or 0) >= 7][:250]

    stamp = now_iso()
    catalogue_payload = {
        "scanned_at": stamp,
        "reported_total": total,
        "unique_skus": len(unique),
        "items": unique,
    }
    candidate_payload = {
        "scanned_at": stamp,
        "reported_total": total,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    for path, value in [(Path(args.catalogue), catalogue_payload), (Path(args.candidates), candidate_payload)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "## Oxfam full catalogue scan ready",
        "",
        f"Scanned at **{stamp}**.",
        f"Oxfam reported **{total}** active Art & Photography Books; **{len(unique)}** unique SKUs were captured.",
        f"Automated screening retained **{len(candidates)}** candidates for human/AI valuation research.",
        "",
        "The ranking is intentionally generous. It is a triage score, not a valuation. The strongest candidates should now be researched individually against exact editions and market comparables.",
        "",
        "### Top 80 screening candidates",
        "",
    ]
    for idx, item in enumerate(candidates[:80], 1):
        title = clean_text(item.get("title")) or "Untitled"
        price = item.get("price_gbp")
        price_text = f"£{price:.2f}" if isinstance(price, (int, float)) else "price unavailable"
        sku = item.get("sku")
        url = item.get("url") or ""
        reasons = ", ".join(item.get("screen_reasons") or [])
        desc = clean_text(item.get("description"))[:350]
        lines += [
            f"{idx}. **{title}** | {price_text} | score {item.get('screen_score')} | `{sku}`",
            f"   {url}",
            f"   Signals: {reasons or 'price/bibliographic clues'}",
            f"   {desc}",
            "",
        ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"captured {len(unique)} unique listings; {len(candidates)} screening candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
