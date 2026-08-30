#!/usr/bin/env python3
"""Split comprehensive market matches into GitHub issue-sized review batches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import market_monitor as monitor

SNAPSHOT = Path("runtime/market/latest-snapshot.json")
OUTDIR = Path("runtime/market/issue-batches")
MAX_BODY_BYTES = 55_000
MAX_ITEMS_PER_BATCH = 20
MAX_FAILURES_IN_ISSUE = 20


def body_bytes(items: list[dict[str, Any]], stamp: str, failures: list[str], note: str) -> int:
    _, body = monitor.issue(items, stamp, failures, note)
    return len(body.encode("utf-8"))


def make_batches(
    items: list[dict[str, Any]], stamp: str, failures: list[str], note: str
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    for item in items:
        proposed = current + [item]
        too_many = len(proposed) > MAX_ITEMS_PER_BATCH
        too_large = body_bytes(proposed, stamp, failures, note) > MAX_BODY_BYTES
        if current and (too_many or too_large):
            batches.append(current)
            current = [item]
        else:
            current = proposed

    if current:
        batches.append(current)
    return batches


def main() -> int:
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    items = payload.get("new_matches") or []
    if not isinstance(items, list) or not items:
        raise RuntimeError("latest snapshot has no new_matches")

    stamp = str(payload.get("checked_at") or monitor.utc_now())
    failures = payload.get("failures") or []
    if not isinstance(failures, list):
        failures = []
    failures = [str(value) for value in failures[-MAX_FAILURES_IN_ISSUE:]]
    note = str(payload.get("targeted") or "targeted sweep")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    for old in OUTDIR.glob("*"):
        if old.is_file():
            old.unlink()

    batches = make_batches(items, stamp, failures, note)
    total = len(batches)

    for index, batch in enumerate(batches, start=1):
        _, body = monitor.issue(batch, stamp, failures, note)
        if total > 1:
            body = (
                f"**Review batch {index} of {total}.** The full discovery run produced "
                f"{len(items)} new matches. This issue contains {len(batch)} of them.\n\n"
                + body
            )
        size = len(body.encode("utf-8"))
        if size >= 65_536:
            raise RuntimeError(f"issue batch {index} is still too large: {size} bytes")

        stem = OUTDIR / f"{index:03d}"
        title = f"EXTERNAL_NEW: {len(batch)} market matches | batch {index}/{total}"
        stem.with_name(stem.name + "-title.txt").write_text(title + "\n", encoding="utf-8")
        stem.with_name(stem.name + "-body.md").write_text(body, encoding="utf-8")

    print(
        f"Prepared {len(items)} market matches as {total} GitHub issue batch(es); "
        f"maximum {MAX_ITEMS_PER_BATCH} items and {MAX_BODY_BYTES} body bytes per batch."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
