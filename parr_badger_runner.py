#!/usr/bin/env python3
"""Run a monitor with Parr/Badger matching layered on top.

The existing monitor scripts stay unchanged. This wrapper:
1. loads the sharded Parr/Badger master database,
2. lets Parr/Badger matches qualify otherwise filtered charity/external listings,
3. appends exact match details to any GitHub issue body the monitor creates.

A database match is a discovery signal only. Edition, printing, completeness,
condition and market value still need verification before purchase.
"""

from __future__ import annotations

import csv
import difflib
import importlib
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

MASTER_DIR = Path("data/parr_badger_master")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "in", "on", "to", "from", "with",
    "by", "at", "or", "photographs", "photography", "photographer", "photographers",
    "photo", "book", "books", "volume", "vol", "edition", "ed", "editor", "editors",
    "various", "untitled", "selected", "works", "work",
}
CONTRIBUTOR_STOPWORDS = STOPWORDS | {
    "text", "texts", "essay", "essays", "introduction", "preface", "edited",
}


def normalize(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("’", "'")
    # Make Ray's and Rays equivalent before punctuation is removed.
    text = re.sub(r"([A-Za-z0-9])'s\b", r"\1s", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    return " ".join(TOKEN_RE.findall(text))


def useful_tokens(value: Any) -> set[str]:
    return {t for t in normalize(value).split() if len(t) >= 2 and t not in STOPWORDS}


def contributor_tokens(value: Any) -> set[str]:
    return {
        t for t in normalize(value).split()
        if len(t) >= 4 and t not in CONTRIBUTOR_STOPWORDS
    }


def contains_normalized_phrase(haystack: str, needle: str) -> bool:
    """Match complete normalized words rather than arbitrary substrings."""
    if not haystack or not needle:
        return False
    return needle == haystack or f" {needle} " in f" {haystack} "


@lru_cache(maxsize=1)
def load_master() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    if not MASTER_DIR.exists():
        return tuple()
    for path in sorted(MASTER_DIR.glob("*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                title = str(raw.get("Title") or "").strip()
                contributor = str(raw.get("Contributor") or "").strip()
                if not title:
                    continue
                title_norm = normalize(title)
                if not title_norm:
                    continue
                row = dict(raw)
                row["_title_norm"] = title_norm
                row["_title_tokens"] = useful_tokens(title)
                row["_contributor_tokens"] = contributor_tokens(contributor)
                rows.append(row)
    return tuple(rows)


def score_record(
    row: dict[str, Any], listing_title: str, listing_full: str
) -> tuple[int, str] | None:
    book_title = row["_title_norm"]
    title_tokens: set[str] = row["_title_tokens"]
    contributor_set: set[str] = row["_contributor_tokens"]
    listing_tokens = set(listing_full.split())

    contributor_hit = bool(contributor_set & listing_tokens)
    exact_in_title = contains_normalized_phrase(listing_title, book_title)
    exact_anywhere = contains_normalized_phrase(listing_full, book_title)

    short_title = len(title_tokens) <= 3 and len(book_title) < 24
    if short_title and not contributor_hit:
        return None

    coverage = 0.0
    if title_tokens:
        coverage = len(title_tokens & listing_tokens) / len(title_tokens)

    ratio = 0.0
    if listing_title:
        ratio = difflib.SequenceMatcher(None, book_title, listing_title).ratio()

    if exact_in_title:
        score = 100 if contributor_hit else 96
        reason = "exact title + contributor" if contributor_hit else "exact title"
    elif exact_anywhere:
        score = 98 if contributor_hit else 91
        reason = "exact title in listing text + contributor" if contributor_hit else "exact title in listing text"
    elif contributor_hit and ratio >= 0.86:
        score = 96
        reason = "strong fuzzy title + contributor"
    elif contributor_hit and coverage >= 0.85 and ratio >= 0.50:
        score = 92
        reason = "title token match + contributor"
    elif contributor_hit and coverage >= 0.70 and len(title_tokens) >= 3:
        score = 86
        reason = "partial title + contributor"
    elif ratio >= 0.93 and len(book_title) >= 14:
        score = 91
        reason = "very strong fuzzy title"
    elif ratio >= 0.84 and coverage >= 0.75 and len(book_title) >= 16:
        score = 85
        reason = "strong fuzzy title"
    else:
        return None

    if str(row.get("Search tier") or "").upper() == "BROAD" and score < 86:
        return None
    return score, reason


def match_listing(
    *,
    title: Any = "",
    author: Any = "",
    description: Any = "",
    publisher: Any = "",
    isbn: Any = "",
    extra_text: Any = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    listing_title = normalize(title)
    listing_full = normalize(
        " ".join(str(v or "") for v in [title, author, description, publisher, isbn, extra_text])
    )
    if not listing_full:
        return []

    matches: list[dict[str, Any]] = []
    for row in load_master():
        scored = score_record(row, listing_title, listing_full)
        if not scored:
            continue
        score, reason = scored
        matches.append({
            "score": score,
            "reason": reason,
            "volumes": row.get("Volumes") or "",
            "contributor": row.get("Contributor") or "",
            "title": row.get("Title") or "",
            "year": row.get("Year") or "",
            "publisher": row.get("Publisher") or "",
            "pb_refs": row.get("PB page / refs") or "",
            "confidence": row.get("Best confidence") or "",
            "search_tier": row.get("Search tier") or "",
        })

    matches.sort(
        key=lambda m: (int(m["score"]), m["search_tier"] == "CORE"), reverse=True
    )
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for match in matches:
        key = (normalize(match["contributor"]), normalize(match["title"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(match)
        if len(unique) >= max(1, limit):
            break
    return unique


def matches_for_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    tags = item.get("tags")
    if isinstance(tags, list):
        tags_text = " ".join(str(x) for x in tags)
    else:
        tags_text = str(tags or "")
    collections = item.get("collections")
    if isinstance(collections, list):
        collections_text = " ".join(str(x) for x in collections)
    else:
        collections_text = str(collections or "")
    return match_listing(
        title=item.get("title"),
        author=item.get("author") or item.get("vendor"),
        description=item.get("description") or item.get("context"),
        publisher=item.get("publisher"),
        isbn=item.get("isbn"),
        extra_text=f"{tags_text} {collections_text}",
    )


def attach_matches(items: list[dict[str, Any]]) -> None:
    for item in items:
        if not item.get("parr_badger_matches"):
            item["parr_badger_matches"] = matches_for_item(item)


def append_match_section(body: str, items: list[dict[str, Any]]) -> str:
    attach_matches(items)
    matched = [(item, item.get("parr_badger_matches") or []) for item in items]
    matched = [(item, matches) for item, matches in matched if matches]
    if not matched:
        return body

    lines = [
        "",
        "### Automatic Parr/Badger matches",
        "",
        "These are discovery matches against the local Parr/Badger master. Verify the exact edition and printing before purchase.",
        "",
    ]
    for item, matches in matched:
        label = item.get("title") or item.get("sku") or item.get("key") or "Listing"
        lines.append(f"- **{label}**")
        for match in matches[:3]:
            volume = str(match.get("volumes") or "?").replace(";", "/")
            tier = str(match.get("search_tier") or "").upper()
            refs = f"; {match['pb_refs']}" if match.get("pb_refs") else ""
            year = f" ({match['year']})" if match.get("year") else ""
            lines.append(
                f"  - V{volume} {tier}: {match['contributor']}, *{match['title']}*{year} | match {match['score']}/100{refs}"
            )
    return body.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"


def patch_monitor(module: Any) -> None:
    original = module.make_issue_body

    def wrapped(items: list[dict[str, Any]], detected_at: str, category_count: int | None) -> str:
        body = original(items, detected_at, category_count)
        return append_match_section(body, items)

    module.make_issue_body = wrapped


def patch_parent_monitor(module: Any) -> None:
    original = module.make_issue

    def wrapped(
        items: list[dict[str, Any]],
        detected_at: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[str, str]:
        title, body = original(items, detected_at, *args, **kwargs)
        return title, append_match_section(body, items)

    module.make_issue = wrapped


def patch_charity_monitor(module: Any) -> None:
    original_radar: Callable[[dict[str, Any]], list[str]] = module.radar_match
    original_issue = module.make_issue

    def radar(product: dict[str, Any]) -> list[str]:
        base = list(original_radar(product))
        item = {
            "title": product.get("title"),
            "description": module.strip_html(product.get("body_html")),
            "vendor": product.get("vendor"),
            "tags": product.get("tags"),
        }
        pb = matches_for_item(item)
        if pb:
            best = pb[0]
            marker = f"Parr/Badger V{best['volumes']} {best['search_tier']}: {best['title']}"
            if marker not in base:
                base.append(marker)
        return sorted(base)

    def issue(items: list[dict[str, Any]], detected_at: str) -> tuple[str, str]:
        title, body = original_issue(items, detected_at)
        return title, append_match_section(body, items)

    module.radar_match = radar
    module.make_issue = issue


def patch_external_monitor(module: Any) -> None:
    original_plausible: Callable[[dict[str, Any]], bool] = module.plausible
    original_issue = module.make_issue_body

    def plausible(item: dict[str, Any]) -> bool:
        pb = matches_for_item(item)
        if pb:
            item["parr_badger_matches"] = pb
            return True
        return original_plausible(item)

    def issue(items: list[dict[str, Any]], detected_at: str, failures: list[str]) -> str:
        body = original_issue(items, detected_at, failures)
        return append_match_section(body, items)

    module.plausible = plausible
    module.make_issue_body = issue


def run(module_name: str) -> int:
    master = load_master()
    print(f"Parr/Badger master loaded: {len(master)} records from {MASTER_DIR}")
    if not master:
        print("WARNING: Parr/Badger master is unavailable; running monitor without canon matching.", file=sys.stderr)

    module = importlib.import_module(module_name)
    if master:
        if module_name == "monitor":
            patch_monitor(module)
        elif module_name == "parent_monitor":
            patch_parent_monitor(module)
        elif module_name == "charity_monitor":
            patch_charity_monitor(module)
        elif module_name == "external_monitor":
            patch_external_monitor(module)
        else:
            raise ValueError(f"Unsupported monitor module: {module_name}")

    # Keep each existing monitor's argparse defaults intact.
    sys.argv = [f"{module_name}.py"]
    result = module.main()
    return int(result or 0)


def self_test() -> int:
    master = load_master()
    if len(master) < 600:
        print(f"ERROR: expected at least 600 master records, found {len(master)}", file=sys.stderr)
        return 1
    examples = [
        {"title": "Richard Billingham Rays a Laugh Scalo photography"},
        {"title": "Vintage Japanese photography book", "description": "Kazuo Kitai Sanrizuka 1969 1971"},
        {"title": "The Americans by Robert Frank Grove Press"},
    ]
    for example in examples:
        matches = match_listing(**example)
        print(example["title"], "=>", matches[0]["title"] if matches else "NO MATCH")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python parr_badger_runner.py <monitor|parent_monitor|charity_monitor|external_monitor|self-test>", file=sys.stderr)
        return 2
    name = sys.argv[1]
    if name == "self-test":
        return self_test()
    return run(name)


if __name__ == "__main__":
    raise SystemExit(main())
