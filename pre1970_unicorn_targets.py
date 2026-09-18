#!/usr/bin/env python3
"""Curated pre-1970 photobook unicorn targets.

This is a deliberately small, collector-led layer for books published in 1969
or earlier that are both important and plausible eBay discoveries. It is not a
price oracle. Exact edition, completeness, condition, postage and current
market value must still be freshly verified before a purchase recommendation.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "photobook_recognition" / "pre1970_unicorns.csv"
EXPECTED_TIER_COUNTS = {"A": 25, "B": 25, "C": 25}
MAX_YEAR = 1969


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


@lru_cache(maxsize=4)
def load_targets(path: Path = DEFAULT_PATH) -> tuple[dict[str, str], ...]:
    resolved = Path(path)
    rows: list[dict[str, str]] = []
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            row = {str(key): _clean(value) for key, value in raw.items() if key is not None}
            if row.get("Title"):
                rows.append(row)

    if len(rows) != 75:
        raise ValueError(f"{resolved} must contain exactly 75 pre-1970 unicorn targets")

    counts = {tier: 0 for tier in EXPECTED_TIER_COUNTS}
    identities: set[tuple[str, str]] = set()
    for row in rows:
        tier = row.get("Unicorn tier", "").upper()
        if tier not in EXPECTED_TIER_COUNTS:
            raise ValueError(f"Invalid unicorn tier {tier!r} for {row.get('Title')}")
        counts[tier] += 1
        try:
            year = int(row.get("Year") or 0)
        except ValueError as exc:
            raise ValueError(f"Invalid year for {row.get('Title')}") from exc
        if not 1800 <= year <= MAX_YEAR:
            raise ValueError(f"Target is not pre-1970: {row.get('Title')} ({year})")
        identity = (_clean(row.get("Contributor")).casefold(), _clean(row.get("Title")).casefold())
        if identity in identities:
            raise ValueError(f"Duplicate unicorn target: {row.get('Contributor')} - {row.get('Title')}")
        identities.add(identity)
        if not row.get("Radar query"):
            raise ValueError(f"Missing Radar query for {row.get('Title')}")
        if len(row["Radar query"]) > 100:
            raise ValueError(f"Radar query exceeds eBay's 100-character limit: {row['Radar query']}")

    if counts != EXPECTED_TIER_COUNTS:
        raise ValueError(f"Unicorn tiers must be exactly {EXPECTED_TIER_COUNTS}; got {counts}")
    return tuple(rows)


def targets_for_tiers(
    tiers: set[str] | tuple[str, ...] | list[str],
    path: Path = DEFAULT_PATH,
) -> list[dict[str, str]]:
    wanted = {str(tier).upper() for tier in tiers}
    return [row for row in load_targets(path) if row["Unicorn tier"].upper() in wanted]


def tier_a_targets(path: Path = DEFAULT_PATH) -> list[dict[str, str]]:
    return targets_for_tiers({"A"}, path)


def search_query(target: dict[str, Any]) -> str:
    return _clean(target.get("Radar query"))[:100]


def visible_terms(target: dict[str, Any]) -> list[str]:
    terms = [
        _clean(target.get("Title")),
        _clean(target.get("Contributor")),
    ]
    aliases = _clean(target.get("Title aliases"))
    if aliases:
        terms.extend(_clean(value) for value in aliases.split("|"))
    return list(dict.fromkeys(term for term in terms if term))
