#!/usr/bin/env python3
"""Discover undervalued collectible photobooks from private eBay UK sellers."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ebay_api
import external_monitor
import parr_badger_runner as pb
import photobook_recognition as recognition

BOOKS_CATEGORY_ID = "261186"
SUPPORTED_MARKETPLACE = "EBAY_GB"
OVERLAP_MINUTES = 12
MAX_SEEN = 12000
FIXED_BUYING_OPTIONS = ["FIXED_PRICE", "BEST_OFFER"]
AUCTION_BUYING_OPTIONS = ["AUCTION"]
NON_COLLECTIBLE_BOOK_TERMS = {
    "camera manual",
    "digital photography handbook",
    "family history",
    "how to photograph",
    "how to take photographs",
    "lightroom",
    "local interest",
    "photo editing",
    "photofinish manual",
    "photography handbook",
    "photography manual",
    "photography the smart way",
    "photos that sell",
    "photoshop",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def incremental_start(value: Any) -> str | None:
    parsed = _parse_stamp(value)
    if parsed is None:
        return None
    return utc_stamp(parsed - timedelta(minutes=OVERLAP_MINUTES))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def set_output(name: str, value: Any) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Private eBay config must be a JSON object")
    marketplace = str(payload.get("marketplace") or SUPPORTED_MARKETPLACE).upper()
    if marketplace != SUPPORTED_MARKETPLACE:
        raise RuntimeError(
            "The explicit INDIVIDUAL seller-account filter is currently enabled only for the EBAY_GB private-seller workflow"
        )
    payload["marketplace"] = marketplace
    payload.setdefault("delivery_country", "GB")
    payload.setdefault("category_ids", BOOKS_CATEGORY_ID)
    payload.setdefault("query_result_limit", 200)
    payload.setdefault("contemporary_records_per_run", 4)
    payload.setdefault("classic_records_per_run", 4)
    payload.setdefault("rotating_records_per_run", 4)
    payload.setdefault("contemporary_contributor_queries_per_run", 1)
    payload.setdefault("classic_contributor_queries_per_run", 1)
    payload.setdefault("contemporary_auction_queries_per_run", 1)
    payload.setdefault("classic_auction_queries_per_run", 1)
    payload.setdefault("max_live_checks_per_run", 12)
    payload.setdefault("max_api_calls_per_run", 42)
    payload.setdefault("quota_reserve", 450)
    payload.setdefault("max_pending_live_checks", 100)
    payload.setdefault("issue_threshold", 72)
    payload.setdefault("urgent_threshold", 90)
    payload.setdefault("max_price_gbp", 750)
    payload.setdefault("auction_horizon_hours", 36)
    payload.setdefault("collectible_queries", [])
    for key in ("broad_queries", "collectible_queries", "collection_queries", "wrong_category_queries"):
        value = payload.get(key)
        if not isinstance(value, list):
            raise RuntimeError(f"{key} must be a list")
        payload[key] = [str(item).strip() for item in value if str(item).strip()]
    return payload


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "seen": {},
            "pending_live": {},
            "query_last_checked": {},
            "cursors": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Private eBay state must be a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("seen", {})
    payload.setdefault("pending_live", {})
    payload.setdefault("query_last_checked", {})
    payload.setdefault("cursors", {})
    if not isinstance(payload["seen"], dict):
        payload["seen"] = {}
    if not isinstance(payload["pending_live"], dict):
        payload["pending_live"] = {}
    if not isinstance(payload["query_last_checked"], dict):
        payload["query_last_checked"] = {}
    if not isinstance(payload["cursors"], dict):
        payload["cursors"] = {}
    return payload


def _cycle_slice(values: list[Any], cursor: int, count: int) -> tuple[list[Any], int]:
    if not values or count <= 0:
        return [], 0
    cursor = max(0, int(cursor)) % len(values)
    selected = [values[(cursor + index) % len(values)] for index in range(min(count, len(values)))]
    return selected, (cursor + len(selected)) % len(values)


def _query_key(lane: str, query: str, buying_options: list[str]) -> str:
    options = "+".join(sorted(buying_options))
    normalized = " ".join(query.lower().split())
    return f"{lane}:{options}:{normalized}"


def _source(lane: str) -> dict[str, Any]:
    return {
        "id": f"ebay_private_{lane}",
        "name": f"eBay UK private sellers - {lane}",
        "marketplace": SUPPORTED_MARKETPLACE,
    }


def _record_priority(row: dict[str, Any]) -> int:
    value = str(row.get("Search priority") or "9").strip()
    return int(value) if value.isdigit() else 9


def _is_contemporary_record(row: dict[str, Any]) -> bool:
    return "curated contemporary documentary" in str(row.get("Canon sources") or "").lower()


def _record_identity(row: dict[str, Any]) -> tuple[str, str]:
    return pb.normalize(row.get("Contributor")), pb.normalize(row.get("Title"))


def _priority_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hot = [row for row in rows if _record_priority(row) <= 0]
    return hot or [row for row in rows if _record_priority(row) <= 1]


def build_search_plan(config: dict[str, Any], state: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    records = list(recognition.load_library())
    cursors: dict[str, Any] = state["cursors"]
    plan: list[dict[str, Any]] = []

    def add(
        lane: str,
        query: str,
        *,
        category_ids: str | None = BOOKS_CATEGORY_ID,
        options: list[str] | None = None,
        description: bool = False,
        incremental: bool = True,
        ending_start: str | None = None,
        ending_end: str | None = None,
    ) -> None:
        query = str(query or "").strip()[:100]
        if not query:
            return
        plan.append(
            {
                "lane": lane,
                "query": query,
                "category_ids": category_ids,
                "buying_options": options or FIXED_BUYING_OPTIONS,
                "search_in_description": description,
                "incremental": incremental,
                "ending_start_date": ending_start,
                "ending_end_date": ending_end,
            }
        )

    for query in config["broad_queries"]:
        add("broad", query, description=True)
    for query in config["collectible_queries"]:
        add("collectible_format", query, description=True)
    for query in config["collection_queries"]:
        add("collection", query, description=True)
    for query in config["wrong_category_queries"]:
        add("wrong_category", query, category_ids=None, description=True)

    contemporary_records = [row for row in records if _is_contemporary_record(row)]
    classic_records = [row for row in records if not _is_contemporary_record(row)]
    contemporary_hot = _priority_records(contemporary_records)
    classic_hot = _priority_records(classic_records)
    contemporary_selected, next_contemporary = _cycle_slice(
        contemporary_hot,
        int(cursors.get("contemporary_hot_records", 0) or 0),
        int(config["contemporary_records_per_run"]),
    )
    classic_selected, next_classic = _cycle_slice(
        classic_hot,
        int(cursors.get("classic_hot_records", 0) or 0),
        int(config["classic_records_per_run"]),
    )
    cursors["contemporary_hot_records"] = next_contemporary
    cursors["classic_hot_records"] = next_classic
    for index in range(max(len(contemporary_selected), len(classic_selected))):
        if index < len(contemporary_selected):
            add(
                "contemporary_hot",
                recognition.search_query_for_record(contemporary_selected[index]),
                description=True,
            )
        if index < len(classic_selected):
            add(
                "classic_hot",
                recognition.search_query_for_record(classic_selected[index]),
                description=True,
            )

    hot_keys = {_record_identity(row) for row in contemporary_hot + classic_hot}
    cold_records = [
        row
        for row in records
        if _record_identity(row) not in hot_keys
    ]
    rotating_selected, next_rotation = _cycle_slice(
        cold_records,
        int(cursors.get("library_records", 0) or 0),
        int(config["rotating_records_per_run"]),
    )
    cursors["library_records"] = next_rotation
    for row in rotating_selected:
        add("library_rotation", recognition.search_query_for_record(row), description=False)

    contemporary_contributors = recognition.unique_contributors(contemporary_records)
    classic_contributors = recognition.unique_contributors(classic_records)
    contemporary_contributor_selected, next_contemporary_contributor = _cycle_slice(
        contemporary_contributors,
        int(cursors.get("contemporary_contributors", 0) or 0),
        int(config["contemporary_contributor_queries_per_run"]),
    )
    classic_contributor_selected, next_classic_contributor = _cycle_slice(
        classic_contributors,
        int(cursors.get("classic_contributors", 0) or 0),
        int(config["classic_contributor_queries_per_run"]),
    )
    cursors["contemporary_contributors"] = next_contemporary_contributor
    cursors["classic_contributors"] = next_classic_contributor
    for index in range(max(len(contemporary_contributor_selected), len(classic_contributor_selected))):
        if index < len(contemporary_contributor_selected):
            add("contemporary_contributor", contemporary_contributor_selected[index], description=True)
        if index < len(classic_contributor_selected):
            add("classic_contributor", classic_contributor_selected[index], description=True)

    contemporary_auction_selected, next_contemporary_auction = _cycle_slice(
        contemporary_hot or contemporary_records,
        int(cursors.get("contemporary_auctions", 0) or 0),
        int(config["contemporary_auction_queries_per_run"]),
    )
    classic_auction_selected, next_classic_auction = _cycle_slice(
        classic_hot or classic_records,
        int(cursors.get("classic_auctions", 0) or 0),
        int(config["classic_auction_queries_per_run"]),
    )
    cursors["contemporary_auctions"] = next_contemporary_auction
    cursors["classic_auctions"] = next_classic_auction
    ending_start = utc_stamp(now)
    ending_end = utc_stamp(now + timedelta(hours=int(config["auction_horizon_hours"])))
    for index in range(max(len(contemporary_auction_selected), len(classic_auction_selected))):
        if index < len(contemporary_auction_selected):
            add(
                "contemporary_auction",
                recognition.search_query_for_record(contemporary_auction_selected[index]),
                description=True,
                options=AUCTION_BUYING_OPTIONS,
                incremental=False,
                ending_start=ending_start,
                ending_end=ending_end,
            )
        if index < len(classic_auction_selected):
            add(
                "classic_auction",
                recognition.search_query_for_record(classic_auction_selected[index]),
                description=True,
                options=AUCTION_BUYING_OPTIONS,
                incremental=False,
                ending_start=ending_start,
                ending_end=ending_end,
            )

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for step in plan:
        key = (
            step["lane"],
            " ".join(step["query"].lower().split()),
            "+".join(sorted(step["buying_options"])),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return unique


def trim_search_plan(plan: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Keep the most valuable search lanes when the shared API quota is tight."""
    if budget <= 0:
        return []
    lane_priority = {
        "broad": 0,
        "contemporary_hot": 1,
        "classic_hot": 1,
        "collectible_format": 2,
        "collection": 3,
        "wrong_category": 4,
        "contemporary_auction": 5,
        "classic_auction": 5,
        "library_rotation": 6,
        "contemporary_contributor": 7,
        "classic_contributor": 7,
    }
    ranked = sorted(
        enumerate(plan),
        key=lambda pair: (lane_priority.get(str(pair[1].get("lane") or ""), 99), pair[0]),
    )
    selected_positions = {position for position, _ in ranked[:budget]}
    return [step for position, step in enumerate(plan) if position in selected_positions]


