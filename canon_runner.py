#!/usr/bin/env python3
"""Run the live listing monitors against the combined photobook canon.

The Parr/Badger 628-record directory remains unchanged because it is also the
fixed input to the one-time Parr/Badger baseline sweep. This wrapper overlays
Andrew Roth's The Book of 101 Books for live discovery without changing that
in-progress sweep target set.
"""
from __future__ import annotations

import csv
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import parr_badger_runner as pb

ROTH_PATH = Path("data/roth_101_master.csv")
_ORIGINAL_PB_LOAD = pb.load_master

# Roth titles whose wording differs from the Parr/Badger operational record.
PB_TITLE_ALIASES = {
    pb.normalize("The North American Indian, Volume One"): "The North American Indian",
    pb.normalize("Camera Work, Number XXXVI"): "Camera Work",
    pb.normalize("Köpfe des Alltags"): "Köpfe des Alltags: unbekannte Menschen",
    pb.normalize("Berühmte Zeitgenossen"): "Berühmte Zeitgenossen in unbewachten Augenblicken",
    pb.normalize("Industriia sotsializma"): "Industriya Sotzializma",
    pb.normalize("Sentimental Journey"): "Senchimentaru na Tabi",
    pb.normalize("Killed By Roses"): "Barakei",
    pb.normalize("The Map"): "Chizu",
    pb.normalize("Bye, Bye Photography, Dear"): "Shashin yo Sayonara",
    pb.normalize("Telex Iran"): "Telex Persan",
    pb.normalize("Fauna"): "Dr Ameisenhaufen's Fauna",
}


def _prepare(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("Title") or "").strip()
    contributor = str(row.get("Contributor") or "").strip()
    row["_title_norm"] = pb.normalize(title)
    row["_title_tokens"] = pb.useful_tokens(title)
    row["_contributor_tokens"] = pb.contributor_tokens(contributor)
    return row


@lru_cache(maxsize=1)
def load_roth() -> tuple[dict[str, str], ...]:
    if not ROTH_PATH.exists():
        return tuple()
    with ROTH_PATH.open("r", encoding="utf-8-sig", newline="") as fh:
        return tuple(dict(row) for row in csv.DictReader(fh) if str(row.get("Title") or "").strip())


def _matching_pb_rows(rows: list[dict[str, Any]], roth: dict[str, str]) -> list[dict[str, Any]]:
    title_norm = pb.normalize(roth.get("Title"))
    target_title = PB_TITLE_ALIASES.get(title_norm, roth.get("Title") or "")
    target_norm = pb.normalize(target_title)
    candidates = [row for row in rows if row.get("_title_norm") == target_norm]
    if not candidates:
        return []
    # Generic titles such as London need contributor confirmation.
    if len(candidates) > 1 or len(target_norm.split()) <= 2:
        rt = pb.contributor_tokens(roth.get("Contributor"))
        narrowed = [row for row in candidates if rt & set(row.get("_contributor_tokens") or set())]
        if narrowed:
            return narrowed
    return candidates if len(candidates) == 1 else []


@lru_cache(maxsize=1)
def load_canon_master() -> tuple[dict[str, Any], ...]:
    rows = [dict(row) for row in _ORIGINAL_PB_LOAD()]
    # The original loader already prepared these hidden matching fields.
    for roth in load_roth():
        overlaps = _matching_pb_rows(rows, roth) if str(roth.get("Parr/Badger overlap") or "").lower() == "yes" else []
        roth_ref = f"Roth 101 p.{roth.get('Roth page')}" if roth.get("Roth page") else "Roth 101"
        if overlaps:
            for row in overlaps:
                refs = str(row.get("PB page / refs") or "").strip()
                if roth_ref not in refs:
                    row["PB page / refs"] = f"{refs} | {roth_ref}".strip(" |")
                row["Search tier"] = "CORE"
                row["Roth 101"] = "Yes"
                row["Roth page"] = roth.get("Roth page") or ""
            continue

        # Roth-only titles become first-class CORE discovery records.
        rows.append(_prepare({
            "Volumes": "R101",
            "Contributor": roth.get("Contributor") or "",
            "Title": roth.get("Title") or "",
            "Year": roth.get("Roth year") or "",
            "Publisher": roth.get("Publication statement") or "",
            "PB page / refs": roth_ref,
            "Best confidence": "Very high",
            "Search tier": "CORE",
            "Roth 101": "Yes",
            "Roth page": roth.get("Roth page") or "",
        }))
    return tuple(rows)


def canon_append_match_section(body: str, items: list[dict[str, Any]]) -> str:
    pb.attach_matches(items)
    matched = [(item, item.get("parr_badger_matches") or []) for item in items]
    matched = [(item, matches) for item, matches in matched if matches]
    if not matched:
        return body
    lines = [
        "",
        "### Automatic photobook-canon matches",
        "",
        "Discovery matches against Parr/Badger plus Andrew Roth's The Book of 101 Books. Verify the exact edition and printing before purchase.",
        "",
    ]
    for item, matches in matched:
        label = item.get("title") or item.get("sku") or item.get("key") or "Listing"
        lines.append(f"- **{label}**")
        for match in matches[:3]:
            volumes = str(match.get("volumes") or "")
            if volumes == "R101":
                canon_label = "Roth 101"
            else:
                canon_label = f"Parr/Badger V{volumes.replace(';', '/')}" if volumes else "Photobook canon"
                if "Roth 101" in str(match.get("pb_refs") or ""):
                    canon_label += " + Roth 101"
            refs = f"; {match['pb_refs']}" if match.get("pb_refs") else ""
            year = f" ({match['year']})" if match.get("year") else ""
            tier = str(match.get("search_tier") or "").upper()
            lines.append(
                f"  - {canon_label} {tier}: {match['contributor']}, *{match['title']}*{year} | match {match['score']}/100{refs}"
            )
    return body.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"


def canon_patch_charity_monitor(module: Any) -> None:
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
        matches = pb.matches_for_item(item)
        if matches:
            best = matches[0]
            volumes = str(best.get("volumes") or "")
            label = "Roth 101" if volumes == "R101" else f"Parr/Badger V{volumes}"
            if "Roth 101" in str(best.get("pb_refs") or "") and volumes != "R101":
                label += " + Roth 101"
            marker = f"{label} {best['search_tier']}: {best['title']}"
            if marker not in base:
                base.append(marker)
        return sorted(base)

    def issue(items: list[dict[str, Any]], detected_at: str) -> tuple[str, str]:
        title, body = original_issue(items, detected_at)
        return title, canon_append_match_section(body, items)

    module.radar_match = radar
    module.make_issue = issue


# Patch the base module at runtime. Keeping pb.load_master unchanged on disk means
# scripts that deliberately need the fixed 628 Parr/Badger set can continue to use it.
pb.load_master = load_canon_master
pb.append_match_section = canon_append_match_section
pb.patch_charity_monitor = canon_patch_charity_monitor


def main() -> int:
    canon = load_canon_master()
    roth = load_roth()
    print(f"Combined photobook canon loaded: {len(canon)} operational records; {len(roth)} Roth 101 records")
    return pb.main()


if __name__ == "__main__":
    raise SystemExit(main())
