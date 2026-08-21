#!/usr/bin/env python3
"""Shared helpers for Oxfam's broad Art & Photography parent-category scans."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

BASE_URL = "https://onlineshop.oxfam.org.uk"
SEARCH_URL = BASE_URL + "/ccstore/v1/search"
CATALOG_ID = "Oxfam_GB"

# User-facing route supplied by Oxfam. We resolve its current Oracle dimensionId
# dynamically instead of hard-coding it.
TARGET_PARENT_ROUTE = "/art-and-photography/category/art-photography"

# Existing Photography child dimension, used only as a discovery fallback.
PHOTOGRAPHY_DIMENSION_ID = "2776812252"

HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": BASE_URL + TARGET_PARENT_ROUTE,
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

SKU_RE = re.compile(r"HD_{1,2}(\d+)")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(url: str, params: dict[str, str] | None = None, retries: int = 4) -> dict[str, Any]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"HTTP {status} from {url}")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Expected JSON object from {url}")
                return payload
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


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


def normalize_route(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    route = value.strip()
    if route.startswith(BASE_URL):
        route = route[len(BASE_URL):]
    return route.rstrip("/")


def _find_target_collection(payload: dict[str, Any]) -> tuple[str, str | None] | None:
    target = TARGET_PARENT_ROUTE.rstrip("/")
    for node in iter_dicts(payload):
        if normalize_route(node.get("route")) != target:
            continue
        dimension = node.get("dimensionId")
        repo_id = node.get("repositoryId") or node.get("id")
        if dimension not in (None, ""):
            return str(dimension), str(repo_id) if repo_id else None
    return None


def discover_parent_dimension_id() -> tuple[str, str | None]:
    """Resolve the current Oracle search dimension for the parent Art & Photography route."""
    # Fast path: ask Oracle for a deep expanded root collection.
    root_url = BASE_URL + "/ccstore/v1/collections/rootCategory"
    try:
        payload = request_json(root_url, {
            "catalogId": CATALOG_ID,
            "expand": "childCategories",
            "maxLevel": "20",
            "disableActiveProdCheck": "true",
        })
        found = _find_target_collection(payload)
        if found:
            return found
    except Exception as exc:
        print(f"Deep root collection discovery failed: {exc}")

    # Robust path: breadth-first collection traversal by repository ID.
    queue = ["rootCategory"]
    seen: set[str] = set()
    while queue and len(seen) < 1200:
        repo_id = queue.pop(0)
        if repo_id in seen:
            continue
        seen.add(repo_id)
        try:
            payload = request_json(
                BASE_URL + "/ccstore/v1/collections/" + urllib.parse.quote(repo_id, safe=""),
                {
                    "catalogId": CATALOG_ID,
                    "expand": "childCategories",
                    "disableActiveProdCheck": "true",
                },
                retries=2,
            )
        except Exception:
            continue

        found = _find_target_collection(payload)
        if found:
            return found

        for node in iter_dicts(payload):
            children = node.get("childCategories")
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                cid = child.get("repositoryId") or child.get("id")
                if cid and str(cid) not in seen:
                    queue.append(str(cid))

    # Final fallback: the known Photography child search response may expose its ancestors.
    payload = request_json(SEARCH_URL, {
        "N": PHOTOGRAPHY_DIMENSION_ID,
        "No": "0",
        "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)",
        "Nrpp": "1",
        "Ns": "product.creationDate|1",
    })
    found = _find_target_collection(payload)
    if found:
        return found

    raise RuntimeError(
        "Could not resolve the Art & Photography parent category's Oracle dimensionId "
        f"for route {TARGET_PARENT_ROUTE!r}"
    )


def _collection_node(payload: dict[str, Any], repository_id: str) -> dict[str, Any] | None:
    for node in iter_dicts(payload):
        node_id = node.get("repositoryId") or node.get("id")
        if str(node_id or "") == repository_id and node.get("dimensionId") not in (None, ""):
            return node
    for node in iter_dicts(payload):
        if node.get("dimensionId") not in (None, "") and isinstance(node.get("childCategories"), list):
            return node
    return None


def discover_leaf_dimension_ids(parent_repository_id: str | None) -> list[str]:
    """Return leaf dimensions beneath the parent to bypass Oracle's roughly 10k result cap."""
    if not parent_repository_id:
        return []

    queue = [parent_repository_id]
    seen: set[str] = set()
    leaves: list[str] = []
    while queue and len(seen) < 1200:
        repository_id = queue.pop(0)
        if repository_id in seen:
            continue
        seen.add(repository_id)
        payload = request_json(
            BASE_URL + "/ccstore/v1/collections/" + urllib.parse.quote(repository_id, safe=""),
            {
                "catalogId": CATALOG_ID,
                "expand": "childCategories",
                "disableActiveProdCheck": "true",
            },
            retries=3,
        )
        node = _collection_node(payload, repository_id)
        if not node:
            raise RuntimeError(f"Could not parse Oxfam collection {repository_id!r}")

        child_ids: list[str] = []
        children = node.get("childCategories")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_id = child.get("repositoryId") or child.get("id")
                if child_id:
                    child_ids.append(str(child_id))

        if child_ids:
            queue.extend(child_id for child_id in child_ids if child_id not in seen)
        elif repository_id != parent_repository_id:
            dimension_id = node.get("dimensionId")
            if dimension_id not in (None, ""):
                leaves.append(str(dimension_id))

    return list(dict.fromkeys(leaves))


