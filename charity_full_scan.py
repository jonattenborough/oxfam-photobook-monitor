#!/usr/bin/env python3
"""Rank the full existing Shelter and Crisis inventories for collectible photobook leads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from charity_monitor import (
    COLLECTIONS,
    collection_products,
    compact_product,
    product_available,
    radar_match,
    searchable_text,
    utc_now,
)

OUT = Path("data/charity_full_candidates.json")

TARGET_PHRASES = [
    "robert frank", "the americans", "william eggleston", "stephen shore", "uncommon places",
    "larry clark", "tulsa", "nan goldin", "ballad of sexual dependency", "sally mann",
    "immediate family", "martin parr", "last resort", "richard billingham", "ray's a laugh",
    "jim goldberg", "raised by wolves", "alec soth", "sleeping by the mississippi",
    "richard avedon", "observations", "robert mapplethorpe", "mary ellen mark", "falkland road",
    "susan meiselas", "carnival strippers", "ed van der elsken", "bruce weber", "corinne day",
    "walker evans", "american photographs", "roy decarava", "sweet flypaper", "josef koudelka",
    "gitans", "new topographics", "larry sultan", "mike mandel", "evidence", "luigi ghirri",
    "kodachrome", "lewis baltz", "diane arbus", "chris killip", "in flagrante", "paul graham",
    "irving penn", "moments preserved", "peter beard", "end of the game", "bruce gilden",
    "daido moriyama", "nobuyoshi araki", "anders petersen", "cafe lehmitz", "bill brandt",
    "don mccullin", "henri cartier-bresson", "robert adams", "joel meyerowitz", "gary winogrand",
    "lee friedlander", "ralph gibson", "helmut newton", "guy bourdin", "peter hujar",
    "francesca woodman", "vivian maier", "saul leiter", "masahisa fukase", "ravens",
    "shomei tomatsu", "tomatsu", "al as dair mclellan", "alasdair mclellan", "william klein",
]

SIGNALS = {
    "original print": 14,
    "with print": 12,
    "signed": 9,
    "inscribed": 9,
    "association copy": 12,
    "artist's proof": 12,
    "artist proof": 12,
    "limited edition": 8,
    "numbered": 6,
    "edition of": 6,
    "first edition": 6,
    "1st edition": 6,
    "first printing": 8,
    "1st printing": 8,
    "slipcase": 4,
    "slip case": 4,
    "glassine": 4,
    "acetate": 4,
    "portfolio": 5,
    "monograph": 4,
    "rare": 3,
    "collectable": 3,
    "collectible": 3,
}

PHOTO_HINTS = ["photograph", "photography", "photobook", "photo book", "photographer", "camera"]


def score(entry: dict[str, Any]) -> tuple[int, list[str]]:
    product = entry["product"]
    text = searchable_text(product)
    memberships = set(entry["memberships"])
    price = entry["compact"].get("price_gbp")
    points = 0
    reasons: list[str] = []

    if "Art & Photography" in memberships:
        points += 8
        reasons.append("Art & Photography")
    if "Rare & Collectable" in memberships:
        points += 5
        reasons.append("Rare & Collectable")

    matched_targets = [p for p in TARGET_PHRASES if p in text]
    if matched_targets:
        boost = min(24, 12 + 4 * (len(matched_targets) - 1))
        points += boost
        reasons.append("target: " + ", ".join(matched_targets[:3]))

    matched_signals = []
    for phrase, weight in SIGNALS.items():
        if phrase in text:
            points += weight
            matched_signals.append(phrase)
    if matched_signals:
        reasons.append("edition clues: " + ", ".join(matched_signals[:5]))

    if any(h in text for h in PHOTO_HINTS) and "Art & Photography" not in memberships:
        points += 5
        reasons.append("photography signal")

    if isinstance(price, (int, float)):
        if price <= 10:
            points += 7
            reasons.append("£10 or less")
        elif price <= 20:
            points += 5
            reasons.append("£20 or less")
        elif price <= 50:
            points += 3
            reasons.append("£50 or less")
        elif price <= 100:
            points += 1
            reasons.append("£100 or less")
        elif price >= 250:
            points -= 2

    return points, reasons


def main() -> int:
    aggregated: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}

    for source in COLLECTIONS:
        products = collection_products(source["base_url"], source["handle"])
        counts[f"{source['store']}:{source['handle']}"] = len(products)
        for product in products:
            if not product_available(product):
                continue
            key = f"{source['store']}:{product['id']}"
            entry = aggregated.setdefault(key, {
                "store": source["store"],
                "store_name": source["store_name"],
                "base_url": source["base_url"],
                "product": product,
                "memberships": [],
            })
            if source["collection_name"] not in entry["memberships"]:
                entry["memberships"].append(source["collection_name"])

    ranked: list[dict[str, Any]] = []
    for key, entry in aggregated.items():
        product = entry["product"]
        matches = radar_match(product)
        compact = compact_product(
            entry["store"], entry["store_name"], entry["base_url"], product,
            entry["memberships"], matches,
        )
        entry["compact"] = compact
        points, reasons = score(entry)
        # Keep every photography-list item plus any broader-list item with meaningful signals.
        if points < 5 and "Art & Photography" not in entry["memberships"]:
            continue
        ranked.append({
            **compact,
            "score": points,
            "score_reasons": reasons,
        })

    ranked.sort(key=lambda x: (-x["score"], x.get("price_gbp") if x.get("price_gbp") is not None else 999999, x["title"].lower()))
    snapshot = {
        "generated_at": utc_now(),
        "collection_counts": counts,
        "unique_available_products_scanned": len(aggregated),
        "candidate_count": len(ranked),
        "candidates": ranked[:300],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Scanned {len(aggregated)} unique available products")
    print(f"Ranked {len(ranked)} candidates; saved top {min(300, len(ranked))}")
    print("Top 30:")
    for item in ranked[:30]:
        price = f"£{item['price_gbp']:.2f}" if item.get("price_gbp") is not None else "price n/a"
        print(f"{item['score']:>3} | {price:>10} | {item['store_name']:<7} | {item['title']} | {item['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
