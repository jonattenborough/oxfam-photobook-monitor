#!/usr/bin/env python3
"""Evidence-based reviewer run ledger.

Chat reviewer automations write START and FINISH markers to one persistent
GitHub issue. This module derives a receipt from those comments and independently
checks every claimed completed issue. Automation schedule metadata is never
accepted as proof of a completed review.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

START_MARKER = "CHATGPT_REVIEW_RUN_START:"
FINISH_MARKER = "CHATGPT_REVIEW_RUN_FINISH:"
DEFAULT_LEDGER_ISSUE = 2145
DEFAULT_TASKS = (
    "Endgame Urgent Review",
    "eBay Fixed Price Review",
    "eBay and Endgame Review",
)
COMPLETION_MARKER = "CHATGPT_GEM_REVIEWED:"
VALID_STATUS = {"COMPLETED", "PARTIAL", "BLOCKED"}


def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _pages(get_list: Callable[[str], list[dict[str, Any]]], endpoint: str):
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        rows = get_list(f"{endpoint}{separator}per_page=100&page={page}")
        yield from rows
        if len(rows) < 100:
            return
        page += 1


def _owner(comment: dict[str, Any], owner: str) -> bool:
    return str((comment.get("user") or {}).get("login") or "").lower() == owner.lower()


def parse_marker(comment: dict[str, Any], owner: str, marker: str) -> dict[str, Any] | None:
    if not _owner(comment, owner):
        return None
    body = str(comment.get("body") or "")
    if not body.startswith(marker):
        return None
    raw = body[len(marker):].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    run_id = str(payload.get("run_id") or "").strip()
    task = str(payload.get("task") or "").strip()
    if not run_id or not task:
        return None
    result = dict(payload)
    result["_comment_id"] = comment.get("id")
    result["_comment_url"] = comment.get("html_url")
    result["_comment_created_at"] = comment.get("created_at")
    return result


def _trusted_completion(comment: dict[str, Any], owner: str) -> bool:
    return (
        _owner(comment, owner)
        and str(comment.get("body") or "").startswith(COMPLETION_MARKER)
    )


def verify_completed_issue(
    repo: str,
    issue_number: int,
    owner: str,
    get_list: Callable[[str], list[dict[str, Any]]],
    get_object: Callable[[str], dict[str, Any]],
) -> tuple[bool, str]:
    try:
        issue = get_object(f"repos/{repo}/issues/{issue_number}")
    except Exception as exc:  # caller records exact error class, not fake success
        return False, f"issue fetch failed ({type(exc).__name__})"
    if str(issue.get("state") or "").lower() != "closed":
        return False, "issue is not closed"
    try:
        comments = _pages(get_list, f"repos/{repo}/issues/{issue_number}/comments")
        if not any(_trusted_completion(comment, owner) for comment in comments):
            return False, "trusted completion marker missing"
    except Exception as exc:
        return False, f"comment verification failed ({type(exc).__name__})"
    return True, "closed with trusted completion marker"


def _age_minutes(now: datetime, value: Any) -> float | None:
    dt = parse_time(value)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 60.0, 1)


def derive_receipt(
    repo: str,
    ledger_issue: int,
    get_list: Callable[[str], list[dict[str, Any]]],
    get_object: Callable[[str], dict[str, Any]],
    now: datetime,
    required_tasks: tuple[str, ...] = DEFAULT_TASKS,
    stale_start_minutes: float = 30.0,
    stale_finish_minutes: float = 95.0,
) -> dict[str, Any]:
    owner = repo.split("/")[0]
    checked_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    warnings: list[str] = []
    try:
        comments = list(_pages(get_list, f"repos/{repo}/issues/{ledger_issue}/comments"))
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "ledger_issue_number": ledger_issue,
            "status": "BLOCKED",
            "latest_verified_completion_at": None,
            "tasks": {},
            "open_runs": [],
            "warnings": [f"Reviewer ledger unreadable ({type(exc).__name__})"],
        }

    starts: dict[str, dict[str, Any]] = {}
    finishes: dict[str, dict[str, Any]] = {}
    malformed_owner_markers = 0
    for comment in comments:
        body = str(comment.get("body") or "")
        if body.startswith(START_MARKER):
            parsed = parse_marker(comment, owner, START_MARKER)
            if parsed:
                starts[parsed["run_id"]] = parsed
            elif _owner(comment, owner):
                malformed_owner_markers += 1
        elif body.startswith(FINISH_MARKER):
            parsed = parse_marker(comment, owner, FINISH_MARKER)
            if parsed:
                finishes[parsed["run_id"]] = parsed
            elif _owner(comment, owner):
                malformed_owner_markers += 1
    if malformed_owner_markers:
        warnings.append(f"{malformed_owner_markers} malformed owner-authored reviewer ledger marker(s)")

    valid_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    open_runs: list[dict[str, Any]] = []
    orphan_finishes: list[str] = []
    for run_id, start in starts.items():
        finish = finishes.get(run_id)
        if finish and finish.get("task") == start.get("task"):
            valid_pairs.append((start, finish))
        else:
            started_at = start.get("started_at") or start.get("_comment_created_at")
            open_runs.append({
                "run_id": run_id,
                "task": start.get("task"),
                "started_at": started_at,
                "age_minutes": _age_minutes(now, started_at),
                "comment_url": start.get("_comment_url"),
            })
    for run_id in finishes:
        if run_id not in starts:
            orphan_finishes.append(run_id)
    if orphan_finishes:
        warnings.append(f"{len(orphan_finishes)} FINISH marker(s) have no matching START")

    task_receipts: dict[str, Any] = {}
    latest_verified_completion_at: str | None = None
    for task in required_tasks:
        pairs = [
            pair for pair in valid_pairs
            if pair[0].get("task") == task and pair[1].get("task") == task
        ]
        pairs.sort(
            key=lambda pair: str(
                pair[1].get("finished_at")
                or pair[1].get("_comment_created_at")
                or ""
            )
        )
        if not pairs:
            task_receipts[task] = {
                "status": "NO_VERIFIED_FINISH",
                "finished_at": None,
                "age_minutes": None,
                "run_id": None,
                "verified_completed_issue_numbers": [],
                "invalid_completed_issue_claims": {},
            }
            warnings.append(f"{task}: no matching START/FINISH receipt has been recorded")
            continue

        start, finish = pairs[-1]
        finished_at = finish.get("finished_at") or finish.get("_comment_created_at")
        age = _age_minutes(now, finished_at)
        claimed = finish.get("completed_issue_numbers") or []
        if not isinstance(claimed, list):
            claimed = []
        normalized_claimed: list[int] = []
        for value in claimed:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in normalized_claimed:
                normalized_claimed.append(number)

        verified: list[int] = []
        invalid: dict[str, str] = {}
        for issue_number in normalized_claimed:
            ok, reason = verify_completed_issue(repo, issue_number, owner, get_list, get_object)
            if ok:
                verified.append(issue_number)
            else:
                invalid[str(issue_number)] = reason

        declared_status = str(finish.get("status") or "").upper()
        if declared_status not in VALID_STATUS:
            declared_status = "INVALID_STATUS"
        evidence_status = declared_status
        if invalid:
            evidence_status = "UNVERIFIED"
            warnings.append(
                f"{task}: FINISH claimed {len(invalid)} issue completion(s) that GitHub did not verify"
            )
        if age is None or age > stale_finish_minutes:
            warnings.append(f"{task}: latest verified FINISH receipt is stale")

        if not invalid and declared_status in {"COMPLETED", "PARTIAL"} and finished_at:
            if latest_verified_completion_at is None or str(finished_at) > latest_verified_completion_at:
                latest_verified_completion_at = str(finished_at)

        task_receipts[task] = {
            "status": evidence_status,
            "declared_status": declared_status,
            "run_id": finish.get("run_id"),
            "started_at": start.get("started_at") or start.get("_comment_created_at"),
            "finished_at": finished_at,
            "age_minutes": age,
            "claimed_completed_issue_numbers": normalized_claimed,
            "verified_completed_issue_numbers": verified,
            "invalid_completed_issue_claims": invalid,
            "unique_listings_reviewed": finish.get("unique_listings_reviewed"),
            "reportable_listing_count": finish.get("reportable_listing_count"),
            "errors": finish.get("errors") if isinstance(finish.get("errors"), list) else [],
            "start_comment_url": start.get("_comment_url"),
            "finish_comment_url": finish.get("_comment_url"),
        }

    for run in open_runs:
        if run.get("task") not in required_tasks:
            continue
        age = run.get("age_minutes")
        if age is not None and age > stale_start_minutes:
            warnings.append(
                f"{run.get('task')}: START has no matching FINISH after {age:.1f} minutes"
            )

    return {
        "checked_at": checked_at,
        "ledger_issue_number": ledger_issue,
        "status": "ATTENTION" if warnings else "OK",
        "latest_verified_completion_at": latest_verified_completion_at,
        "tasks": task_receipts,
        "open_runs": open_runs,
        "warnings": warnings,
    }