def api_call_budget(
    client: ebay_api.EbayBrowseClient,
    config: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, str | None]:
    configured_budget = max(1, int(config["max_api_calls_per_run"]))
    try:
        quota = client.browse_quota()
    except Exception as exc:
        return configured_budget, None, f"Browse quota lookup failed; using the conservative run cap: {exc}"
    usable = max(0, int(quota.get("remaining") or 0) - int(config["quota_reserve"]))
    return min(configured_budget, usable), quota, None


def run_query(
    client: ebay_api.EbayBrowseClient,
    state: dict[str, Any],
    *,
    lane: str,
    query: str,
    category_ids: str | None,
    buying_options: list[str],
    search_in_description: bool,
    limit: int,
    delivery_country: str,
    max_price_gbp: float,
    detected_at: str,
    incremental: bool,
    ending_start_date: str | None,
    ending_end_date: str | None,
) -> list[dict[str, Any]]:
    key = _query_key(lane, query, buying_options)
    last_checked = state["query_last_checked"].get(key)
    # A newly introduced query must not backfill old active stock. Start it
    # from the monitor's previous run, or just the overlap window on a truly
    # fresh state, so every discovery lane remains new-listings-only.
    incremental_baseline = last_checked or state.get("last_run") or detected_at
    item_start_date = incremental_start(incremental_baseline) if incremental else None
    rows = client.search(
        query,
        limit=limit,
        category_ids=category_ids,
        fixed_price_only=False,
        buying_options=buying_options,
        seller_account_type="INDIVIDUAL",
        delivery_country=delivery_country,
        item_start_date=item_start_date,
        ending_start_date=ending_start_date,
        ending_end_date=ending_end_date,
        search_in_description=search_in_description,
        price_max=max_price_gbp,
        price_currency="GBP",
    )
    state["query_last_checked"][key] = detected_at
    source = _source(lane)
    items: list[dict[str, Any]] = []
    for raw in rows:
        item = ebay_api.listing_from_summary(raw, source)
        if item is None:
            continue
        item["private_seller"] = True
        item["seller_account_type"] = item.get("seller_account_type") or "INDIVIDUAL"
        item["search_lane"] = lane
        item["search_query"] = query
        item["search_in_description"] = search_in_description
        items.append(item)
    return items


