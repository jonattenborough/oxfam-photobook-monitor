#!/usr/bin/env python3
"""Read-only issue triage index and evidence-based monitor health.

Never closes, comments on, or discards an issue. The index is discovery data,
not a valuation or live eBay check. A scheduled trigger is NOT a review receipt.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PREFIXES = (
    "ENDGAME_4H:", "ENDGAME_EARLY:", "EBAY_PRIVATE_NEW:", "CHARITY_NEW:",
    "EXTERNAL_NEW:", "OXFAM_NEW:", "OXFAM_ART_NEW:",
)
MARKER = "CHATGPT_GEM_REVIEWED:"
EXTERNAL_MARKER = "CHATGPT_EXTERNAL_REVIEWED:"
ITEM_ID = re.compile(r"https?://(?:www\.)?ebay\.[a-z.]+/itm/(?:[^/\s)]+/)?(\d{9,15})")
END_DATE = re.compile(r"\*\*Ends:\*\*\s*(\d{4}-\d\d-\d\dT[\d:.]+Z)")
PRICE = re.compile(r"(?:\*\*(?:Observed price|Bid/price|Oxfam price):\*\*\s*)(?:GBP\s*|£)([\d,]+(?:\.\d{1,2})?)")


def parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def github_get(endpoint: str) -> list[dict[str, Any]]:
    # No shell interpolation of issue text, URLs, titles, or comments.
    result = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                            capture_output=True, text=True, check=True, timeout=60)
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise ValueError("Expected a GitHub list response")
    return payload


def github_get_object(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(["gh", "api", "--method", "GET", endpoint],
                            capture_output=True, text=True, check=True, timeout=60)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("Expected a GitHub object response")
    return payload


def pages(get: Callable, endpoint: str):
    """Use the list API beyond 1,000 issues, not GitHub Search's result cap."""
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        rows = get(f"{endpoint}{separator}per_page=100&page={page}")
        yield from rows
        if len(rows) < 100:
            break
        page += 1


def trusted_receipt(comment: dict[str, Any], owner: str, source: str = "") -> bool:
    markers = (MARKER, EXTERNAL_MARKER) if source in ("", "EXTERNAL_NEW") else (MARKER,)
    return (str(comment.get("body") or "").startswith(markers)
            and str((comment.get("user") or {}).get("login") or "").lower() == owner.lower())


def summarize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    body = str(issue.get("body") or "")
    summaries = []
    for section in re.split(r"(?m)^### ", body)[1:]:
        heading = section.splitlines()[0]
        price = PRICE.search(section)
        summaries.append({
            "title": heading[:350],
            "listing_ids": list(dict.fromkeys(ITEM_ID.findall(section))),
            "observed_price_gbp": float(price.group(1).replace(",", "")) if price else None,
        })
    amounts = [row["observed_price_gbp"] for row in summaries if row["observed_price_gbp"] is not None]
    ends = sorted(set(END_DATE.findall(body)))
    return {
        "number": issue["number"], "title": issue["title"],
        "created_at": issue["created_at"], "updated_at": issue.get("updated_at"),
        "url": issue["html_url"],
        "earliest_end_at": ends[0] if ends else None,
        "all_end_times": ends,
        "lowest_observed_gbp": min(amounts) if amounts else None,
        "candidate_summaries": summaries,
    }


