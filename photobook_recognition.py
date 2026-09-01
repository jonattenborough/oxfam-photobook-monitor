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
COLLECTION_DISCOVERY_SIGNALS = {
    "job lot",
    "book bundle",
    "books bundle",
    "bundle of books",
    "book lot",
    "lot of books",
    "collection of books",
    "book collection",
    "books collection",
    "house clearance",
}
EXPERT_SIGNALS = {
    "first edition",
    "first printing",
    "first impression",
    "parr badger",
    "parr & badger",
    "roth 101",
    "rare",
    "scarce",
    "collectable",
    "collectible",
    "provenance",
    "dustwrapper",
    "dust jacket",
    "fine/fine",
    "near fine",
    "bibliography",
}
COLLECTIBLE_FORMAT_RULES = (
    (
        "book or edition with an original print",
        12,
        {
            "book and print",
            "book with a print",
            "book with print",
            "c type print",
            "c-type print",
            "gelatin silver print",
            "includes a print",
            "including a print",
            "original photograph",
            "original print",
            "pigment print",
            "print edition",
            "silver gelatin print",
            "silver print",
            "with an archival print",
            "with a print",
            "with original print",
            "with print",
        },
    ),
    (
        "unique work or artist proof",
        9,
        {
            "a/p copy",
            "artist proof",
            "artist's proof",
            "hand drawn",
            "hors commerce",
            "monoprint",
            "original artwork",
            "original drawing",
            "unique print",
        },
    ),
    ("association or presentation copy", 9, {"association copy", "presentation copy"}),
    ("signed by the photographer", 6, {"hand signed", "signed", "signed by", "signed copy"}),
    ("numbered copy", 5, {"hand numbered", "numbered", "numbered copy"}),
    ("limited or special edition", 4, {"deluxe edition", "edition of", "limited edition", "special edition"}),
    ("collector housing", 3, {"clamshell", "presentation box", "slipcase", "slipcased", "solander"}),
)
EDITION_IDENTITY_FIELDS = {
    "Year",
    "Publisher",
    "ISBN",
    "First edition notes",
}
PIPE_MERGE_FIELDS = {
    "Awards and evidence",
    "Canon sources",
    "Collectible variants",
    "Collector profile",
    "Contributor aliases",
    "Title aliases",
}
TIER_BONUS = {"S": 25, "A": 20, "B": 14, "C": 8, "D": 3}
LISTING_NOISE_TOKENS = {
    "1st",
    "2nd",
    "3rd",
    "anniversary",
    "book",
    "books",
    "copy",
    "collectable",
    "collectible",
    "condition",
    "damaged",
    "dj",
    "dust",
    "ed",
    "edition",
    "first",
    "fine",
    "format",
    "hardback",
    "hardcover",
    "hb",
    "monograph",
    "new",
    "old",
    "original",
    "paperback",
    "pb",
    "photo",
    "photobook",
    "photographs",
    "photography",
    "printing",
    "rare",
    "revised",
    "second",
    "signed",
    "softback",
    "softcover",
    "trade",
    "used",
    "vg",
    "vintage",
    "wrapper",
}
PUBLISHER_NOISE_TOKENS = {
    "and",
    "book",
    "books",
    "edition",
    "editions",
    "foundation",
    "inc",
    "ltd",
    "press",
    "publisher",
    "publishers",
    "publishing",
}
REISSUE_SIGNALS = {
    "anniversary edition",
    "facsimile edition",
    "new edition",
    "reissue",
    "reprint",
    "revised edition",
    "second edition",
    "second printing",
    "third edition",
    "third printing",
    "later printing",
}
FIRST_EDITION_SIGNALS = {
    "first edition",
    "first impression",
    "first printing",
    "1st edition",
    "1st impression",
    "1st printing",
}
YEAR_RE = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2})(?!\d)")
LIMITATION_RE = re.compile(
    r"(?i)\b(?:no\.?|number)?\s*\d{1,4}\s*(?:/|of)\s*\d{1,5}\b"
)
PROOF_MARK_RE = re.compile(r"(?i)(?<![a-z])(?:a\s*/\s*p|h\s*/\s*c)(?![a-z])")
MULTIPLE_BOOKS_RE = re.compile(r"(?i)\b(?:[2-9]|[1-9]\d{1,2})\s+(?:photography\s+|photo\s+|art\s+)?books\b")


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


