#!/usr/bin/env python3
"""Shared 175-photographer targeting for every eBay monitor.

The Endgame JSON is the single source of truth.  Search planners use its tiered
names and aliases, while seller monitors use the same data for zero-call local
recognition of titles and summary metadata.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_TARGETS_PATH = Path(__file__).resolve().parent / "data" / "ebay_endgame_targets.json"
EXPECTED_TIER_COUNTS = {"1": 50, "2": 70, "3": 55}
ITEM_TEXT_FIELDS = (
    "title",
    "context",
    "description",
    "condition_description",
    "author",
    "publisher",
    "edition",
    "vendor",
)


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


@lru_cache(maxsize=8)
def load_targets(path: Path = DEFAULT_TARGETS_PATH) -> dict[str, Any]:
    resolved = Path(path)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tiers"), dict):
        raise ValueError(f"{resolved} must contain a tiered target object")

    all_names: list[str] = []
    for tier, expected in EXPECTED_TIER_COUNTS.items():
        settings = payload["tiers"].get(tier)
        names = settings.get("names") if isinstance(settings, dict) else None
        if not isinstance(names, list) or len(names) != expected:
            raise ValueError(f"Core photographer tier {tier} must contain exactly {expected} names")
        all_names.extend(str(name).strip() for name in names)
    identities = [normalized(name) for name in all_names]
    if len(all_names) != 175 or len(set(identities)) != 175:
        raise ValueError("Core photographer target file must contain 175 unique names")
    return payload


def target_records(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = config or load_targets()
    aliases = source.get("aliases") if isinstance(source.get("aliases"), dict) else {}
    records: list[dict[str, Any]] = []
    for tier in ("1", "2", "3"):
        for raw_name in source["tiers"][tier]["names"]:
            name = str(raw_name).strip()
            extras = aliases.get(name) if isinstance(aliases, dict) else None
            terms = [name]
            if isinstance(extras, list):
                terms.extend(str(value).strip() for value in extras if str(value).strip())
            records.append({"name": name, "tier": tier, "terms": terms})
    return records


def photographer_terms(config: dict[str, Any], tier: str) -> list[str]:
    return [
        term
        for record in target_records(config)
        if record["tier"] == str(tier)
        for term in record["terms"]
    ]


def compile_or_queries(terms: list[str], character_limit: int) -> list[dict[str, Any]]:
    """Pack terms into eBay's parenthesised comma-separated OR syntax."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        term = " ".join(re.sub(r"[(),]+", " ", str(raw or "")).split()).strip()
        identity = normalized(term)
        if not term or not identity or identity in seen:
            continue
        if len(term) + 2 > character_limit:
            raise ValueError(f"Core photographer search term is too long: {term}")
        seen.add(identity)
        cleaned.append(term)

    groups: list[dict[str, Any]] = []
    current: list[str] = []
    for term in cleaned:
        proposed = current + [term]
        query = f"({','.join(proposed)})"
        if current and len(query) > character_limit:
            groups.append({"query": f"({','.join(current)})", "terms": current})
            current = [term]
        else:
            current = proposed
    if current:
        groups.append({"query": f"({','.join(current)})", "terms": current})
    return groups


def tier_query_groups(
    config: dict[str, Any] | None = None,
    *,
    character_limit: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    source = config or load_targets()
    limit = int(character_limit or source.get("query_character_limit") or 90)
    return {
        tier: compile_or_queries(photographer_terms(source, tier), limit)
        for tier in ("1", "2", "3")
    }


def _item_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ITEM_TEXT_FIELDS:
        value = item.get(field)
        if isinstance(value, list):
            parts.extend(str(entry) for entry in value)
        else:
            parts.append(str(value or ""))
    tags = item.get("tags")
    if isinstance(tags, list):
        parts.extend(str(value) for value in tags)
    elif tags:
        parts.append(str(tags))
    return normalized(" ".join(parts))


def matches_for_item(
    item: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return exact visible core-name or alias matches from local item data."""
    text = _item_text(item)
    if not text:
        return []
    padded = f" {text} "
    matches: list[dict[str, str]] = []
    for record in target_records(config):
        for term in record["terms"]:
            identity = normalized(term)
            if identity and f" {identity} " in padded:
                matches.append(
                    {
                        "name": str(record["name"]),
                        "tier": str(record["tier"]),
                        "matched_term": str(term),
                    }
                )
                break
    matches.sort(key=lambda value: (int(value["tier"]), normalized(value["name"])))
    return matches
