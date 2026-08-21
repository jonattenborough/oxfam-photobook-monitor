#!/usr/bin/env python3
"""Deep one-off scan of Oxfam's whole Art & Photography parent category."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from oxfam_parent_common import (
    absolute_product_url,
    collect_metadata,
    discover_leaf_dimension_ids,
    discover_parent_dimension_id,
    fetch_search,
    item_from_meta,
    ordered_skus,
    searchable_text,
    total_matching_records,
    utc_now,
)

PAGE_SIZE = 90
OUT = Path("data/oxfam_parent_full_candidates.json")
RUNTIME = Path("runtime/parent_full_scan")

# High-priority photographer/title fingerprints.
TARGETS = [
    "robert frank", "les américains", "les americains", "the americans",
    "william eggleston", "guide", "democratic camera",
    "stephen shore", "uncommon places", "american surfaces",
    "larry clark", "tulsa", "teenage lust",
    "nan goldin", "ballad of sexual dependency",
    "sally mann", "immediate family",
    "martin parr", "last resort", "bad weather", "small world",
    "richard billingham", "ray's a laugh", "rays a laugh",
    "jim goldberg", "raised by wolves",
    "alec soth", "sleeping by the mississippi", "niagara",
    "richard avedon", "observations", "in the american west",
    "diane arbus", "aperture monograph",
    "robert mapplethorpe", "black book",
    "mary ellen mark", "falkland road",
    "susan meiselas", "carnival strippers",
    "ed van der elsken", "love on the left bank", "saint-germain",
    "bruce weber", "o rio", "bear pond",
    "corinne day", "diary",
    "walker evans", "american photographs",
    "roy decarava", "sweet flypaper",
    "josef koudelka", "gitans", "gypsies", "exiles",
    "new topographics",
    "larry sultan", "mike mandel", "evidence",
    "luigi ghirri", "kodachrome",
    "lewis baltz", "new industrial parks",
    "chris killip", "in flagrante",
    "paul graham", "a1 the great north road", "beyond caring",
    "irving penn", "moments preserved",
    "peter beard", "end of the game",
    "bruce gilden", "go",
    "daido moriyama", "farewell photography", "stray dog",
    "nobuyoshi araki", "sentimental journey",
    "anders petersen", "cafe lehmitz",
    "bill brandt", "perspective of nudes",
    "don mccullin", "homecoming",
    "henri cartier-bresson", "decisive moment", "images à la sauvette", "images a la sauvette",
    "robert adams", "new west", "summer nights",
    "joel meyerowitz", "cape light",
    "gary winogrand", "animals",
    "lee friedlander", "self portrait",
    "ralph gibson", "somnambulist",
    "helmut newton", "white women",
    "guy bourdin", "peter hujar", "francesca woodman", "vivian maier", "saul leiter",
    "masahisa fukase", "ravens",
    "shomei tomatsu", "11:02 nagasaki", "11.02 nagasaki",
    "hiroshi sugimoto", "rieko shiga", "takuma nakahira", "koji taki",
    "kikuji kawada", "the map", "chizu",
    "eikoh hosoe", "kamaitachi",
    "ikongraphy", "provoke",
    "tish murtha", "youth unemployment",
    "ken grant", "the close season",
    "nick waplington", "living room",
    "tom wood", "photie man",
    "mark power", "shipping forecast",
    "john davies", "chris steele-perkins", "café royal books", "cafe royal books",
]

PHOTO_TERMS = [
    "photograph", "photography", "photographer", "photographic", "photobook", "photo book",
    "photojournal", "camera", "darkroom", "contact sheet", "black-and-white photographs",
    "black and white photographs", "colour photographs", "color photographs",
    "portrait photography", "documentary photography", "street photography",
    "fashion photography", "landscape photography", "images by", "photographs by",
]

EDITION_TERMS = {
    "original print": 18, "with print": 15, "signed print": 18,
    "signed": 8, "inscribed": 9, "association copy": 15,
    "limited edition": 9, "numbered": 7, "edition of": 6,
    "first edition": 6, "1st edition": 6, "first printing": 8, "1st printing": 8,
    "artist proof": 12, "artist's proof": 12,
    "slipcase": 5, "slip case": 5, "glassine": 5, "acetate": 4,
    "portfolio": 6, "monograph": 3,
}

PUBLISHERS = [
    "aperture", "steidl", "mack", "twin palms", "nazraeli", "scalo", "delpire",
    "dewi lewis", "hatje cantz", "schirmer", "twelvetrees", "lustrum",
    "grey editions", "promenade", "punto e virgola", "castelli graphics",
    "museum of modern art", "moma", "new york graphic society",
    "powerhouse", "phaidon", "thames & hudson", "thames and hudson",
    "kehrer", "göttingen", "gottingen", "contrasto", "charta", "damiani",
    "loose joints", "skin nerboox", "skinnerboox", "void", "stanley/barker",
    "stanley barker", "gost", "morel", "super labo", "superlabo", "akio nagasawa",
    "shashin", "sokyusha", "rat hole", "rat hole gallery", "little brown mushroom",
    "gnomic", "bluecoat press", "café royal books", "cafe royal books",
]

# Generic art words that are useful only in combination, not enough on their own.
SOFT_PHOTO = ["plates", "images", "illustrated", "monograph", "portraits", "archive"]


def score(item: dict[str, Any]) -> tuple[int, list[str]]:
    text = searchable_text(item)
    points = 0
    reasons: list[str] = []

    targets = sorted({t for t in TARGETS if t in text})
    if targets:
        points += min(40, 20 + 5 * (len(targets) - 1))
        reasons.append("target: " + ", ".join(targets[:5]))

    photos = sorted({t for t in PHOTO_TERMS if t in text})
    if photos:
        points += min(16, 7 + 2 * (len(photos) - 1))
        reasons.append("photography signal: " + ", ".join(photos[:4]))

    pubs = sorted({p for p in PUBLISHERS if p in text})
    if pubs:
        points += min(12, 6 + 2 * (len(pubs) - 1))
        reasons.append("publisher: " + ", ".join(pubs[:3]))

    edition_hits = []
    for phrase, weight in EDITION_TERMS.items():
        if phrase in text:
            points += weight
            edition_hits.append(phrase)
    if edition_hits:
        reasons.append("edition clues: " + ", ".join(edition_hits[:6]))

    soft = sorted({t for t in SOFT_PHOTO if t in text})
    if soft and (photos or pubs or edition_hits):
        points += min(5, len(soft) + 1)

    price = item.get("price_gbp")
    if isinstance(price, (int, float)):
        if price <= 10:
            points += 8
            reasons.append("£10 or less")
        elif price <= 20:
            points += 6
            reasons.append("£20 or less")
        elif price <= 50:
            points += 4
            reasons.append("£50 or less")
        elif price <= 100:
            points += 2
        elif price >= 300:
            points -= 2

    # A very cheap title with strong book-edition clues deserves inspection even when Oxfam
    # never says "photography".
    if isinstance(price, (int, float)) and price <= 30 and len(edition_hits) >= 2:
        points += 4

    return points, reasons


def add_payload_items(payload: dict[str, Any], items: dict[str, dict[str, Any]]) -> tuple[int, list[str]]:
    skus, product_ids = ordered_skus(payload)
    metadata = collect_metadata(payload, set(skus))
    added = 0
    for sku in skus:
        if sku in items:
            continue
        items[sku] = item_from_meta(sku, product_ids.get(sku), metadata.get(sku, {}))
        added += 1
    return added, skus


def crawl_dimension(
    dimension_id: str,
    label: str,
    items: dict[str, dict[str, Any]],
    allow_backend_cap: bool = False,
    sort_key: str = "product.creationDate|1",
) -> None:
    offset = 0
    page = 0
    local_seen: set[str] = set()
    while True:
        payload = fetch_search(dimension_id, offset, PAGE_SIZE, sort_key=sort_key)
        added, skus = add_payload_items(payload, items)
        if not skus:
            break
        new_local = [sku for sku in skus if sku not in local_seen]
        local_seen.update(skus)
        page += 1
        print(f"{label} page {page}: {len(skus)} SKUs, {added} new, total unique {len(items)}")
        if len(skus) < PAGE_SIZE:
            break
        if not new_local:
            if allow_backend_cap:
                print(f"{label}: Oracle pagination cap reached after {len(local_seen)} local SKUs")
                break
            raise RuntimeError(f"Search pagination repeated a page in {label}")
        offset += PAGE_SIZE
        if page > 400:
            raise RuntimeError(f"Safety stop after 400 pages in {label}")


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    dimension_id, repository_id = discover_parent_dimension_id()
    print(f"Resolved parent Art & Photography dimension: {dimension_id} (repo={repository_id})")

    items: dict[str, dict[str, Any]] = {}
    parent_first = fetch_search(dimension_id, 0, PAGE_SIZE)
    expected_total = total_matching_records(parent_first)
    add_payload_items(parent_first, items)

    leaf_dimensions = discover_leaf_dimension_ids(repository_id)
    print(f"Resolved {len(leaf_dimensions)} leaf category dimensions")
    for index, leaf_dimension in enumerate(leaf_dimensions, 1):
        crawl_dimension(leaf_dimension, f"leaf {index}/{len(leaf_dimensions)}", items)

    # Include products assigned directly to the parent. Oracle repeats pages at
    # roughly 10,000 results, so this pass is allowed to stop at that ceiling.
    if not leaf_dimensions or (expected_total is not None and len(items) < expected_total):
        crawl_dimension(dimension_id, "parent newest fallback", items, allow_backend_cap=True)

    # The parent contains some listings not assigned to any leaf category. A
    # reverse chronological pass reaches the opposite side of Oracle's result
    # ceiling and fills those direct-parent gaps without relying on price syntax.
    if expected_total is not None and len(items) < expected_total:
        crawl_dimension(
            dimension_id,
            "parent oldest fallback",
            items,
            allow_backend_cap=True,
            sort_key="product.creationDate|0",
        )

    if expected_total is not None and len(items) < expected_total:
        raise RuntimeError(
            f"Segmented crawl found {len(items)} of {expected_total} parent-category products"
        )

    ranked = []
    for item in items.values():
        points, reasons = score(item)
        # Deliberately permissive: we want a few thousand plausible records rather than
        # accidentally throwing away an obscure miscatalogued photobook.
        if points >= 6:
            ranked.append({
                **item,
                "score": points,
                "score_reasons": reasons,
                "url": absolute_product_url(item),
            })

    ranked.sort(key=lambda x: (
        -x["score"],
        x.get("price_gbp") if isinstance(x.get("price_gbp"), (int, float)) else 999999,
        str(x.get("title") or "").lower(),
    ))

    snapshot = {
        "generated_at": utc_now(),
        "source_route": "/art-and-photography/category/art-photography",
        "resolved_dimension_id": dimension_id,
        "resolved_repository_id": repository_id,
        "expected_parent_products": expected_total,
        "leaf_dimensions_scanned": len(leaf_dimensions),
        "unique_products_scanned": len(items),
        "candidate_count": len(ranked),
        "candidates": ranked[:2000],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    # Keep the GitHub issue focused enough for immediate manual review while the
    # much broader ranked candidate set remains available in the saved JSON file.
    top = ranked[:50]
    issue_lines = [
        "## Oxfam full Art & Photography parent-category scan",
        "",
        f"Generated **{snapshot['generated_at']}**.",
        f"Scanned **{len(items)}** unique live products in the parent Art & Photography category.",
        f"Broad first-pass filter retained **{len(ranked)}** candidates; top 2,000 are stored in `data/oxfam_parent_full_candidates.json`.",
        "",
        "This scan is deliberately broader than Oxfam's Photography subsection to catch miscategorised photobooks.",
        "",
        "### Highest-ranked candidates",
        "",
    ]
    for i, item in enumerate(top, 1):
        title = item.get("title") or item["sku"]
        price = f"£{item['price_gbp']:.2f}" if isinstance(item.get("price_gbp"), (int, float)) else "price n/a"
        issue_lines += [
            f"{i}. **{title}** - {price} - score {item['score']}",
            f"   - SKU `{item['sku']}`",
            f"   - {item['url']}",
            f"   - Reasons: {'; '.join(item['score_reasons'])}",
        ]
        if item.get("author"):
            issue_lines.append(f"   - Author/photographer: {item['author']}")
        if item.get("publisher"):
            issue_lines.append(f"   - Publisher: {item['publisher']}")
        if item.get("description"):
            desc = re.sub(r"\s+", " ", str(item["description"])).strip()
            issue_lines.append(f"   - Description: {desc[:500]}")
        issue_lines.append("")

    (RUNTIME / "issue-title.txt").write_text(
        f"OXFAM_ART_SCAN: full parent-category scan {snapshot['generated_at'][:10]}\n",
        encoding="utf-8",
    )
    (RUNTIME / "issue-body.md").write_text("\n".join(issue_lines), encoding="utf-8")

    print(f"Finished: {len(items)} products, {len(ranked)} broad candidates")
    for item in ranked[:30]:
        print(item["score"], item.get("price_gbp"), item.get("title"), item["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