def _contributor_identity(value: Any) -> str:
    """Return a name-order-insensitive identity for de-duplication.

    The canon uses forms such as ``Klein, William`` while supplements tend to
    use ``William Klein``. Sorting normalized name tokens merges those records
    without making ordinary title matching less strict.
    """
    tokens = {
        token
        for token in pb.normalize(value).split()
        if len(token) >= 2 and token not in {"and", "by", "et", "al"}
    }
    return " ".join(sorted(tokens))


def _record_key(contributor: Any, title: Any) -> tuple[str, str]:
    return _contributor_identity(contributor), pb.normalize(title)


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
    index_tokens = set(row["_title_tokens"]) | contributor_tokens
    for alias in title_aliases:
        index_tokens.update(pb.useful_tokens(alias))
    row["_index_tokens"] = index_tokens
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


def _record_rank(row: dict[str, Any]) -> int:
    record_id = _clean(row.get("Record ID")).lower()
    source = f"{_clean(row.get('Canon sources'))} {_clean(row.get('Source'))}".lower()
    if (
        "priority seed" in source
        or "emerging watch" in source
        or "curated contemporary documentary" in source
    ):
        return 3
    if record_id.startswith("canon:") or "parr/badger" in source or "roth 101" in source:
        return 2
    if record_id.startswith("openlibrary:"):
        return 1
    return 2


def _best_tier(first: Any, second: Any) -> str:
    order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    values = [value for value in (_clean(first).upper(), _clean(second).upper()) if value]
    return min(values, key=lambda value: order.get(value, 99)) if values else ""


def _best_priority(first: Any, second: Any) -> str:
    values = [value for value in (_clean(first), _clean(second)) if value.isdigit()]
    return str(min(int(value) for value in values)) if values else (_clean(first) or _clean(second))


def _merge_pipe_values(first: Any, second: Any) -> str:
    values = [part.strip() for part in f"{_clean(first)} | {_clean(second)}".split("|") if part.strip()]
    return " | ".join(dict.fromkeys(values))


def _merge_record(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    base_rank = _record_rank(base)
    extra_rank = _record_rank(extra)
    for key, value in extra.items():
        if key.startswith("_"):
            continue
        if not _clean(value):
            continue
        if key in PIPE_MERGE_FIELDS:
            merged[key] = _merge_pipe_values(merged.get(key), value)
        elif key == "Collectibility tier":
            merged[key] = _best_tier(merged.get(key), value)
        elif key == "Search priority":
            merged[key] = _best_priority(merged.get(key), value)
        elif key == "Search tier":
            tiers = {_clean(merged.get(key)).upper(), _clean(value).upper()}
            merged[key] = "CORE" if "CORE" in tiers else _clean(value).upper()
        elif key == "Evidence confidence":
            confidence = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "": 0}
            if confidence.get(_clean(value).upper(), 0) > confidence.get(_clean(merged.get(key)).upper(), 0):
                merged[key] = value
        elif key in EDITION_IDENTITY_FIELDS:
            # A publisher backlist often describes a current reissue rather
            # than the original collectible edition. It may add aliases and
            # provenance to a canonical work, but it cannot fill or replace
            # edition-identifying metadata from a higher-authority record.
            if extra_rank >= base_rank:
                merged[key] = value
        elif not _clean(merged.get(key)) or extra_rank >= base_rank:
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


@lru_cache(maxsize=1)
def _library_token_index() -> dict[str, tuple[int, ...]]:
    index: dict[str, list[int]] = {}
    for position, row in enumerate(load_library()):
        for token in row.get("_index_tokens") or set():
            index.setdefault(str(token), []).append(position)
    return {token: tuple(positions) for token, positions in index.items()}


