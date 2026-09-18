"""Durable, budgeted Browse pagination over a frozen time window.

This is recovery, not a snapshot guarantee: eBay inventory can change between
requests. Callers persist the cursor together with the rows they accepted.
A failed or capped request must never advance the completed-window watermark.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parsed(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Search checkpoint timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def new_checkpoint(start: str, end: str, *, offset: int = 0) -> dict[str, Any]:
    if parsed(start) > parsed(end):
        raise ValueError("Search window starts after it ends")
    if offset < 0 or offset > 9999:
        raise ValueError("Invalid starting search offset")
    return {
        "version": 1,
        "start": start,
        "end": end,
        "pending": [{"start": start, "end": end, "offset": offset}],
        "completed": False,
        "successful_pages": 0,
        "last_error": "",
    }


def drain(
    checkpoint: dict[str, Any],
    fetch_window: Callable[[str, str, int], dict[str, Any]],
    fetch_next: Callable[[str], dict[str, Any]],
    *,
    max_calls: int,
    page_size: int = 200,
    retryable_errors: tuple[type[Exception], ...] = (RuntimeError,),
    newest_first: bool = False,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Return this run's rows and mutate a JSON-serializable resume cursor.

    Ordinary pages use eBay's returned next link. Windows that approach the
    10,000-result ceiling are split, including when no next link is returned.
    Inclusive split boundaries intentionally overlap and callers deduplicate
    listing IDs. Unsplittable overflow remains incomplete and visible.
    """
    if checkpoint.get("version") != 1 or not isinstance(checkpoint.get("pending"), list):
        raise ValueError("Invalid search checkpoint")
    if not 1 <= page_size <= 200 or max_calls < 0:
        raise ValueError("Invalid pagination budget or page size")
    rows: dict[str, dict[str, Any]] = {}
    calls = 0
    pending = checkpoint["pending"]
    while pending and calls < max_calls:
        work = pending[0]
        calls += 1
        try:
            page = (fetch_next(work["next"]) if work.get("next") else
                    fetch_window(work["start"], work["end"], int(work.get("offset", 0))))
            if not isinstance(page, dict) or not isinstance(page.get("itemSummaries", []), list):
                raise ValueError("Invalid Browse page envelope")
        except retryable_errors as exc:
            # Leave the failing page at the head, but return previous successes.
            checkpoint["last_error"] = str(exc)[:240]
            break
        checkpoint["last_error"] = ""
        checkpoint["successful_pages"] = int(checkpoint.get("successful_pages", 0)) + 1
        items = [row for row in page.get("itemSummaries", []) if isinstance(row, dict)]
        for row in items:
            if row.get("itemId"):
                rows[str(row["itemId"])] = row
        offset = int(page.get("offset", work.get("offset", 0)) or 0)
        total = int(page.get("total", 0) or 0)
        next_url = str(page.get("next") or "")
        next_offset = offset + page_size
        if next_url:
            try:
                next_offset = int(parse_qs(urlparse(next_url).query).get("offset", [next_offset])[0])
            except (TypeError, ValueError):
                checkpoint["last_error"] = "Invalid next-page offset; window not completed"
                break
        has_more = bool(next_url) or total > offset + len(items)
        overflow = (total >= 10000 or (has_more and next_offset >= 10000)
                    or (has_more and not next_url))
        if overflow:
            start, end = parsed(work["start"]), parsed(work["end"])
            if end - start <= timedelta(milliseconds=1):
                checkpoint["last_error"] = "Unsplittable dense time window; coverage incomplete"
                break
            middle = start + (end - start) / 2
            middle = middle.replace(microsecond=(middle.microsecond // 1000) * 1000)
            if middle <= start or middle >= end:
                checkpoint["last_error"] = "Cannot split time window further; coverage incomplete"
                break
            children = [
                {"start": work["start"], "end": stamp(middle), "offset": 0},
                {"start": stamp(middle), "end": work["end"], "offset": 0},
            ]
            if newest_first:
                children.reverse()
            pending[:1] = children
        elif next_url:
            if work.get("next") == next_url:
                checkpoint["last_error"] = "Repeated next-page URL; coverage incomplete"
                break
            pending[0] = {**work, "next": next_url, "offset": next_offset}
        else:
            pending.pop(0)
    checkpoint["completed"] = not pending
    return list(rows.values()), calls, not pending


def pending_steps(state: dict[str, Any], budget: int) -> list[dict[str, Any]]:
    """Give old continuations a bounded share, never a new full search budget."""
    windows = state.get("query_windows") or {}
    waiting = [value for value in windows.values()
               if isinstance(value, dict) and value.get("pending") and value.get("step")]
    waiting.sort(key=lambda value: (value.get("last_attempt_at", ""), value.get("end", "")))
    count = min(4, budget // 4)
    return [{**copy.deepcopy(value["step"]), "resume_only": True} for value in waiting[:count]]
