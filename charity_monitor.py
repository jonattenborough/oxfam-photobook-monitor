#!/usr/bin/env python3
"""Monitor Shelter and Crisis charity shops for newly listed collectible books."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAGE_SIZE = 250

HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

# We deliberately watch more than just photography-labelled stock. A valuable
# photobook can be miscategorised as ordinary art/non-fiction, and edition clues
# such as signed/limited/first edition are useful signals in those broader lists.
COLLECTIONS = [
    {
        "store": "shelter",
        "store_name": "Shelter",
        "base_url": "https://shop.shelter.org.uk",
        "handle": "art-photography-books",
        "collection_name": "Art & Photography",
        "alert_all": True,
    },
    {
        "store": "shelter",
        "store_name": "Shelter",
        "base_url": "https://shop.shelter.org.uk",
        "handle": "antiquarian-rare-collectable-books",
        "collection_name": "Rare & Collectable",
        "alert_all": False,
    },
    {
        "store": "shelter",
        "store_name": "Shelter",
        "base_url": "https://shop.shelter.org.uk",
        "handle": "secondhand-books",
        "collection_name": "Second Hand Books",
        "alert_all": False,
    },
    {
        "store": "crisis",
        "store_name": "Crisis",
        "base_url": "https://shopfromcrisis.org.uk",
        "handle": "books",
        "collection_name": "Books",
        "alert_all": True,
    },
]

# Broad enough to catch canonical names/titles and edition/rarity clues outside
# the dedicated photography collection. The AI research stage does the final
# judgement, so false positives are preferable to missing a major bargain.
RADAR_TERMS = [
    # Generic photography / collectible signals
    "photograph", "photography", "photobook", "photo book", "camera",
    "signed", "inscribed", "signature", "first edition", "1st edition",
    "first printing", "1st printing", "limited edition", "numbered",
    "edition of", "artist proof", "artist's proof", "slipcase", "slip case",
    "glassine", "acetate", "original print", "with print", "portfolio",
    "rare", "collectable", "collectible", "monograph",
    # Canonical photographers / recurring targets
    "robert frank", "the americans", "william eggleston", "stephen shore",
    "uncommon places", "larry clark", "tulsa", "nan goldin", "ballad of sexual dependency",
    "sally mann", "immediate family", "martin parr", "last resort",
    "richard billingham", "ray's a laugh", "jim goldberg", "raised by wolves",
    "alec soth", "sleeping by the mississippi", "richard avedon", "observations",
    "robert mapplethorpe", "black book", "mary ellen mark", "falkland road",
    "susan meiselas", "carnival strippers", "ed van der elsken", "saint-germain",
    "bruce weber", "o rio", "bear pond", "corinne day", "diary",
    "walker evans", "american photographs", "roy decarava", "sweet flypaper",
    "josef koudelka", "gitans", "new topographics", "nicholas nixon",
    "mike mandel", "larry sultan", "evidence", "luigi ghirri", "kodachrome",
    "lewis baltz", "diane arbus", "chris killip", "in flagrante",
    "paul graham", "a1", "irving penn", "moments preserved", "peter beard",
    "end of the game", "bruce gilden", "daido moriyama", "nobuyoshi araki",
    "anders petersen", "cafe lehmitz", "bill brandt", "don mccullin",
    "henri cartier-bresson", "robert adams", "joel meyerowitz", "gary winogrand",
    "lee friedlander", "ralph gibson", "helmut newton", "guy bourdin",
    "peter hujar", "francesca woodman", "vivian maier", "saul leiter",
    "masahisa fukase", "ravens", "shomei tomatsu", "tomatsu",
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(url: str, retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                if getattr(response, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {response.status} from {url}")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Expected JSON object from {url}")
                return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_error}")


def collection_products(base_url: str, handle: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 1
    while True:
        params = urllib.parse.urlencode({"limit": PAGE_SIZE, "page": page})
        url = f"{base_url}/collections/{handle}/products.json?{params}"
        payload = request_json(url)
        batch = payload.get("products")
        if not isinstance(batch, list):
            raise RuntimeError(f"Shopify response for {handle} has no products list")
        if not batch:
            break
        new_on_page = 0
        for product in batch:
            if not isinstance(product, dict) or product.get("id") is None:
                continue
            pid = str(product["id"])
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            products.append(product)
            new_on_page += 1
        if len(batch) < PAGE_SIZE:
            break
        if new_on_page == 0:
            raise RuntimeError(f"Pagination for {handle} repeated a page; refusing an infinite loop")
        page += 1
        if page > 50:
            raise RuntimeError(f"Unexpectedly large Shopify collection {handle}; stopped after 50 pages")
    return products


def strip_html(value: Any) -> str:
    text = "" if value is None else str(value)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def product_available(product: dict[str, Any]) -> bool:
    variants = product.get("variants")
    if not isinstance(variants, list):
        return False
    return any(isinstance(v, dict) and bool(v.get("available")) for v in variants)


def product_price(product: dict[str, Any]) -> float | None:
    values: list[float] = []
    variants = product.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            try:
                values.append(float(variant.get("price")))
            except (TypeError, ValueError):
                pass
    return round(min(values), 2) if values else None


def searchable_text(product: dict[str, Any]) -> str:
    tags = product.get("tags")
    if isinstance(tags, list):
        tags_text = " ".join(str(x) for x in tags)
    else:
        tags_text = str(tags or "")
    return " ".join([
        str(product.get("title") or ""),
        strip_html(product.get("body_html")),
        str(product.get("vendor") or ""),
        str(product.get("product_type") or ""),
        tags_text,
    ]).lower()


def radar_match(product: dict[str, Any]) -> list[str]:
    text = searchable_text(product)
    return sorted({term for term in RADAR_TERMS if term in text})


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "products": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Charity monitor state is not a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("products", {})
    return payload


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def set_output(name: str, value: str) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def product_url(base_url: str, product: dict[str, Any]) -> str:
    handle = str(product.get("handle") or "").strip()
    return f"{base_url}/products/{handle}" if handle else base_url


def compact_product(store: str, store_name: str, base_url: str, product: dict[str, Any], memberships: list[str], matches: list[str]) -> dict[str, Any]:
    tags = product.get("tags")
    if isinstance(tags, list):
        clean_tags = [str(x) for x in tags][:30]
    elif tags:
        clean_tags = [str(tags)]
    else:
        clean_tags = []
    return {
        "key": f"{store}:{product['id']}",
        "store": store,
        "store_name": store_name,
        "id": str(product["id"]),
        "title": str(product.get("title") or "Untitled product"),
        "url": product_url(base_url, product),
        "price_gbp": product_price(product),
        "available": product_available(product),
        "published_at": product.get("published_at"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "tags": clean_tags,
        "description": strip_html(product.get("body_html"))[:1800],
        "collections": sorted(memberships),
        "radar_matches": matches,
    }


def make_issue(items: list[dict[str, Any]], detected_at: str) -> tuple[str, str]:
    stores = sorted({item["store_name"] for item in items})
    title = f"CHARITY_NEW: {' + '.join(stores)} book listings {detected_at[:16].replace('T', ' ')}Z"
    lines = [
        "## New charity-shop book listings",
        "",
        f"Detected at **{detected_at}** by the Shelter/Crisis Shopify monitor.",
        "These need collection/value analysis before any email alert is sent.",
        "",
    ]
    for item in items:
        lines += [
            f"### {item['title']}",
            "",
            f"- **Charity:** {item['store_name']}",
            f"- **Product:** {item['url']}",
        ]
        if item.get("price_gbp") is not None:
            lines.append(f"- **Price:** £{item['price_gbp']:.2f}")
        lines.append(f"- **Collections:** {', '.join(item['collections'])}")
        if item.get("product_type"):
            lines.append(f"- **Product type:** {item['product_type']}")
        if item.get("vendor"):
            lines.append(f"- **Vendor:** {item['vendor']}")
        if item.get("published_at"):
            lines.append(f"- **Published online:** {item['published_at']}")
        if item.get("radar_matches"):
            lines.append(f"- **Radar matches:** {', '.join(item['radar_matches'])}")
        if item.get("tags"):
            lines.append(f"- **Tags:** {', '.join(item['tags'][:20])}")
        if item.get("description"):
            lines.append(f"- **Description:** {item['description']}")
        lines.append("")
    return title[:240], "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/charity_state.json")
    parser.add_argument("--runtime-dir", default="runtime/charity")
    args = parser.parse_args()

    state_path = Path(args.state)
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    old_products: dict[str, Any] = state["products"]
    first_run = not bool(old_products)

    aggregated: dict[str, dict[str, Any]] = {}
    collection_counts: dict[str, int] = {}

    for source in COLLECTIONS:
        products = collection_products(source["base_url"], source["handle"])
        collection_key = f"{source['store']}:{source['handle']}"
        collection_counts[collection_key] = len(products)
        print(f"{source['store_name']} / {source['collection_name']}: {len(products)} products")
        for product in products:
            pid = str(product.get("id"))
            key = f"{source['store']}:{pid}"
            entry = aggregated.setdefault(key, {
                "store": source["store"],
                "store_name": source["store_name"],
                "base_url": source["base_url"],
                "product": product,
                "memberships": [],
                "alert_all": False,
            })
            if source["collection_name"] not in entry["memberships"]:
                entry["memberships"].append(source["collection_name"])
            entry["alert_all"] = bool(entry["alert_all"] or source["alert_all"])

    detected_at = utc_now()
    proposed_products: dict[str, Any] = dict(old_products)
    alert_items: list[dict[str, Any]] = []
    newly_seen_count = 0

    for key, entry in aggregated.items():
        product = entry["product"]
        matches = radar_match(product)
        compact = compact_product(
            entry["store"], entry["store_name"], entry["base_url"],
            product, entry["memberships"], matches,
        )
        is_new = key not in old_products
        if is_new:
            newly_seen_count += 1
        proposed_products[key] = {
            "first_seen": old_products.get(key, {}).get("first_seen", detected_at),
            "last_seen": detected_at,
            "title": compact["title"],
            "url": compact["url"],
            "price_gbp": compact["price_gbp"],
            "available": compact["available"],
            "collections": compact["collections"],
        }
        if is_new and not first_run and compact["available"] and (entry["alert_all"] or matches):
            alert_items.append(compact)

    proposed = {
        "version": 1,
        "last_checked": detected_at,
        "collection_counts": collection_counts,
        "products": proposed_products,
    }
    write_json(runtime / "proposed-state.json", proposed)
    write_json(runtime / "latest-snapshot.json", {
        "checked_at": detected_at,
        "collection_counts": collection_counts,
        "unique_products": len(aggregated),
        "newly_seen": newly_seen_count,
        "alert_candidates": alert_items,
    })

    if alert_items:
        title, body = make_issue(alert_items, detected_at)
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(body, encoding="utf-8")

    print(f"Unique current products across monitored collections: {len(aggregated)}")
    print(f"Newly seen products: {newly_seen_count}")
    if first_run:
        print("Initial baseline run: current products seeded silently; no false new-listing alert created.")
    print(f"Alert candidates: {len(alert_items)}")

    set_output("new_count", str(len(alert_items)))
    set_output("state_changed", "true")
    set_output("first_run", "true" if first_run else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