def _candidate_records(listing_full: str) -> list[dict[str, Any]]:
    rows = load_library()
    positions: set[int] = set()
    index = _library_token_index()
    for token in set(listing_full.split()):
        positions.update(index.get(token, ()))
    return [rows[position] for position in sorted(positions)]


def _raw_name_tokens(value: Any) -> set[str]:
    return {
        token
        for token in pb.normalize(value).split()
        if len(token) >= 2 and token not in {"and", "by", "et", "al"}
    }


def _is_eponymous(row: dict[str, Any], title_value: Any) -> bool:
    title_tokens = set(pb.normalize(title_value).split())
    contributor_tokens = _raw_name_tokens(row.get("Contributor"))
    publisher_tokens = _publisher_tokens(row.get("Publisher"))
    generic_tokens = LISTING_NOISE_TOKENS | {
        "a",
        "an",
        "and",
        "by",
        "of",
        "the",
    }
    core_title_tokens = title_tokens - publisher_tokens - generic_tokens
    if not core_title_tokens:
        core_title_tokens = title_tokens - generic_tokens
    return bool(
        core_title_tokens
        and contributor_tokens
        and (
            core_title_tokens <= contributor_tokens
            or contributor_tokens <= core_title_tokens
        )
    )


def _short_title_guard(
    row: dict[str, Any],
    matched_title: str,
    listing_title: str,
    reason: str,
) -> bool:
    """Reject a short title merely embedded inside a different book title.

    The first production run matched every Diane Arbus biography containing
    her name to the eponymous 1972 monograph. Short and eponymous records now
    require the rest of the listing title to be ordinary sale metadata or
    metadata expected for that exact record.
    """
    title_tokens = set(matched_title.split())
    eponymous = _is_eponymous(row, matched_title)
    short = len(pb.useful_tokens(matched_title)) <= 2 or len(matched_title) < 14
    if not eponymous and not short:
        return True
    if not reason.startswith("exact"):
        return False

    listing_tokens = set(listing_title.split())
    contributor_tokens = _raw_name_tokens(row.get("Contributor"))
    if short and not eponymous:
        # A one- or two-word title such as ``Untitled`` is useful only when the
        # complete named contributor also appears in the listing title. This
        # still identifies the actual work while avoiding generic-word hits.
        return bool(contributor_tokens and contributor_tokens <= listing_tokens)

    expected_tokens = {
        token
        for token in pb.normalize(row.get("Publisher")).split()
        if len(token) >= 3 and token not in PUBLISHER_NOISE_TOKENS
    }
    expected_tokens.update(YEAR_RE.findall(_clean(row.get("Year"))))
    extras = {
        token
        for token in listing_title.split()
        if len(token) >= 2
        and token not in title_tokens
        and token not in contributor_tokens
        and token not in LISTING_NOISE_TOKENS
        and token not in {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with"}
        and not YEAR_RE.fullmatch(token)
    }
    return not (extras - expected_tokens)


def _score_alias(row: dict[str, Any], alias: str, listing_title: str, listing_full: str) -> tuple[int, str] | None:
    # A contributor-only alias such as "Diane Arbus" cannot safely identify
    # a longer titled monograph. It otherwise matches biographies and every
    # other book about the same photographer.
    alias_name_tokens = _raw_name_tokens(alias)
    contributor_tokens = _raw_name_tokens(row.get("Contributor"))
    if (
        alias_name_tokens
        and alias_name_tokens <= contributor_tokens
        and pb.normalize(alias) != pb.normalize(row.get("Title"))
    ):
        return None
    alias_row = dict(row)
    alias_row["_title_norm"] = pb.normalize(alias)
    alias_row["_title_tokens"] = pb.useful_tokens(alias)
    scored = pb.score_record(alias_row, listing_title, listing_full)
    if scored is None:
        return None
    score, reason = scored
    if not _short_title_guard(row, alias_row["_title_norm"], listing_title, reason):
        return None
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
                _clean(item.get("author")),
                _clean(item.get("vendor")),
                _clean(item.get("publisher")),
                _clean(item.get("publication_year")),
                _clean(item.get("edition")),
                _clean(item.get("isbn")),
                tags_text,
            ]
        )
    )
    if not listing_full:
        return []

    matches: list[dict[str, Any]] = []
    for row in _candidate_records(listing_full):
        scored = pb.score_record(row, listing_title, listing_full)
        if scored is not None and not _short_title_guard(
            row,
            str(row.get("_title_norm") or ""),
            listing_title,
            scored[1],
        ):
            scored = None
        for alias in row.get("_title_aliases") or []:
            alias_scored = _score_alias(row, alias, listing_title, listing_full)
            if alias_scored is not None and (scored is None or alias_scored[0] > scored[0]):
                scored = alias_scored
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
                "collector_profile": _clean(row.get("Collector profile")),
                "documentary_relevance": _clean(row.get("Documentary relevance")).upper(),
                "first_monograph": _clean(row.get("First monograph")).upper(),
                "collectible_variants": _clean(row.get("Collectible variants")),
                "special_edition_priority": _clean(row.get("Special edition priority")).upper(),
                "awards_and_evidence": _clean(row.get("Awards and evidence")),
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
                _clean(item.get("author")),
                _clean(item.get("publisher")),
                _clean(item.get("publication_year")),
                _clean(item.get("edition")),
                _clean(item.get("isbn")),
                _clean(item.get("condition")),
                _clean(item.get("condition_description")),
                _clean(item.get("category_path")),
                _clean(item.get("tags")),
            ]
        )
    )


