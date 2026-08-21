#!/usr/bin/env python3
"""Monitor Oxfam's Art & Photography category for newly listed SKUs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_URL = "https://onlineshop.oxfam.org.uk"
CATEGORY_URL = (
    BASE_URL
    + "/art-photography-books/category/art-photography-books?"
    + "N=2776812252&Ns=product.creationDate%7C1"
)
SEARCH_URL = BASE_URL + "/ccstore/v1/search"
CATEGORY_ID = "2776812252"
CATALOG_ID = "Oxfam_GB"
PAGE_SIZE = 30

HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": CATEGORY_URL,
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

SKU_RE = re.compile(r"HD_{1,2}(\d+)")


@dataclass
class FetchResult:
    payload: dict[str, Any]
    status: int
    url: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sku(value: Any) -> str | None:
    if isinstance(value, list):
        for part in value:
            sku = canonical_sku(part)
            if sku:
                return sku
        return None
    if not isinstance(value, str):
        return None
    m = SKU_RE.search(value)
    return f"HD_{m.group(1)}" if m else None


def request_json(url: str, params: dict[str, str] | None = None, retries: int = 3) -> FetchResult:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                if status != 200:
                    raise RuntimeError(f"HTTP {status} from {url}")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    preview = raw[:300].decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Expected JSON from Oxfam but got {content_type!r}: {preview!r}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise RuntimeError("Oxfam response was JSON but not an object")
                return FetchResult(payload=payload, status=status, url=url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch Oxfam after {retries} attempts: {last_error}")


def fetch_search() -> FetchResult:
    params = {
        "N": CATEGORY_ID,
        "No": "0",
        "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)",
        "Nrpp": str(PAGE_SIZE),
        "Ns": "product.creationDate|1",
    }
    return request_json(SEARCH_URL, params)


def find_results_summary(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("searchEventSummary")
    if not isinstance(event, dict):
        raise RuntimeError("Response is missing searchEventSummary")
    summaries = event.get("resultsSummary")
    if not isinstance(summaries, list):
        raise RuntimeError("Response is missing resultsSummary")

    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        sort = summary.get("sort")
        if not isinstance(sort, dict):
            continue
        keys = sort.get("sortKeys")
        if not isinstance(keys, list):
            continue
        if any(
            isinstance(key, dict)
            and key.get("attribute") == "product.creationDate"
            and key.get("direction") == "desc"
            for key in keys
        ):
            records = summary.get("records")
            if isinstance(records, list):
                return summary
    raise RuntimeError("Could not verify newest-first product.creationDate ordering")


def ordered_skus(payload: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    summary = find_results_summary(payload)
    records = summary.get("records", [])
    ordered: list[str] = []
    product_ids: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        sku = canonical_sku(record.get("sku.listingId")) or canonical_sku(record.get("record.id"))
        if not sku or sku in ordered:
            continue
        ordered.append(sku)
        record_id = record.get("record.id")
        if isinstance(record_id, str):
            marker = "/sku-"
            if marker in record_id:
                tail = record_id.split(marker, 1)[1]
                product_id = tail.split("..", 1)[0]
                if product_id:
                    product_ids[sku] = product_id
    if not ordered:
        raise RuntimeError("Oxfam response contained no listing SKUs")
    return ordered, product_ids


def scalarize(value: Any) -> Any:
    if isinstance(value, list):
        if len(value) == 1:
            return scalarize(value[0])
        return [scalarize(v) for v in value if isinstance(v, (str, int, float, bool, type(None)))]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def collect_metadata(payload: dict[str, Any], allowed_skus: set[str]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {sku: {} for sku in allowed_skus}
    interesting_prefixes = ("sku.", "product.")
    interesting_names = {
        "displayName", "title", "name", "description", "longDescription",
        "listPrice", "activePrice", "salePrice", "creationDate", "url", "route",
        "isbn", "ISBN", "publisher", "publishedDate", "author", "condition",
    }

    for node in iter_dicts(payload):
        attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
        candidates = [
            node.get("sku.listingId"), node.get("listingId"), node.get("record.id"), node.get("id"),
            attrs.get("sku.listingId"), attrs.get("record.id"), attrs.get("id"),
        ]
        sku = next((canonical_sku(v) for v in candidates if canonical_sku(v)), None)
        if not sku or sku not in allowed_skus:
            continue
        dest = metadata[sku]
        merged = {**attrs, **node}
        for key, value in merged.items():
            if key in {"attributes", "records", "detailsAction", "pagingActionTemplate"}:
                continue
            if key in interesting_names or key.startswith(interesting_prefixes):
                simple = scalarize(value)
                if simple not in (None, "", []):
                    dest.setdefault(key, simple)
    return metadata


def first_value(meta: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if value not in (None, ""):
            return value
    return None


def normalize_price(value: Any) -> float | None:
    if isinstance(value, list) and value:
        value = value[0]
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def item_from_meta(sku: str, product_id: str | None, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": sku,
        "product_id": product_id,
        "title": first_value(meta, ["product.displayName", "displayName", "product.title", "title", "product.name", "name"]),
        "price_gbp": normalize_price(first_value(meta, ["sku.activePrice", "activePrice", "sku.listPrice", "listPrice", "sku.minActivePrice"])),
        "description": first_value(meta, ["product.longDescription", "longDescription", "product.description", "description"]),
        "condition": first_value(meta, ["product.condition", "condition", "sku.condition"]),
        "isbn": first_value(meta, ["product.isbn", "isbn", "ISBN", "sku.isbn"]),
        "publisher": first_value(meta, ["product.publisher", "publisher"]),
        "creation_date": first_value(meta, ["product.creationDate", "creationDate"]),
        "route": first_value(meta, ["product.route", "route", "product.url", "url"]),
    }


def fetch_product_detail(product_id: str) -> tuple[dict[str, Any] | None, str | None]:
    url = BASE_URL + "/ccstore/v1/products/" + urllib.parse.quote(product_id, safe="")
    params = {
        "catalogId": CATALOG_ID,
        "includeChildSKUsListingIds": "true",
        "withPrices": "true",
    }
    try:
        result = request_json(url, params, retries=2)
        return result.payload, None
    except Exception as exc:
        return None, str(exc)


def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    product_id = item.get("product_id")
    if not isinstance(product_id, str) or not product_id:
        item["detail_fetch_error"] = "No product id could be derived from search response"
        return item
    payload, error = fetch_product_detail(product_id)
    if error:
        item["detail_fetch_error"] = error
        return item
    assert payload is not None
    detail_meta = collect_metadata(payload, {item["sku"]}).get(item["sku"], {})
    root = payload if isinstance(payload, dict) else {}
    for key in [
        "displayName", "title", "description", "longDescription", "listPrice", "salePrice",
        "creationDate", "route", "url", "isbn", "ISBN", "publisher", "condition",
    ]:
        if key in root and key not in detail_meta:
            detail_meta[key] = root[key]
    enriched = item_from_meta(item["sku"], product_id, {**detail_meta})
    for key, value in enriched.items():
        if value not in (None, "", []):
            item[key] = value
    compact = {}
    for key in [
        "displayName", "description", "longDescription", "creationDate", "id", "route",
        "listPrice", "salePrice", "brand", "type", "parentCategories", "childSKUs",
    ]:
        if key in root:
            compact[key] = root[key]
    if compact:
        item["detail_snapshot"] = compact
    return item


def search_fingerprint(item: dict[str, Any]) -> str:
    stable = {
        "title": item.get("title"),
        "price_gbp": item.get("price_gbp"),
        "condition": item.get("condition"),
        "isbn": item.get("isbn"),
        "publisher": item.get("publisher"),
    }
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "products": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("State file is not a JSON object")
    data.setdefault("version", 1)
    data.setdefault("products", {})
    return data


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def make_issue_body(items: list[dict[str, Any]], detected_at: str, category_count: int | None) -> str:
    lines = [
        "## New Oxfam Art & Photography listings",
        "",
        f"Detected at **{detected_at}** by the Oxfam catalogue monitor.",
        f"Category: {CATEGORY_URL}",
    ]
    if category_count is not None:
        lines.append(f"Oxfam reported **{category_count}** matching category records.")
    lines += ["", "These listings need ChatGPT collection/value analysis before any email alert is sent.", ""]

    for item in items:
        title = item.get("title") or "Title not exposed by search response"
        lines += [f"### {title}", "", f"- **SKU:** `{item['sku']}`"]
        if item.get("price_gbp") is not None:
            lines.append(f"- **Oxfam price:** £{item['price_gbp']:.2f}")
        if item.get("product_id"):
            lines.append(f"- **Oracle product id:** `{item['product_id']}`")
        if item.get("isbn"):
            lines.append(f"- **ISBN:** {item['isbn']}")
        if item.get("publisher"):
            lines.append(f"- **Publisher:** {item['publisher']}")
        if item.get("condition"):
            lines.append(f"- **Condition:** {item['condition']}")
        if item.get("creation_date"):
            lines.append(f"- **Creation date:** {item['creation_date']}")
        if item.get("route"):
            route = str(item["route"])
            if route.startswith("/"):
                route = BASE_URL + route
            lines.append(f"- **Possible product URL:** {route}")
        if item.get("description"):
            text = re.sub(r"\s+", " ", str(item["description"])).strip()
            lines.append(f"- **Description:** {text[:1200]}")
        if item.get("detail_fetch_error"):
            lines.append(f"- **Product-detail API:** unavailable on this run ({item['detail_fetch_error']})")
        lines += ["", f"Search fallback: `{item['sku']}` on Oxfam / the wider web.", ""]
    return "\n".join(lines).rstrip() + "\n"


def set_github_output(name: str, value: str) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--runtime-dir", default="runtime")
    args = parser.parse_args()

    state_path = Path(args.state)
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)

    state = load_state(state_path)
    products_state: dict[str, Any] = state["products"]

    fetched = fetch_search()
    payload = fetched.payload
    ordered, product_ids = ordered_skus(payload)
    meta = collect_metadata(payload, set(ordered))
    items = [item_from_meta(sku, product_ids.get(sku), meta.get(sku, {})) for sku in ordered]

    event = payload.get("searchEventSummary", {})
    category_count = None
    if isinstance(event, dict):
        for summary in event.get("resultsSummary", []) if isinstance(event.get("resultsSummary"), list) else []:
            if isinstance(summary, dict) and isinstance(summary.get("totalMatchingRecords"), int):
                category_count = summary["totalMatchingRecords"]
                break

    new_items: list[dict[str, Any]] = []
    state_changed = False
    detected_at = utc_now()

    for item in items:
        sku = item["sku"]
        fp = search_fingerprint(item)
        previous = products_state.get(sku)
        if previous is None:
            enriched = enrich_item(dict(item))
            new_items.append(enriched)
            products_state[sku] = {
                "first_seen": detected_at,
                "search_fingerprint": fp,
                "last_snapshot": item,
            }
            state_changed = True
            continue

        previous_fp = previous.get("search_fingerprint") if isinstance(previous, dict) else None
        if not previous_fp:
            if not isinstance(previous, dict):
                previous = {"first_seen": "baseline"}
                products_state[sku] = previous
            previous["search_fingerprint"] = fp
            previous["last_snapshot"] = item
            state_changed = True
            continue

        if previous_fp != fp:
            previous["search_fingerprint"] = fp
            previous["last_snapshot"] = item
            previous["last_changed"] = detected_at
            state_changed = True

    proposed_state = dict(state)
    proposed_state["products"] = products_state
    proposed_state["last_successful_fetch"] = detected_at
    proposed_state["last_top_skus"] = ordered
    proposed_state["reported_category_count"] = category_count
    write_json(runtime / "proposed-state.json", proposed_state)

    if new_items:
        write_json(runtime / "new-items.json", new_items)
        title_skus = ", ".join(item["sku"] for item in new_items[:4])
        if len(new_items) > 4:
            title_skus += f" +{len(new_items) - 4}"
        issue_title = f"OXFAM_NEW: {len(new_items)} listing{'s' if len(new_items) != 1 else ''} | {title_skus}"
        (runtime / "issue-title.txt").write_text(issue_title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            make_issue_body(new_items, detected_at, category_count), encoding="utf-8"
        )

    set_github_output("new_count", str(len(new_items)))
    set_github_output("state_changed", "true" if state_changed else "false")
    print(f"Oxfam fetch OK: {len(ordered)} newest SKUs parsed; {len(new_items)} new.")
    print("Newest SKUs:", ", ".join(ordered[:10]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
