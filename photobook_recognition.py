#!/usr/bin/env python3
"""Recognition and opportunity scoring for collectible photobook listings.

The recognition library is layered rather than duplicated:
1. Parr/Badger operational master
2. Roth 101 overlay from canon_runner
3. supplemental CSV shards in data/photobook_recognition/

This keeps the existing 628-record baseline audit stable while allowing the
private-seller discovery engine to grow toward several thousand book records.
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import canon_runner
import parr_badger_runner as pb

SUPPLEMENT_DIR = Path("data/photobook_recognition")
CSV_GLOB = "*.csv"
GENERIC_LISTING_TERMS = {
    "photography book",
    "photo book",
    "photographs book",
    "old photography book",
    "art photography book",
    "coffee table book",
    "book of photographs",
}
CASUAL_SIGNALS = {
    "job lot",
    "bundle",
    "collection of books",
    "book collection",
    "house clearance",
    "clearance",
    "loft",
    "attic",
    "estate",
    "inherited",
    "found",
    "old book",
    "old books",
    "not sure",
    "dont know",
    "don't know",
    "selling for",
}
EXPERT_SIGNALS = {
    "first edition",
    "first printing",
    "first impression",
    "signed",
    "inscribed",
    "association copy",
    "parr badger",
    "parr & badger",
    "roth 101",
    "rare",
    "scarce",
    "collectable",
    "collectible",
    "limited edition",
    "edition of",
    "provenance",
    "dustwrapper",
    "dust jacket",
    "fine/fine",
    "near fine",
    "bibliography",
}
TIER_BONUS = {"S": 25, "A": 20, "B": 14, "C": 8, "D": 3}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    text = _clean(value).replace("£", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _split_aliases(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    parts = re.split(r"\s*\|\s*|\s*;\s*", text)
    return [part for part in parts if part]


def _record_key(contributor: Any, title: Any) -> tuple[str, str]:
    return pb.normalize(contributor), pb.normalize(title)


def _prepare(row: dict[str, Any]) -> dict[str, Any]:
    title = _clean(row.get("Title"))
    contributor = _clean(row.get("Contributor"))
    title_aliases = _split_aliases(row.get("Title aliases"))
    contributor_aliases = _split_aliases(row.get("Contributor aliases"))
    row["_title_norm"] = pb.normalize(title)
    row["_title_tokens"] = pb.useful_tokens(title)
    contributor_tokens = set(pb.contributor_tokens(contributor))
    for alias in contributor_aliases:
        contributor_tokens.update(pb.contributor_tokens(alias))
    row["_contributor_tokens"] = contributor_tokens
    row["_title_aliases"] = title_aliases
    row["_contributor_aliases"] = contributor_aliases
    return row


def _canon_to_record(row: dict[str, Any]) -> dict[str, Any]:
    volumes = _clean(row.get("Volumes"))
    refs = _clean(row.get("PB page / refs"))
    roth = volumes == "R101" or "Roth 101" in refs or _clean(row.get("Roth 101")).lower() == "yes"
    if volumes == "R101":
        source_label = "Roth 101"
    elif volumes:
        source_label = f"Parr/Badger V{volumes.replace(';', '/')}"
        if roth:
            source_label += " + Roth 101"
    else:
        source_label = "Photobook canon"
    search_tier = _clean(row.get("Search tier")).upper() or "CORE"
    record = {
        "Record ID": f"canon:{pb.normalize(row.get('Contributor'))}:{pb.normalize(row.get('Title'))}",
        "Contributor": _clean(row.get("Contributor")),
        "Contributor aliases": "",
        "Title": _clean(row.get("Title")),
        "Title aliases": "",
        "Year": _clean(row.get("Year")),
        "Publisher": _clean(row.get("Publisher")),
        "ISBN": "",
        "Canon sources": source_label,
        "Collectibility tier": "S" if roth else ("A" if search_tier == "CORE" else "B"),
        "Search priority": "1" if roth else ("2" if search_tier == "CORE" else "3"),
        "First edition notes": "",
        "Strong buy GBP": "",
        "Bargain GBP": "",
        "Evidence confidence": _clean(row.get("Best confidence")),
        "Source": refs or source_label,
        "Search tier": search_tier,
    }
    return _prepare(record)


def _load_supplement_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            if not _clean(raw.get("Title")):
                continue
            row = dict(raw)
            row.setdefault("Search tier", "CORE")
            row["Search tier"] = _clean(row.get("Search tier")).upper() or "CORE"
            rows.append(_prepare(row))
    return rows


def _merge_record(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if key.startswith("_"):
            continue
        if _clean(value):
            if key == "Canon sources" and _clean(merged.get(key)):
                sources = [part.strip() for part in f"{merged[key]} | {value}".split("|") if part.strip()]
                merged[key] = " | ".join(dict.fromkeys(sources))
            else:
                merged[key] = value
    return _prepare(merged)


@lru_cache(maxsize=1)
def load_library() -> tuple[dict[str, Any], ...]:
    """Load and de-duplicate the complete local recognition library."""
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in canon_runner.load_canon_master():
        row = _canon_to_record(dict(raw))
        records[_record_key(row["Contributor"], row["Title"])] = row

    if SUPPLEMENT_DIR.exists():
        for path in sorted(SUPPLEMENT_DIR.glob(CSV_GLOB)):
            for row in _load_supplement_file(path):
                key = _record_key(row.get("Contributor"), row.get("Title"))
                if key in records:
                    records[key] = _merge_record(records[key], row)
                else:
                    records[key] = row

    ordered = sorted(
        records.values(),
        key=lambda row: (
            int(_clean(row.get("Search priority")) or "9"),
            pb.normalize(row.get("Contributor")),
            pb.normalize(row.get("Title")),
        ),
    )
    return tuple(ordered)


def library_stats() -> dict[str, Any]:
    rows = load_library()
    tiers: dict[str, int] = {}
    priorities: dict[str, int] = {}
    for row in rows:
        tier = _clean(row.get("Collectibility tier")).upper() or "?"
        priority = _clean(row.get("Search priority")) or "?"
        tiers[tier] = tiers.get(tier, 0) + 1
        priorities[priority] = priorities.get(priority, 0) + 1
    return {"records": len(rows), "tiers": tiers, "priorities": priorities}


def _score_alias(row: dict[str, Any], alias: str, listing_title: str, listing_full: str) -> tuple[int, str] | None:
    alias_row = dict(row)
    alias_row["_title_norm"] = pb.normalize(alias)
    alias_row["_title_tokens"] = pb.useful_tokens(alias)
    scored = pb.score_record(alias_row, listing_title, listing_full)
    if scored is None:
        return None
    score, reason = scored
    return score, f"{reason} via title alias"


def match_listing(item: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """Fuzzy-match a listing against the complete recognition library."""
    tags = item.get("tags")
    tags_text = " ".join(str(value) for value in tags) if isinstance(tags, list) else _clean(tags)
    listing_title = pb.normalize(item.get("title"))
    listing_full = pb.normalize(
        " ".join(
            [
                _clean(item.get("title")),
                _clean(item.get("context")),
                _clean(item.get("description")),
                _clean(item.get("vendor")),
                _clean(item.get("publisher")),
                _clean(item.get("isbn")),
                tags_text,
            ]
        )
    )
    if not listing_full:
        return []

    matches: list[dict[str, Any]] = []
    for row in load_library():
        scored = pb.score_record(row, listing_title, listing_full)
        if scored is None:
            for alias in row.get("_title_aliases") or []:
                scored = _score_alias(row, alias, listing_title, listing_full)
                if scored is not None:
                    break
        if scored is None:
            continue
        score, reason = scored
        matches.append(
            {
                "record_id": _clean(row.get("Record ID")),
                "score": int(score),
                "reason": reason,
                "contributor": _clean(row.get("Contributor")),
                "title": _clean(row.get("Title")),
                "year": _clean(row.get("Year")),
                "publisher": _clean(row.get("Publisher")),
                "isbn": _clean(row.get("ISBN")),
                "canon_sources": _clean(row.get("Canon sources")),
                "collectibility_tier": _clean(row.get("Collectibility tier")).upper(),
                "search_priority": _clean(row.get("Search priority")),
                "first_edition_notes": _clean(row.get("First edition notes")),
                "strong_buy_gbp": _float(row.get("Strong buy GBP")),
                "bargain_gbp": _float(row.get("Bargain GBP")),
                "evidence_confidence": _clean(row.get("Evidence confidence")),
                "source": _clean(row.get("Source")),
            }
        )

    matches.sort(
        key=lambda match: (
            int(match["score"]),
            TIER_BONUS.get(str(match.get("collectibility_tier") or "").upper(), 0),
            -int(str(match.get("search_priority") or "9") if str(match.get("search_priority") or "").isdigit() else 9),
        ),
        reverse=True,
    )
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        key = _record_key(match.get("contributor"), match.get("title"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
        if len(unique) >= max(1, int(limit)):
            break
    return unique


def search_query_for_record(row: dict[str, Any]) -> str:
    """Build a compact eBay query that stays within the Browse API q limit."""
    contributor = _clean(row.get("Contributor"))
    title = _clean(row.get("Title"))
    query = " ".join(part for part in (contributor, title) if part).strip()
    return query[:100].strip()


def unique_contributors(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None) -> list[str]:
    source = rows if rows is not None else load_library()
    names: dict[str, str] = {}
    for row in source:
        name = _clean(row.get("Contributor"))
        norm = pb.normalize(name)
        if name and norm and norm not in names:
            names[norm] = name
    return sorted(names.values(), key=pb.normalize)


def _listing_text(item: dict[str, Any]) -> str:
    return pb.normalize(
        " ".join(
            [
                _clean(item.get("title")),
                _clean(item.get("context")),
                _clean(item.get("description")),
                _clean(item.get("condition")),
                _clean(item.get("category_path")),
            ]
        )
    )


def opportunity_score(item: dict[str, Any], match: dict[str, Any]) -> tuple[int, list[str]]:
    """Score how interesting an identified private-seller listing looks.

    Recognition strength and collectibility dominate. Low price and signs that
    the seller has not bibliographically described the book add opportunity.
    Expert seller language reduces the mispricing component but never cancels a
    strong canonical match.
    """
    score = round(int(match.get("score") or 0) * 0.48)
    reasons: list[str] = [f"recognition {int(match.get('score') or 0)}/100"]

    tier = str(match.get("collectibility_tier") or "").upper()
    tier_bonus = TIER_BONUS.get(tier, 0)
    score += tier_bonus
    if tier_bonus:
        reasons.append(f"collectibility tier {tier}")

    price = item.get("price_gbp")
    try:
        price_gbp = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_gbp = None
    strong_buy = match.get("strong_buy_gbp")
    bargain = match.get("bargain_gbp")
    if price_gbp is not None:
        if bargain is not None and price_gbp <= float(bargain):
            score += 18
            reasons.append("at or below curated bargain benchmark")
        elif strong_buy is not None and price_gbp <= float(strong_buy):
            score += 12
            reasons.append("below curated strong-buy benchmark")
        elif price_gbp <= 25:
            score += 10
            reasons.append("very low asking price")
        elif price_gbp <= 60:
            score += 6
            reasons.append("low asking price")
        elif price_gbp <= 120:
            score += 3

    buying = {str(value).upper() for value in item.get("buying_options", []) if str(value)}
    if "FIXED_PRICE" in buying:
        score += 3
        reasons.append("immediate fixed-price purchase possible")
    if "BEST_OFFER" in buying:
        score += 2
        reasons.append("Best Offer available")

    account_type = _clean(item.get("seller_account_type")).upper()
    if account_type == "INDIVIDUAL" or item.get("private_seller") is True:
        score += 4
        reasons.append("private individual seller")

    text = _listing_text(item)
    casual = sorted(signal for signal in CASUAL_SIGNALS if pb.normalize(signal) in text)
    if casual:
        score += min(9, 3 + len(casual) * 2)
        reasons.append("casual seller wording")

    title_norm = pb.normalize(item.get("title"))
    if title_norm in {pb.normalize(term) for term in GENERIC_LISTING_TERMS}:
        score += 7
        reasons.append("generic listing title")
    elif len(title_norm.split()) <= 5:
        score += 3
        reasons.append("brief listing title")

    context = _clean(item.get("context"))
    description = _clean(item.get("description"))
    if len(context) + len(description) < 180:
        score += 4
        reasons.append("minimal bibliographic detail")

    year = _clean(match.get("year"))
    publisher = _clean(match.get("publisher"))
    if year and pb.normalize(year) not in text:
        score += 2
        reasons.append("seller does not mention expected year")
    if publisher and not any(token in text for token in pb.useful_tokens(publisher)):
        score += 2
        reasons.append("seller does not mention expected publisher")

    contributor = pb.normalize(match.get("contributor"))
    title = pb.normalize(match.get("title"))
    if contributor and contributor not in title_norm:
        score += 2
        reasons.append("photographer not fully identified in title")
    if title and not pb.contains_normalized_phrase(title_norm, title):
        score += 2
        reasons.append("book title is incomplete or variant")

    feedback_score = item.get("seller_feedback_score")
    try:
        feedback = int(feedback_score) if feedback_score is not None else None
    except (TypeError, ValueError):
        feedback = None
    if feedback is not None and feedback < 100:
        score += 3
        reasons.append("low-volume seller account")

    expert_hits = sorted(signal for signal in EXPERT_SIGNALS if pb.normalize(signal) in text)
    if expert_hits:
        penalty = min(10, 2 + len(expert_hits) * 2)
        score -= penalty
        reasons.append("seller uses collector-aware language")

    return max(0, min(100, score)), reasons


def self_test() -> int:
    stats = library_stats()
    print(f"Photobook recognition library: {stats['records']} records; tiers={stats['tiers']}")
    if stats["records"] < 628:
        print("ERROR: recognition library unexpectedly smaller than Parr/Badger base")
        return 1
    example = {
        "title": "Richard Billingham Rays a Laugh old photography book",
        "context": "Used book",
        "price_gbp": 25.0,
        "price_value": 25.0,
        "price_currency": "GBP",
        "seller_account_type": "INDIVIDUAL",
        "private_seller": True,
        "buying_options": ["FIXED_PRICE"],
    }
    matches = match_listing(example)
    if not matches or pb.normalize(matches[0].get("title")) != pb.normalize("Ray's a Laugh"):
        print("ERROR: known Ray's a Laugh recognition test failed")
        return 1
    score, reasons = opportunity_score(example, matches[0])
    if score < 60:
        print(f"ERROR: opportunity scoring test unexpectedly low: {score}; {reasons}")
        return 1
    print(f"Self-test match: {matches[0]['contributor']} - {matches[0]['title']} | opportunity {score}/100")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