def collection_bundle_evidence(item: dict[str, Any]) -> bool:
    """Return true only when the listing describes multiple physical books.

    A bare word such as "collection" is common in individual book titles and
    is not evidence of a job lot. This narrower test prevents those titles
    from receiving the high-recall bundle boost.
    """
    text = _listing_text(item)
    if any(
        pb.contains_normalized_phrase(text, pb.normalize(signal))
        for signal in COLLECTION_DISCOVERY_SIGNALS
    ):
        return True
    raw_text = " ".join(
        _clean(item.get(key))
        for key in ("title", "context", "description", "condition_description")
    )
    return bool(MULTIPLE_BOOKS_RE.search(raw_text))


def collectible_format_evidence(
    item: dict[str, Any],
    match: dict[str, Any],
) -> tuple[int, list[str], list[str]]:
    """Score physical-object features separately from seller sophistication.

    A knowledgeable seller may describe a signed or numbered edition
    accurately, but that language is also evidence that the object itself is
    more collectible. Keeping these two judgements separate prevents the
    monitor from penalising exactly the formats it is meant to find.
    """
    text = _listing_text(item)
    raw_text = " ".join(
        _clean(item.get(key))
        for key in (
            "title",
            "context",
            "description",
            "edition",
            "condition_description",
        )
    )
    labels: list[str] = []
    score = 0
    for label, bonus, signals in COLLECTIBLE_FORMAT_RULES:
        if any(
            pb.contains_normalized_phrase(text, pb.normalize(signal))
            for signal in signals
        ):
            labels.append(label)
            score += bonus

    if LIMITATION_RE.search(raw_text) and "numbered copy" not in labels:
        labels.append("numbered copy")
        score += 5
    if PROOF_MARK_RE.search(raw_text) and "unique work or artist proof" not in labels:
        labels.append("unique work or artist proof")
        score += 9

    reasons: list[str] = []
    if labels:
        score = min(18, score)
        reasons.append("collectible object: " + ", ".join(labels))

    known_variants = _clean(match.get("collectible_variants"))
    priority = _clean(match.get("special_edition_priority")).upper()
    if labels and known_variants:
        score += 3 if priority == "HIGH" else 2
        reasons.append("listing resembles a known collectible variant")

    first_monograph = _clean(match.get("first_monograph")).upper() == "YES"
    first_claim = any(
        pb.contains_normalized_phrase(text, pb.normalize(signal))
        for signal in FIRST_EDITION_SIGNALS
    )
    if first_monograph and first_claim:
        score += 4
        reasons.append("first-edition claim for a first monograph")

    return min(22, score), reasons, labels


