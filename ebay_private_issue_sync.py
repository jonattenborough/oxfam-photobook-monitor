#!/usr/bin/env python3
"""Prevent duplicate private review issues after a partial GitHub publication."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import ebay_charity_issue_sync as github_issues
import ebay_private_alert_builder as builder
import ebay_private_recall_monitor as recall
import ebay_private_seller_monitor as monitor

FINGERPRINT = re.compile(r"(?m)^- \*\*Alert fingerprint:\*\*\s+([0-9a-f]{20})\s*$")


def published_fingerprints(issues: list[dict[str, Any]]) -> set[str]:
    return {
        fingerprint
        for issue in issues if str(issue.get("title") or "").startswith("EBAY_PRIVATE_NEW:")
        for fingerprint in FINGERPRINT.findall(str(issue.get("body") or ""))
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=Path("runtime/ebay-private"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--config", type=Path, default=Path("data/ebay_private_recall_searches.json"))
    args = parser.parse_args()
    snapshot = json.loads((args.runtime / "latest-snapshot.json").read_text(encoding="utf-8"))
    items = json.loads((args.runtime / "alert-items.json").read_text(encoding="utf-8"))
    published = published_fingerprints(
        github_issues.recent_issues(args.repository, snapshot["checked_at"])
    )
    fresh = [item for item in items if recall.alert_fingerprint(item) not in published]
    config = monitor.load_config(args.config)
    count = builder.write_packets(
        args.runtime, fresh,
        detected_at=snapshot["checked_at"],
        stats=snapshot.get("library_stats") or {},
        failures=[str(value) for value in snapshot.get("failures") or []],
        urgent_threshold=int(config["urgent_threshold"]),
    )
    print(f"Private issue reconciliation: {len(items) - len(fresh)} already published, "
          f"{len(fresh)} in {count} new packets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
