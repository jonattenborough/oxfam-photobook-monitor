#!/usr/bin/env python3
"""Monitor newest listings across Oxfam's broad Art & Photography parent category."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from oxfam_parent_common import (
    PHOTOGRAPHY_DIMENSION_ID,
    absolute_product_url,
    collect_metadata,
    discover_parent_dimension_id,
    fetch_search,
    item_from_meta,
    ordered_skus,
    require_newest_first,
    utc_now,
)

PAGE_SIZE = 90
PAGES = 2  # newest 180 parent-category items per run


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def set_output(name: str, value: str) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def child_photography_seen() -> set[str]:
    """Avoid duplicate alerts for items already handled by the dedicated Photography monitor."""
    seen: set[str] = set()
    for path in [Path("data/state.json"), Path("runtime/proposed-state.json")]:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        products = data.get("products") if isinstance(data, dict) else None
        if isinstance(products, dict):
            seen.update(str(k) for k in products)

    # Also query the live Photography child category. This closes the small gap
    # between the dedicated monitor's ten-minute runs, when a brand-new child
    # listing could otherwise be reported first by this broader parent monitor.
    for page in range(PAGES):
        payload = fetch_search(PHOTOGRAPHY_DIMENSION_ID, page * PAGE_SIZE, PAGE_SIZE)
        require_newest_first(payload)
        skus, _ = ordered_skus(payload)
        seen.update(skus)
    return seen


def make_issue(items: list[dict[str, Any]], detected_at: str) -> tuple[str, str]:
    title = f"OXFAM_ART_NEW: {len(items)} broad Art & Photography listings"
    lines = [
        "## New Oxfam Art & Photography parent-category listings",
        "",
        f"Detected at **{detected_at}**.",
        "These are outside the dedicated Photography monitor's already-seen SKU set.",
        "This intentionally broad feed exists to catch photobooks miscategorised elsewhere in Art & Photography.",
        "",
        "Review every item as a possible photography/artist book before deciding it is irrelevant.",
        "",
    ]
    for item in items:
        name = item.get("title") or item["sku"]
        lines += [f"### {name}", "", f"- **SKU:** `{item['sku']}`"]
        if isinstance(item.get("price_gbp"), (int, float)):
            lines.append(f"- **Oxfam price:** £{item['price_gbp']:.2f}")
        if item.get("author"):
            lines.append(f"- **Author/photographer:** {item['author']}")
        if item.get("publisher"):
            lines.append(f"- **Publisher:** {item['publisher']}")
        if item.get("condition"):
            lines.append(f"- **Condition:** {item['condition']}")
        lines.append(f"- **Product URL:** {absolute_product_url(item)}")
        if item.get("description"):
            desc = re.sub(r"\s+", " ", str(item["description"])).strip()
            lines.append(f"- **Description:** {desc[:1200]}")
        lines.append("")
    return title, "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/parent_state.json")
    parser.add_argument("--runtime-dir", default="runtime/parent_monitor")
    args = parser.parse_args()

    state_path = Path(args.state)
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)

    state = load_json(state_path, {"version": 1, "products": {}})
    old_products = state.get("products") if isinstance(state, dict) else {}
    if not isinstance(old_products, dict):
        old_products = {}
    first_run = not bool(old_products)

    dimension_id, repository_id = discover_parent_dimension_id()
    current: dict[str, dict[str, Any]] = {}
    for page in range(PAGES):
        payload = fetch_search(dimension_id, page * PAGE_SIZE, PAGE_SIZE)
        require_newest_first(payload)
        skus, product_ids = ordered_skus(payload)
        metadata = collect_metadata(payload, set(skus))
        for sku in skus:
            current.setdefault(sku, item_from_meta(sku, product_ids.get(sku), metadata.get(sku, {})))

    if not current:
        raise RuntimeError("Parent monitor parsed zero products")

    detected_at = utc_now()
    child_seen = child_photography_seen()

    new_items = [
        item for sku, item in current.items()
        if sku not in old_products and sku not in child_seen
    ]

    proposed_products = dict(old_products)
    for sku, item in current.items():
        existing = old_products.get(sku, {}) if isinstance(old_products.get(sku), dict) else {}
        proposed_products[sku] = {
            "first_seen": existing.get("first_seen", detected_at),
            "title": item.get("title"),
            "price_gbp": item.get("price_gbp"),
            "url": absolute_product_url(item),
        }

    proposed = {
        "version": 1,
        "last_successful_fetch": detected_at,
        "resolved_dimension_id": dimension_id,
        "resolved_repository_id": repository_id,
        "products": proposed_products,
    }
    (runtime / "proposed-state.json").write_text(
        json.dumps(proposed, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Initial run is baseline only. Future unseen parent SKUs alert.
    alert_items = [] if first_run else new_items
    if alert_items:
        title, body = make_issue(alert_items, detected_at)
        (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(body, encoding="utf-8")

    set_output("new_count", str(len(alert_items)))
    state_changed = (
        first_run
        or proposed_products != old_products
        or state.get("resolved_dimension_id") != dimension_id
        or state.get("resolved_repository_id") != repository_id
    )
    set_output("state_changed", "true" if state_changed else "false")
    print(
        f"Parent monitor: dimension={dimension_id}, newest={len(current)}, "
        f"new outside Photography={len(alert_items)}, baseline={first_run}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
