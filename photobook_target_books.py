#!/usr/bin/env python3
"""175-photographer roster with an evidence-backed, growing book target layer.

Only CORE library books enter the curated target set. Artists without a curated
book retain name discovery while their bibliographies are being researched.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import ebay_core_targets as core
import parr_badger_runner as pb
import photobook_recognition as recognition


@lru_cache(maxsize=1)
def registry() -> dict[str, dict[str, Any]]:
    records = {
        core.normalized(record["name"]): {**record, "books": []}
        for record in core.target_records()
    }
    for book in recognition.load_library():
        owner = records.get(core.normalized(book.get("Contributor")))
        if owner is not None and str(book.get("Search tier") or "").upper() == "CORE":
            owner["books"].append(book)
    return records


def coverage() -> dict[str, dict[str, int]]:
    return {
        tier: {
            "photographers": sum(row["tier"] == tier for row in registry().values()),
            "with_books": sum(row["tier"] == tier and bool(row["books"]) for row in registry().values()),
            "books": sum(len(row["books"]) for row in registry().values() if row["tier"] == tier),
        }
        for tier in ("1", "2", "3")
    }


def _matches_core_book(match: dict[str, Any]) -> bool:
    owner = registry().get(core.normalized(match.get("contributor")))
    return bool(owner and any(
        pb.normalize(book["Title"]) == pb.normalize(match.get("title"))
        for book in owner["books"]
    ))


def _title_only_match(item: dict[str, Any]) -> dict[str, Any] | None:
    title = pb.normalize(item.get("title"))
    if not title:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for owner in registry().values():
        for row in owner["books"]:
            for value in [row["Title"], *row.get("_title_aliases", [])]:
                phrase = pb.normalize(value)
                # Eponymous, one-word and short titles need the photographer's
                # identity; they are too easy to confuse with unrelated books.
                if len(phrase) < 12 or len(pb.useful_tokens(phrase)) < 2:
                    continue
                if not pb.contains_normalized_phrase(title, phrase):
                    continue
                name = pb.normalize(owner["name"])
                if pb.contains_normalized_phrase(title, name):
                    continue  # The ordinary recognizer already sees this.
                context = pb.normalize(" ".join(str(item.get(key) or "") for key in (
                    "title", "context", "category_path", "publisher", "publication_year")))
                publisher = pb.normalize(row.get("Publisher"))
                year = str(row.get("Year") or "")
                is_book = (str(item.get("category_id") or "") == core.BOOKS_CATEGORY_ID
                           or any(pb.contains_normalized_phrase(context, term) for term in
                                  ("book", "photobook", "photography", "monograph")))
                bibliographic = bool((publisher and pb.contains_normalized_phrase(context, publisher))
                                     or (year and year in context.split()))
                if not (is_book or bibliographic):
                    continue
                candidates.append((len(phrase), {
                    "record_id": row.get("Record ID"), "score": 85,
                    "reason": "distinctive target title without photographer name",
                    "contributor": owner["name"], "title": row["Title"],
                    "year": row.get("Year"), "publisher": row.get("Publisher"),
                    "isbn": row.get("ISBN"), "edition_traps": row.get("Edition traps", "").split(" | "),
                    "collectibility_tier": row.get("Collectibility tier"),
                    "strong_buy_gbp": recognition._float(row.get("Strong buy GBP")),
                    "bargain_gbp": recognition._float(row.get("Bargain GBP")),
                }))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    best = candidates[0][1]
    # A shared phrase cannot identify a title-only book unambiguously.
    if any(other["contributor"] != best["contributor"]
           and other["title"] != best["title"] for _, other in candidates[1:]):
        return None
    return best


def assess_listing(item: dict[str, Any]) -> dict[str, Any]:
    matches = (item["recognition_matches"] if "recognition_matches" in item
               else recognition.match_listing(item, limit=10))
    match = next((entry for entry in matches if _matches_core_book(entry)), None)
    if match is None:
        match = _title_only_match(item)
    visible = core.matches_for_item(item)
    if match is None:
        known_other = ((item.get("best_recognition") or {}).get("contributor")
                       or (matches[0].get("contributor") if matches else None))
        non_target_book = bool(known_other and visible and
                               core.normalized(known_other) == core.normalized(visible[0]["name"]))
        return {"hunt": "photographer discovery" if visible else "open discovery",
                "target_book": None, "non_target_book": non_target_book,
                "collectibility": "unassessed", "identification_confidence": "unassessed",
                "price_opportunity": "unassessed"}

    owner = registry()[core.normalized(match["contributor"])]
    title_only = not pb.contains_normalized_phrase(
        pb.normalize(item.get("title")), pb.normalize(owner["name"])
    )
    edition, reasons = recognition.assess_edition(item, match)
    traps = [term for term in match.get("edition_traps") or [] if term.strip()]
    listing_text = pb.normalize(" ".join(str(item.get(field) or "") for field in
                                       ("title", "description", "edition", "publisher")))
    trap_hits = [term for term in traps
                 if pb.contains_normalized_phrase(listing_text, pb.normalize(term))]
    if trap_hits:
        edition = "mismatch"
        reasons = [reason for reason in reasons
                   if reason != "exact collectible edition is not established by the listing metadata"]
        reasons += [f"different edition or study: {term}" for term in trap_hits]
    # A title-only match identifies a work, not a photographer/edition with
    # certainty. Require a title-page or colophon check before any buying call.
    confidence = ({"confirmed": "high", "plausible": "medium", "claimed": "low",
                   "unknown": "low", "mismatch": "conflict"}[edition])
    if title_only and confidence == "high":
        confidence = "medium"
    price = item.get("landed_price_gbp", item.get("price_gbp"))
    try:
        price = float(price) if price is not None else None
    except (ValueError, TypeError):
        price = None
    bargain = recognition._float(match.get("bargain_gbp"))
    strong = recognition._float(match.get("strong_buy_gbp"))
    price_opportunity = "unassessed"
    if edition != "mismatch" and price is not None:
        if bargain is not None and price <= bargain:
            price_opportunity = "below curated bargain ceiling"
        elif strong is not None and price <= strong:
            price_opportunity = "below curated investigation ceiling"
    return {
        "hunt": "target book", "target_book": f"{owner['name']}: {match['title']}",
        "target_tier": owner["tier"], "title_only": title_only,
        "non_target_book": False,
        "collectibility": {"S": "very high", "A": "high", "B": "moderate"}.get(
            str(match.get("collectibility_tier") or "").upper(), "unassessed"),
        "identification_confidence": confidence,
        "edition_status": edition, "edition_reasons": reasons,
        "known_later_edition": bool(trap_hits),
        "price_opportunity": price_opportunity,
    }


def title_query_groups(limit: int = 90) -> list[dict[str, Any]]:
    """Rotating title-only search covers omitted photographer names at zero extra quota."""
    names: list[str] = []
    for owner in registry().values():
        for row in owner["books"]:
            title = str(row.get("Title") or "")
            if len(pb.useful_tokens(title)) >= 2 and len(pb.normalize(title)) >= 12:
                names.append(title)
    return core.compile_or_queries(names, limit)