def find_results_summary(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("searchEventSummary")
    if not isinstance(event, dict):
        raise RuntimeError("Search response is missing searchEventSummary")
    summaries = event.get("resultsSummary")
    if not isinstance(summaries, list):
        raise RuntimeError("Search response is missing resultsSummary")

    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        records = summary.get("records")
        if not isinstance(records, list):
            continue
        sort = summary.get("sort")
        if isinstance(sort, dict):
            keys = sort.get("sortKeys")
            if isinstance(keys, list) and any(
                isinstance(k, dict)
                and k.get("attribute") == "product.creationDate"
                and k.get("direction") == "desc"
                for k in keys
            ):
                return summary
    # Full scans do not depend on sorting for completeness, but the live monitor does.
    for summary in summaries:
        if isinstance(summary, dict) and isinstance(summary.get("records"), list):
            return summary
    raise RuntimeError("No record-bearing resultsSummary found")


def total_matching_records(payload: dict[str, Any]) -> int | None:
    summary = find_results_summary(payload)
    value = summary.get("totalMatchingRecords")
    return value if isinstance(value, int) else None


def require_newest_first(payload: dict[str, Any]) -> None:
    """Fail closed when a live monitor response is not verified newest-first."""
    summary = find_results_summary(payload)
    sort = summary.get("sort")
    keys = sort.get("sortKeys") if isinstance(sort, dict) else None
    if not isinstance(keys, list) or not any(
        isinstance(key, dict)
        and key.get("attribute") == "product.creationDate"
        and key.get("direction") == "desc"
        for key in keys
    ):
        raise RuntimeError("Could not verify newest-first product.creationDate ordering")


def fetch_search(
    dimension_id: str,
    offset: int,
    page_size: int,
    sort_key: str = "product.creationDate|1",
) -> dict[str, Any]:
    return request_json(SEARCH_URL, {
        "N": dimension_id,
        "No": str(offset),
        "Nr": "AND(NOT(sku.listPrice:0.000000),product.active:1)",
        "Nrpp": str(page_size),
        "Ns": sort_key,
    })


def ordered_skus(payload: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    summary = find_results_summary(payload)
    ordered: list[str] = []
    product_ids: dict[str, str] = {}
    for record in summary.get("records", []):
        if not isinstance(record, dict):
            continue
        sku = canonical_sku(record.get("sku.listingId")) or canonical_sku(record.get("record.id"))
        if not sku or sku in ordered:
            continue
        ordered.append(sku)
        rid = record.get("record.id")
        if isinstance(rid, str) and "/sku-" in rid:
            tail = rid.split("/sku-", 1)[1]
            pid = tail.split("..", 1)[0]
            if pid:
                product_ids[sku] = pid
    return ordered, product_ids


def scalarize(value: Any) -> Any:
    if isinstance(value, list):
        simple = [scalarize(v) for v in value]
        simple = [v for v in simple if v not in (None, "", [])]
        if len(simple) == 1:
            return simple[0]
        return simple[:20]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


def collect_metadata(payload: dict[str, Any], allowed_skus: set[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {sku: {} for sku in allowed_skus}
    wanted_fragments = (
        "displayname", "title", "name", "description", "author", "photograph",
        "publisher", "isbn", "condition", "price", "creationdate", "route", "url",
        "format", "year", "category", "binding", "edition", "book", "ox_",
    )
    for node in iter_dicts(payload):
        attrs = node.get("attributes") if isinstance(node.get("attributes"), dict) else {}
        candidates = [
            node.get("sku.listingId"), node.get("listingId"), node.get("record.id"), node.get("id"),
            attrs.get("sku.listingId"), attrs.get("record.id"), attrs.get("id"),
        ]
        sku = next((canonical_sku(v) for v in candidates if canonical_sku(v)), None)
        if not sku or sku not in allowed_skus:
            continue
        merged = {**attrs, **node}
        dest = out[sku]
        for key, value in merged.items():
            kl = str(key).lower()
            if not (kl.startswith("product.") or kl.startswith("sku.") or any(f in kl for f in wanted_fragments)):
                continue
            simple = scalarize(value)
            if simple not in (None, "", []):
                dest.setdefault(str(key), simple)
    return out


def first_value(meta: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if value not in (None, ""):
            return value
    return None


def fuzzy_value(meta: dict[str, Any], fragments: tuple[str, ...]) -> Any:
    for key, value in meta.items():
        kl = key.lower()
        if any(f in kl for f in fragments):
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
        if isinstance(value, str):
            m = re.search(r"(\d+(?:\.\d{1,2})?)", value.replace(",", ""))
            if m:
                return round(float(m.group(1)), 2)
        return None


def strip_html(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    return text or None


def item_from_meta(sku: str, product_id: str | None, meta: dict[str, Any]) -> dict[str, Any]:
    title = first_value(meta, [
        "product.displayName", "displayName", "product.title", "title", "product.name", "name"
    ]) or fuzzy_value(meta, ("displayname", "title"))
    description = first_value(meta, [
        "product.longDescription", "longDescription", "product.description", "description"
    ]) or fuzzy_value(meta, ("description",))
    author = first_value(meta, [
        "product.author", "author", "product.bookAuthor", "book-author", "product.ox_author"
    ]) or fuzzy_value(meta, ("author",))
    publisher = first_value(meta, ["product.publisher", "publisher"]) or fuzzy_value(meta, ("publisher",))
    route = first_value(meta, ["product.route", "route", "product.url", "url"]) or fuzzy_value(meta, ("route",))
    condition = first_value(meta, ["product.condition", "condition", "sku.condition"]) or fuzzy_value(meta, ("condition",))
    price = normalize_price(first_value(meta, [
        "sku.activePrice", "activePrice", "sku.listPrice", "listPrice", "sku.minActivePrice"
    ]) or fuzzy_value(meta, ("activeprice", "listprice")))
    return {
        "sku": sku,
        "product_id": product_id,
        "title": strip_html(title),
        "author": strip_html(author),
        "price_gbp": price,
        "description": strip_html(description),
        "condition": strip_html(condition),
        "publisher": strip_html(publisher),
        "isbn": strip_html(first_value(meta, ["product.isbn", "isbn", "ISBN"]) or fuzzy_value(meta, ("isbn",))),
        "creation_date": first_value(meta, ["product.creationDate", "creationDate"]) or fuzzy_value(meta, ("creationdate",)),
        "route": route,
    }


def absolute_product_url(item: dict[str, Any]) -> str:
    route = str(item.get("route") or "")
    if route.startswith("http://") or route.startswith("https://"):
        return route
    if route.startswith("/"):
        return BASE_URL + route
    sku = item.get("sku", "")
    return BASE_URL + "/searchresults?Ntt=" + urllib.parse.quote(str(sku))


def searchable_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(k) or "") for k in (
        "title", "author", "description", "publisher", "condition", "isbn", "route"
    )).lower()