def _isbn_key(value: Any) -> str:
    return "".join(character for character in _clean(value).upper() if character.isdigit() or character == "X")


def _isbn_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    for part in re.split(r"\s*[|;,\n]\s*", _clean(value)):
        key = _isbn_key(part)
        if len(key) in {10, 13}:
            keys.add(key)
    return keys


def _expected_year(value: Any) -> str:
    match = YEAR_RE.search(_clean(value))
    return match.group(1) if match else ""


def _publisher_tokens(value: Any) -> set[str]:
    return {
        token
        for token in pb.normalize(value).split()
        if len(token) >= 3 and token not in PUBLISHER_NOISE_TOKENS
    }


def assess_edition(item: dict[str, Any], match: dict[str, Any]) -> tuple[str, list[str]]:
    """Assess whether the listing plausibly represents the target edition.

    This deliberately separates a valuable *work* match from evidence for the
    collectible edition. Unknown metadata remains reviewable, but a known
    reprint or conflicting bibliographic field cannot become an urgent alert.
    """
    expected_year = _expected_year(match.get("year"))
    expected_publisher = _publisher_tokens(match.get("publisher"))
    expected_isbns = _isbn_keys(match.get("isbn"))

    listing_publisher = _publisher_tokens(item.get("publisher"))
    listing_isbns = _isbn_keys(item.get("isbn"))
    explicit_years = set(YEAR_RE.findall(_clean(item.get("publication_year"))))
    title_years = set(
        YEAR_RE.findall(
            " ".join([_clean(item.get("title")), _clean(item.get("description")), _clean(item.get("edition"))])
        )
    )
    bibliographic_text = pb.normalize(
        " ".join(
            [
                _clean(item.get("title")),
                _clean(item.get("description")),
                _clean(item.get("publisher")),
                _clean(item.get("publication_year")),
                _clean(item.get("edition")),
                _clean(item.get("isbn")),
            ]
        )
    )

    evidence: list[str] = []
    conflicts: list[str] = []
    if expected_isbns and listing_isbns:
        if expected_isbns & listing_isbns:
            evidence.append("target ISBN matches")
        else:
            conflicts.append("listing ISBN differs from target")
    if expected_year:
        if expected_year in explicit_years or expected_year in title_years:
            evidence.append(f"target year {expected_year} appears")
        elif explicit_years:
            conflicts.append(
                f"listed publication year {', '.join(sorted(explicit_years))} differs from target {expected_year}"
            )
    if expected_publisher and listing_publisher:
        if expected_publisher & listing_publisher:
            evidence.append("target publisher matches")
        else:
            conflicts.append("listed publisher differs from target")

    reissue_hits = sorted(
        signal
        for signal in REISSUE_SIGNALS
        if pb.contains_normalized_phrase(bibliographic_text, pb.normalize(signal))
    )
    if reissue_hits and not (expected_isbns & listing_isbns):
        conflicts.append("listing explicitly describes a reissue or later edition")

    if conflicts:
        return "mismatch", conflicts
    if any(reason == "target ISBN matches" for reason in evidence):
        return "confirmed", evidence

    first_claims = sorted(
        signal
        for signal in FIRST_EDITION_SIGNALS
        if pb.contains_normalized_phrase(bibliographic_text, pb.normalize(signal))
    )
    if first_claims:
        return "claimed", evidence + ["seller claims a first edition but target metadata is unconfirmed"]
    if evidence:
        return "plausible", evidence
    return "unknown", ["exact collectible edition is not established by the listing metadata"]


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

    documentary_relevance = _clean(match.get("documentary_relevance")).upper()
    if documentary_relevance == "HIGH":
        score += 3
        reasons.append("high fit for documentary collecting profile")
    elif documentary_relevance == "MEDIUM":
        score += 1
        reasons.append("documentary-adjacent collector fit")
    if _clean(match.get("first_monograph")).upper() == "YES":
        reasons.append("recognised first monograph")

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
    format_bonus, format_reasons, format_labels = collectible_format_evidence(item, match)
    if format_bonus:
        score += format_bonus
        reasons.extend(format_reasons)
    match["collectible_format_evidence"] = format_labels
    match["collectible_format_bonus"] = format_bonus

    casual = sorted(
        signal
        for signal in CASUAL_SIGNALS
        if pb.contains_normalized_phrase(text, pb.normalize(signal))
    )
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

    expert_hits = sorted(
        signal
        for signal in EXPERT_SIGNALS
        if pb.contains_normalized_phrase(text, pb.normalize(signal))
    )
    if expert_hits:
        penalty = min(10, 2 + len(expert_hits) * 2)
        score -= penalty
        reasons.append("seller uses collector-aware language")

    edition_status, edition_reasons = assess_edition(item, match)
    match["edition_status"] = edition_status
    match["edition_reasons"] = edition_reasons
    reasons.append(f"edition evidence: {edition_status}")
    priority_text = str(match.get("search_priority") or "9")
    sensitive = tier == "S" or (tier == "A" and priority_text in {"0", "1", "2"})
    if edition_status == "mismatch":
        score -= 28 if sensitive else 10
        score = min(score, 62 if sensitive else 76)
    elif sensitive and edition_status == "plausible":
        score = min(score, 89)
    elif sensitive and edition_status == "unknown":
        score = min(score, 86)
    elif sensitive and edition_status == "claimed":
        score = min(score, 88)

    # Open Library publisher-backlist records broaden recognition, but they do
    # not by themselves establish collectibility or a valuable edition. Keep a
    # routine single-book listing below the alert threshold unless the listing
    # also carries a genuine seller-ignorance or collection-discovery signal.
    record_id = str(match.get("record_id") or "").lower()
    backlist_only = record_id.startswith("openlibrary:")
    discovery_lane = str(item.get("search_lane") or "")
    discovery_lanes = set(discovery_lane.split("+"))
    high_recall_context = bool(casual) or bool(discovery_lanes & {"wrong_category"})
    if "collection" in discovery_lanes and collection_bundle_evidence(item):
        high_recall_context = True
    if backlist_only and tier in {"C", "D"} and bargain is None and strong_buy is None and not high_recall_context:
        score = min(score, 70)
        reasons.append("publisher-backlist record lacks independent value evidence")

    # A respected recent book is not automatically a bargain. Plain copies
    # above the user's normal discovery range stay below the issue threshold
    # unless there is a curated price benchmark, a collectible-format signal,
    # or genuine high-recall seller context.
    curated_contemporary = (
        record_id.startswith("contemporary:")
        or "curated contemporary documentary" in _clean(match.get("canon_sources")).lower()
    )
    contemporary_market_signal = bool(
        format_bonus
        or casual
        or bargain is not None
        or strong_buy is not None
        or (price_gbp is not None and price_gbp <= 150)
        or discovery_lanes & {"wrong_category"}
        or ("collection" in discovery_lanes and collection_bundle_evidence(item))
    )
    if curated_contemporary and not contemporary_market_signal:
        score = min(score, 70)
        reasons.append("respected recent title lacks a current bargain or special-edition signal")

    return max(0, min(100, score)), reasons


def self_test() -> int:
    stats = library_stats()
    print(f"Photobook recognition library: {stats['records']} records; tiers={stats['tiers']}")
    if stats["records"] < 2000:
        print("ERROR: recognition library fell below the 2,000-record operating floor")
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