def collect(repo: str, get: Callable, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository name")
    previous = previous or {}
    cache = previous.get("comment_cache") or {}
    next_cache = {}
    queues = {prefix[:-1]: [] for prefix in PREFIXES}
    completed_open = []
    errors = []
    owner = repo.split("/")[0]
    last_review = str(previous.get("last_completed_review_at") or "")
    for issue in pages(get, f"repos/{repo}/issues?state=open&sort=created&direction=asc"):
        if "pull_request" in issue:
            continue
        prefix = next((p for p in PREFIXES if str(issue.get("title") or "").startswith(p)), None)
        if not prefix:
            continue
        key = str(issue["number"])
        cached = cache.get(key, {})
        signature = [issue.get("updated_at"), issue.get("comments", 0)]
        receipt = None
        if int(issue.get("comments") or 0):
            if cached.get("signature") == signature:
                receipt = cached.get("receipt")
            else:
                try:
                    comments = pages(get, f"repos/{repo}/issues/{key}/comments")
                    receipts = [c for c in comments if trusted_receipt(c, owner, prefix[:-1])]
                    receipt = max(receipts, key=lambda c: c.get("created_at", ""), default=None)
                except (subprocess.SubprocessError, ValueError) as exc:
                    # Unread comments are not proof of a completed review.
                    errors.append(f"Issue {key}: comments not verified ({type(exc).__name__})")
                    signature = None
        small_receipt = {"created_at": receipt.get("created_at")} if receipt else None
        next_cache[key] = {"signature": signature, "receipt": small_receipt}
        if receipt:
            completed_open.append(issue["number"])
            last_review = max(last_review, str(receipt.get("created_at") or ""))
        else:
            queues[prefix[:-1]].append(summarize_issue(issue))
    # Closed issues contain most completed reviews. Inspect recent comments;
    # preserve the previously observed timestamp rather than inventing one.
    for page in range(1, 6):
        recent = get(f"repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100&page={page}")
        receipts = [c for c in recent if trusted_receipt(c, owner)]
        if receipts:
            last_review = max(last_review, max(str(c.get("created_at") or "") for c in receipts))
            break
        if len(recent) < 100:
            break
    for name, entries in queues.items():
        if name.startswith("ENDGAME"):
            entries.sort(key=lambda row: (row["earliest_end_at"] or "9999", row["created_at"]))
        else:
            entries.sort(key=lambda row: (price_band(row["lowest_observed_gbp"]), row["created_at"]))
    return {"queues": queues, "completed_but_open": completed_open,
            "last_completed_review_at": last_review or None,
            "comment_cache": next_cache, "errors": errors, "index_scan_complete": not errors}


def price_band(value: float | None) -> int:
    if value is None:
        return 3
    return 0 if value <= 50 else 1 if value <= 100 else 2 if value <= 150 else 4


def health(index: dict[str, Any], endgame: dict[str, Any], now: datetime) -> dict[str, Any]:
    def age(value):
        dt = parse_time(value)
        return round((now - dt).total_seconds() / 60, 1) if dt else None
    discovery = endgame.get("last_discovery_at")
    discovery_age = age(discovery)
    review_age = age(index.get("last_completed_review_at"))
    counts = {name: len(entries) for name, entries in index["queues"].items()}
    warnings = list(index["errors"])
    if discovery_age is None or discovery_age > 120:
        warnings.append("Successful auction discovery is stale or unknown")
    if sum(counts.values()) and (review_age is None or review_age > 90):
        warnings.append("Waiting issues have no recent recorded completed review")
    overdue = []
    urgent = []
    for name, entries in index["queues"].items():
        if not name.startswith("ENDGAME"):
            continue
        for row in entries:
            times = [parse_time(t) for t in row["all_end_times"]]
            times = [t for t in times if t]
            if any(t <= now for t in times):
                overdue.append(row["number"])
            if any(0 < (t - now).total_seconds() <= 14400 for t in times):
                urgent.append(row["number"])
    if overdue:
        warnings.append("Unreviewed Endgame packets include elapsed auction deadlines")
    if any(count > 12 for name, count in counts.items() if not name.startswith("ENDGAME")):
        warnings.append("Fixed-price review backlog exceeds two nominal six-issue runs")
    return {
        "checked_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "ATTENTION" if warnings else "OK",
        "index_scan_complete": index["index_scan_complete"],
        "successful_discovery_at": discovery,
        "discovery_age_minutes": discovery_age,
        "last_completed_review_at": index.get("last_completed_review_at"),
        "completed_review_age_minutes": review_age,
        "unreviewed_issues_by_source": counts,
        "completed_but_open": index["completed_but_open"],
        "unreviewed_elapsed_deadline_issue_numbers": overdue,
        "unreviewed_next_four_hours_issue_numbers": urgent,
        "unfinished_endgame_windows": len(endgame.get("search_windows", {})),
        "warnings": warnings,
        "limitations": "Issue index only; not live eBay availability, edition proof, or full search coverage.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "jonattenborough/oxfam-photobook-monitor"))
    parser.add_argument("--runtime", type=Path, default=Path("runtime/review-health"))
    args = parser.parse_args()
    state_path = Path("data/photobook_review_index.json")
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    index = collect(args.repo, github_get, previous)
    endgame_path = Path("data/ebay_endgame_state.json")
    endgame = json.loads(endgame_path.read_text()) if endgame_path.exists() else {}
    now = datetime.now(timezone.utc)
    index["checked_at"] = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    report = health(index, endgame, now)
    import photobook_reviewer_ledger as reviewer_ledger
    ledger_issue = int(os.getenv("PHOTOBOOK_REVIEW_LEDGER_ISSUE", reviewer_ledger.DEFAULT_LEDGER_ISSUE))
    reviewer_receipt = reviewer_ledger.derive_receipt(
        args.repo, ledger_issue, github_get, github_get_object, now,
    )
    report["reviewer_run_receipt_status"] = reviewer_receipt["status"]
    report["latest_verified_reviewer_run_at"] = reviewer_receipt["latest_verified_completion_at"]
    if reviewer_receipt["warnings"]:
        report["warnings"].extend(reviewer_receipt["warnings"])
        report["status"] = "ATTENTION"
    import ebay_endgame as radar
    merged_state = {**radar.blank_state(), **endgame}
    report["auction_search_routes"] = radar.coverage_status(
        radar.build_tasks(radar.load_config()), merged_state, now,
    )
    if report["auction_search_routes"]["never_searched"]:
        report["warnings"].append("Some configured auction search routes have never completed a first request")
        report["status"] = "ATTENTION"
    atomic_json(args.runtime / "index.json", index)
    atomic_json(args.runtime / "health.json", report)
    atomic_json(args.runtime / "reviewer-receipt.json", reviewer_receipt)
    summary = ("# Photobook review health\n\n" + json.dumps(report, indent=2) + "\n")
    (args.runtime / "summary.md").write_text(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