def _fallback_score(item: dict[str, Any]) -> tuple[int, list[str]]:
    text = pb.normalize(
        " ".join(
            str(item.get(field) or "")
            for field in ("title", "context", "description", "condition_description")
        )
    )
    score = 0
    reasons: list[str] = []
    photo_hits = [term for term in external_monitor.DIRECT_PHOTO_TERMS if pb.normalize(term) in text]
    if photo_hits:
        score += 22
        reasons.append("photography-book wording")
    collection_evidence = recognition.collection_bundle_evidence(item)
    if collection_evidence:
        score += 26
        reasons.append("collection or job-lot wording")
    if any(pb.normalize(term) in text for term in external_monitor.PUBLISHER_TERMS):
        score += 22
        reasons.append("specialist photobook publisher")
    if any(pb.normalize(term) in text for term in external_monitor.EDITION_TERMS):
        score += 10
        reasons.append("collectible-edition wording")
    lane = str(item.get("search_lane") or "")
    if "wrong_category" in lane or ("collection" in lane and collection_evidence):
        score += 6
        reasons.append("high-recall discovery lane")
    try:
        raw_price = (
            item.get("landed_price_gbp")
            if item.get("landed_price_gbp") is not None
            else item.get("price_gbp")
        )
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is not None and price <= 25:
        score += 14
        reasons.append("very low asking price")
    elif price is not None and price <= 60:
        score += 9
        reasons.append("low asking price")
    elif price is not None and price <= 120:
        score += 4
    if item.get("private_seller"):
        score += 4
        reasons.append("private individual seller")
    if any(
        pb.contains_normalized_phrase(text, pb.normalize(term))
        for term in NON_COLLECTIBLE_BOOK_TERMS
    ):
        score -= 25
        reasons.append("instructional, technical or local-history wording")
    return max(0, min(100, score)), reasons


