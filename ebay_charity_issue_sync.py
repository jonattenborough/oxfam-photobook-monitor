#!/usr/bin/env python3
"""Exclude charity listings already published if a prior run failed mid-publish."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ebay_seller_monitor as monitor

LISTING_URL = re.compile(r"(?m)^- \*\*Listing:\*\*\s+(\S+)")
ITEM_ID = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})\b")


def listing_id(item: dict[str, Any]) -> str:
    external_id = str(item.get("external_id") or "")
    if external_id.isdigit():
        return external_id
    match = ITEM_ID.search(str(item.get("url") or ""))
    return match.group(1) if match else str(item.get("key") or "")


def published_ids(issues: list[dict[str, Any]]) -> set[str]:
    published: set[str] = set()
    for issue in issues:
        if not str(issue.get("title") or "").startswith("CHARITY_NEW:"):
            continue
        for url in LISTING_URL.findall(str(issue.get("body") or "")):
            identifier = listing_id({"url": url})
            if identifier:
                published.add(identifier)
    return published


def recent_issues(repository: str, detected_at: str) -> list[dict[str, Any]]:
    """Read all recently updated issues via the paginated list API, not search indexing."""
    detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    since = (detected - timedelta(days=14)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        endpoint = f"repos/{repository}/issues?state=all&since={since}&per_page=100&page={page}"
        completed = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            capture_output=True, text=True, check=True, timeout=60,
        )
        rows = json.loads(completed.stdout)
        if not isinstance(rows, list):
            raise ValueError("GitHub issues endpoint returned a non-list response")
        results.extend(rows)
        if len(rows) < 100:
            return results
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=Path("runtime/ebay-sellers"))
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    snapshot = json.loads((args.runtime / "latest-snapshot.json").read_text(encoding="utf-8"))
    candidates = json.loads((args.runtime / "new-items.json").read_text(encoding="utf-8"))
    previous = published_ids(recent_issues(args.repository, snapshot["checked_at"]))
    unpublished = [item for item in candidates if listing_id(item) not in previous]
    count = monitor.write_issue_packets(
        args.runtime, unpublished, snapshot["checked_at"], snapshot.get("failed_sellers") or [],
    )
    print(f"Charity issue reconciliation: {len(candidates) - len(unpublished)} already published, "
          f"{len(unpublished)} in {count} new packets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