def classify(item: dict[str, Any]) -> dict[str, Any]:
    classified = dict(item)
    matches = recognition.match_listing(classified)
    if matches:
        best = matches[0]
        score, reasons = recognition.opportunity_score(classified, best)
        classified["recognition_matches"] = matches
        classified["best_recognition"] = best
        classified["opportunity_score"] = score
        classified["opportunity_reasons"] = reasons
        classified["recognized"] = True
        canon_sources = str(best.get("canon_sources") or "").lower()
        classified["collecting_lane"] = (
            "recent documentary"
            if "curated contemporary documentary" in canon_sources
            else "classic or established canon"
        )
        if best.get("collectible_format_evidence"):
            classified["opportunity_kind"] = "special edition or collectible object"
        else:
            classified["opportunity_kind"] = "collectible title or edition lead"
    else:
        score, reasons = _fallback_score(classified)
        classified["recognition_matches"] = []
        classified["opportunity_score"] = score
        classified["opportunity_reasons"] = reasons
        classified["recognized"] = False
        classified["collecting_lane"] = "open discovery"
        classified["opportunity_kind"] = (
            "collection or job lot"
            if recognition.collection_bundle_evidence(classified)
            else "unrecognized collectible-format lead"
        )
    return classified


def _merge_live_detail(item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(item)
    seller = detail.get("seller") if isinstance(detail.get("seller"), dict) else {}
    buying = detail.get("buyingOptions") if isinstance(detail.get("buyingOptions"), list) else []
    price = detail.get("price") if isinstance(detail.get("price"), dict) else {}
    shipping_options = detail.get("shippingOptions") if isinstance(detail.get("shippingOptions"), list) else []
    description = str(detail.get("description") or "").strip()
    aspects = detail.get("localizedAspects") if isinstance(detail.get("localizedAspects"), list) else []
    if description:
        merged["description"] = description[:12000]
    if buying:
        merged["buying_options"] = [str(value) for value in buying]
        merged["tags"] = " ".join(str(value) for value in buying)
    if seller:
        merged["vendor"] = str(seller.get("username") or merged.get("vendor") or "")
        merged["seller_feedback_percentage"] = seller.get("feedbackPercentage", merged.get("seller_feedback_percentage"))
        merged["seller_feedback_score"] = seller.get("feedbackScore", merged.get("seller_feedback_score"))
        account_type = str(seller.get("sellerAccountType") or merged.get("seller_account_type") or "").upper()
        merged["seller_account_type"] = account_type
        if account_type:
            merged["private_seller"] = account_type == "INDIVIDUAL"

    aspect_fields = {
        "author": "author",
        "authors": "author",
        "autor": "author",
        "autore": "author",
        "auteur": "author",
        "edition": "edition",
        "edicion": "edition",
        "edizione": "edition",
        "editie": "edition",
        "auflage": "edition",
        "wydanie": "edition",
        "isbn": "isbn",
        "isbn 10": "isbn",
        "isbn 13": "isbn",
        "publication year": "publication_year",
        "published year": "publication_year",
        "annee de publication": "publication_year",
        "anno di pubblicazione": "publication_year",
        "ano de publicacion": "publication_year",
        "erscheinungsjahr": "publication_year",
        "publicatiejaar": "publication_year",
        "rok wydania": "publication_year",
        "publisher": "publisher",
        "editeur": "publisher",
        "editore": "publisher",
        "editorial": "publisher",
        "uitgever": "publisher",
        "verlag": "publisher",
        "wydawnictwo": "publisher",
        "year": "publication_year",
        "jahr": "publication_year",
        "annee": "publication_year",
        "anno": "publication_year",
        "ano": "publication_year",
        "jaar": "publication_year",
        "rok": "publication_year",
    }
    collected: dict[str, list[str]] = {}
    for aspect in aspects:
        if not isinstance(aspect, dict):
            continue
        name = pb.normalize(aspect.get("name"))
        target = aspect_fields.get(name)
        value = str(aspect.get("value") or "").strip()
        if target and value:
            collected.setdefault(target, []).append(value)
    for target, values in collected.items():
        merged[target] = " | ".join(dict.fromkeys(values))[:1000]

    condition_description = str(detail.get("conditionDescription") or "").strip()
    if condition_description:
        merged["condition_description"] = condition_description[:4000]
    try:
        value = float(price.get("value"))
    except (TypeError, ValueError):
        value = None
    currency = str(price.get("currency") or "").upper()
    if value is not None:
        merged["price_value"] = value
        merged["price_currency"] = currency
        merged["price_gbp"] = value if currency == "GBP" else merged.get("price_gbp")
    shipping_values: list[float] = []
    for option in shipping_options:
        if not isinstance(option, dict):
            continue
        cost = option.get("shippingCost") if isinstance(option.get("shippingCost"), dict) else {}
        cost_currency = str(cost.get("currency") or currency).upper()
        if currency and cost_currency and cost_currency != currency:
            continue
        try:
            shipping_values.append(float(cost.get("value")))
        except (TypeError, ValueError):
            continue
    if shipping_values:
        merged["shipping_value"] = min(shipping_values)
        merged["shipping_currency"] = currency
    shipping_value = merged.get("shipping_value")
    try:
        shipping_number = float(shipping_value) if shipping_value is not None else 0.0
    except (TypeError, ValueError):
        shipping_number = 0.0
    if value is not None and currency == "GBP":
        merged["landed_price_gbp"] = round(value + shipping_number, 2)
    merged["item_end_date"] = str(detail.get("itemEndDate") or merged.get("item_end_date") or "")
    merged["live_estimated_availability"] = str(detail.get("estimatedAvailabilityStatus") or "")
    return merged


def _trim_seen(seen: dict[str, Any]) -> dict[str, Any]:
    if len(seen) <= MAX_SEEN:
        return seen
    ranked: list[tuple[str, str, Any]] = []
    for key, value in seen.items():
        stamp = str(value.get("last_seen") or value.get("first_seen") or "") if isinstance(value, dict) else ""
        ranked.append((stamp, key, value))
    ranked.sort(reverse=True)
    return {key: value for _, key, value in ranked[:MAX_SEEN]}


def _record_seen(seen: dict[str, Any], item: dict[str, Any], detected_at: str) -> None:
    key = str(item.get("key") or "")
    if not key:
        return
    previous = seen.get(key)
    first_seen = (
        str(previous.get("first_seen") or detected_at)
        if isinstance(previous, dict)
        else detected_at
    )
    seen[key] = {
        "first_seen": first_seen,
        "last_seen": detected_at,
        "title": item.get("title"),
        "url": item.get("url"),
        "score": item.get("opportunity_score"),
    }


def _price_line(item: dict[str, Any]) -> str:
    value = item.get("price_value")
    currency = str(item.get("price_currency") or "")
    if value is None:
        return "Price not returned by API"
    if currency == "GBP":
        return f"£{float(value):.2f}"
    return f"{currency} {float(value):.2f}".strip()


def make_issue_body(
    items: list[dict[str, Any]],
    *,
    detected_at: str,
    stats: dict[str, Any],
    failures: list[str],
    urgent_threshold: int,
) -> str:
    lines = [
        "## New private-seller eBay photobook opportunities",
        "",
        f"Detected at **{detected_at}** by the private-seller discovery engine.",
        f"Recognition library: **{stats['records']} books**.",
        "",
        "Every surfaced listing has been re-fetched through eBay's live item endpoint immediately before this issue was created.",
        "The score is a discovery priority, not a purchase verdict. ChatGPT should still verify exact edition, printing, completeness, condition, delivery cost and current market value before recommending a purchase.",
        "",
    ]
    for item in sorted(items, key=lambda value: int(value.get("opportunity_score") or 0), reverse=True):
        score = int(item.get("opportunity_score") or 0)
        urgency = "URGENT" if score >= urgent_threshold else "REVIEW"
        lines.extend(
            [
                f"### {urgency} {score}/100 - {item.get('title') or 'Untitled listing'}",
                "",
                f"- **Observed price:** {_price_line(item)}",
                f"- **Private seller:** {item.get('vendor') or 'eBay individual account'}",
                f"- **Collector lane:** {item.get('collecting_lane') or 'open discovery'}",
                f"- **Opportunity type:** {item.get('opportunity_kind') or 'review lead'}",
                f"- **Discovery lane:** {item.get('search_lane') or 'unknown'}",
                f"- **Search:** `{item.get('search_query') or ''}`",
                f"- **Buying format:** {', '.join(item.get('buying_options') or []) or 'not returned'}",
                f"- **Live verification:** confirmed at {item.get('live_verified_at') or detected_at}",
                f"- **Why it surfaced:** {', '.join(item.get('opportunity_reasons') or [])}",
                f"- **Listing:** {item.get('url')}",
            ]
        )
        best = item.get("best_recognition")
        if isinstance(best, dict):
            canon = str(best.get("canon_sources") or "Recognition library")
            tier = str(best.get("collectibility_tier") or "?")
            year = f" ({best.get('year')})" if best.get("year") else ""
            lines.append(
                f"- **Best recognition:** {best.get('contributor')}, *{best.get('title')}*{year} | match {best.get('score')}/100 | tier {tier} | {canon}"
            )
            if best.get("first_edition_notes"):
                lines.append(f"- **Edition target note:** {best.get('first_edition_notes')}")
            collector_fit = [
                str(best.get("collector_profile") or "").strip(),
                "first monograph" if str(best.get("first_monograph") or "").upper() == "YES" else "",
            ]
            collector_fit = [value for value in collector_fit if value]
            if collector_fit:
                lines.append(f"- **Collector fit:** {' | '.join(collector_fit)}")
            if best.get("awards_and_evidence"):
                lines.append(f"- **Respect evidence:** {best.get('awards_and_evidence')}")
            if best.get("collectible_variants"):
                lines.append(f"- **Known collectible variants:** {best.get('collectible_variants')}")
            if best.get("collectible_format_evidence"):
                lines.append(
                    f"- **Listing object evidence:** {', '.join(best.get('collectible_format_evidence') or [])}"
                )
            if best.get("edition_status"):
                edition_detail = "; ".join(str(value) for value in best.get("edition_reasons") or [])
                lines.append(
                    f"- **Edition evidence:** {best.get('edition_status')}"
                    + (f" | {edition_detail}" if edition_detail else "")
                )
        bibliographic = [
            f"author: {item.get('author')}" if item.get("author") else "",
            f"publisher: {item.get('publisher')}" if item.get("publisher") else "",
            f"year: {item.get('publication_year')}" if item.get("publication_year") else "",
            f"edition: {item.get('edition')}" if item.get("edition") else "",
            f"ISBN: {item.get('isbn')}" if item.get("isbn") else "",
        ]
        bibliographic = [value for value in bibliographic if value]
        if bibliographic:
            lines.append(f"- **eBay bibliographic fields:** {', '.join(bibliographic)}")
        if item.get("image_url"):
            lines.append(f"- **Main image:** {item.get('image_url')}")
        if item.get("description"):
            excerpt = " ".join(str(item.get("description") or "").split())[:700]
            lines.append(f"- **Live description excerpt:** {excerpt}")
        lines.append("")
    if failures:
        lines.extend(["### Temporary search warnings", ""])
        lines.extend(f"- {failure}" for failure in failures[:20])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--state", default="data/ebay_private_seller_state.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    state = load_state(Path(args.state))
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    detected_at = utc_now()
    now = _parse_stamp(detected_at) or datetime.now(timezone.utc)
    stats = recognition.library_stats()
    client = ebay_api.EbayBrowseClient(marketplace=config["marketplace"])
    full_search_plan = build_search_plan(config, state, now)
    call_budget, quota, quota_warning = api_call_budget(client, config)
    reserved_live_checks = min(int(config["max_live_checks_per_run"]), call_budget)
    search_budget = max(0, call_budget - reserved_live_checks)
    search_plan = trim_search_plan(full_search_plan, search_budget)

    if not search_plan:
        write_json(
            runtime / "latest-snapshot.json",
            {
                "checked_at": detected_at,
                "library_stats": stats,
                "planned_queries": 0,
                "quota": quota,
                "quota_warning": quota_warning,
                "skipped": "shared Browse API reserve protected",
                "new_candidates": [],
            },
        )
        set_output("new_count", 0)
        set_output("state_changed", "false")
        set_output("query_count", 0)
        set_output("library_records", stats["records"])
        print("eBay private seller scan skipped to protect the shared Browse API reserve.")
        return 0

    raw_by_key: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    if quota_warning:
        failures.append(quota_warning)
    successful_queries = 0
    for step in search_plan:
        try:
            items = run_query(
                client,
                state,
                lane=step["lane"],
                query=step["query"],
                category_ids=step["category_ids"],
                buying_options=step["buying_options"],
                search_in_description=bool(step["search_in_description"]),
                limit=int(config["query_result_limit"]),
                delivery_country=str(config["delivery_country"]),
                max_price_gbp=float(config["max_price_gbp"]),
                detected_at=detected_at,
                incremental=bool(step.get("incremental", True)),
                ending_start_date=step.get("ending_start_date"),
                ending_end_date=step.get("ending_end_date"),
            )
            successful_queries += 1
        except Exception as exc:
            failures.append(f"{step['lane']} `{step['query']}`: {exc}")
            continue
        for item in items:
            key = str(item.get("key") or "")
            if not key:
                continue
            current = raw_by_key.get(key)
            if current is None:
                raw_by_key[key] = item
            else:
                lanes = {str(current.get("search_lane") or ""), str(item.get("search_lane") or "")}
                current["search_lane"] = "+".join(sorted(lane for lane in lanes if lane))

    if successful_queries == 0:
        raise RuntimeError("All private-seller eBay searches failed; refusing to update state")

    seen: dict[str, Any] = state["seen"]
    pending_live: dict[str, Any] = state["pending_live"]
    unseen_items = [item for key, item in raw_by_key.items() if key not in seen]

    # Carry forward promising listings that could not be live-verified on a prior run.
    # This prevents a strict live-verification rule from losing a listing merely
    # because the hourly API-call budget was exhausted.
    combined_unseen: dict[str, dict[str, Any]] = {}
    for key, item in pending_live.items():
        if key not in seen and isinstance(item, dict):
            combined_unseen[key] = dict(item)
    for item in unseen_items:
        combined_unseen[str(item.get("key") or "")] = item

    classified = [classify(item) for item in combined_unseen.values() if item.get("key")]
    classified.sort(key=lambda item: int(item.get("opportunity_score") or 0), reverse=True)

    issue_threshold = int(config["issue_threshold"])
    pending_keys = set(pending_live)
    live_eligible = [
        item
        for item in classified
        if int(item.get("opportunity_score") or 0) >= max(55, issue_threshold - 12)
    ]
    # Old pending candidates get first claim on the live-check allowance.
    live_eligible.sort(
        key=lambda item: (
            str(item.get("key") or "") in pending_keys,
            int(item.get("opportunity_score") or 0),
        ),
        reverse=True,
    )
    live_allowance = max(0, call_budget - len(search_plan))
    live_check_pool = live_eligible[: min(int(config["max_live_checks_per_run"]), live_allowance)]

    live_checked: dict[str, dict[str, Any]] = {}
    for item in live_check_pool:
        rest_item_id = str(item.get("rest_item_id") or "")
        if not rest_item_id:
            continue
        try:
            is_live, reason, detail = client.live_status(rest_item_id)
        except Exception as exc:
            failures.append(f"live-check {item.get('external_id')}: {exc}")
            continue
        if not is_live:
            stale = dict(item)
            stale["live_verified"] = False
            stale["live_status_reason"] = reason
            live_checked[item["key"]] = stale
            continue
        enriched = _merge_live_detail(item, detail)
        refreshed = classify(enriched)
        refreshed["live_verified"] = True
        refreshed["live_verified_at"] = detected_at
        live_checked[item["key"]] = refreshed

    final_classified: list[dict[str, Any]] = []
    stale_items: list[dict[str, Any]] = []
    for item in classified:
        replacement = live_checked.get(item["key"])
        if replacement is not None:
            if replacement.get("live_status_reason"):
                stale_items.append(replacement)
                continue
            item = replacement
        final_classified.append(item)

    # A purchase candidate is never surfaced unless eBay's live item endpoint
    # has just confirmed that it remains available.
    candidates = [
        item
        for item in final_classified
        if int(item.get("opportunity_score") or 0) >= issue_threshold
        and item.get("live_verified") is True
        and item.get("private_seller") is True
        and str(item.get("seller_account_type") or "").upper() != "BUSINESS"
    ]
    candidate_keys = {str(item.get("key") or "") for item in candidates}

    new_pending: dict[str, Any] = {}
    for item in final_classified:
        key = str(item.get("key") or "")
        score = int(item.get("opportunity_score") or 0)
        if score >= issue_threshold and key not in candidate_keys:
            pending_copy = dict(item)
            pending_copy.pop("live_verified", None)
            pending_copy["pending_since"] = (
                str(pending_live.get(key, {}).get("pending_since") or detected_at)
                if isinstance(pending_live.get(key), dict)
                else detected_at
            )
            new_pending[key] = pending_copy
            continue
        _record_seen(seen, item, detected_at)

    for item in stale_items:
        _record_seen(seen, item, detected_at)

    pending_limit = int(config["max_pending_live_checks"])
    ranked_pending = sorted(
        new_pending.items(),
        key=lambda pair: (
            int(pair[1].get("opportunity_score") or 0),
            str(pair[1].get("pending_since") or ""),
        ),
        reverse=True,
    )[:pending_limit]
    state["pending_live"] = dict(ranked_pending)
    state["seen"] = _trim_seen(seen)
    state["last_run"] = detected_at
    state["last_query_count"] = len(search_plan)
    state["last_successful_queries"] = successful_queries
    state["last_live_checks"] = len(live_checked)
    state["last_failure_count"] = len(failures)
    state["library_records"] = stats["records"]
    state["last_api_call_budget"] = call_budget
    state["last_browse_quota"] = quota

    write_json(runtime / "proposed-state.json", state)
    write_json(
        runtime / "latest-snapshot.json",
        {
            "checked_at": detected_at,
            "library_stats": stats,
            "quota": quota,
            "api_call_budget": call_budget,
            "planned_queries": len(search_plan),
            "successful_queries": successful_queries,
            "live_checks": len(live_checked),
            "failures": failures,
            "unique_results": len(raw_by_key),
            "unseen_results": len(unseen_items),
            "pending_live_verification": len(state["pending_live"]),
            "new_candidates": candidates,
        },
    )

    if candidates:
        write_json(runtime / "new-items.json", candidates)
        title = (
            f"EBAY_PRIVATE_NEW: {len(candidates)} private-seller photobook "
            f"candidate{'s' if len(candidates) != 1 else ''}"
        )
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            make_issue_body(
                candidates,
                detected_at=detected_at,
                stats=stats,
                failures=failures,
                urgent_threshold=int(config["urgent_threshold"]),
            ),
            encoding="utf-8",
        )

    set_output("new_count", len(candidates))
    set_output("state_changed", "true")
    set_output("query_count", len(search_plan))
    set_output("library_records", stats["records"])
    print(
        f"eBay private seller scan: {len(search_plan)} planned queries, {successful_queries} succeeded, "
        f"{len(raw_by_key)} unique results, {len(unseen_items)} unseen, "
        f"{len(state['pending_live'])} pending live checks, {len(candidates)} live candidates."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
