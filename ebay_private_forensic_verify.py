#!/usr/bin/env python3
"""Live-verify the strongest score-independent forensic audit candidates."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import ebay_api
import ebay_private_forensic_audit as audit
import ebay_private_seller_backfill as backfill
import ebay_private_seller_monitor as live_monitor
import parr_badger_runner as pb

DEFAULT_MAX_CALLS = 200
DEFAULT_HARD_CALL_CAP = 200
DEFAULT_QUOTA_RESERVE = 250
DEFAULT_CANDIDATE_LIMIT = 200


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return backfill.load_json(path, default, path.name)


def candidate_queue(candidates: dict[str, Any], *, limit: int) -> list[str]:
    items = candidates.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Forensic audit candidates items is not a list")
    eligible = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("review") is True
        and item.get("obvious_nonbook") is not True
        and str(item.get("prior_status") or "") != "surfaced"
        and str(item.get("key") or "")
    ]
    eligible.sort(
        key=lambda item: (
            -int(item.get("audit_priority") or 0),
            audit._price(item),
            pb.normalize(item.get("title")),
        )
    )
    return [str(item["key"]) for item in eligible[: max(1, int(limit))]]


def initialize_state(
    state: dict[str, Any],
    candidates: dict[str, Any],
    detected_at: str,
    *,
    limit: int,
    restart: bool,
) -> bool:
    if isinstance(state.get("queue"), list) and not restart:
        return False
    queue = candidate_queue(candidates, limit=limit)
    state.clear()
    state.update(
        {
            "version": 1,
            "created_at": detected_at,
            "candidate_limit": int(limit),
            "initial_queue_size": len(queue),
            "queue": queue,
            "complete": not queue,
            "total_calls": 0,
            "total_live": 0,
            "total_unavailable": 0,
        }
    )
    return True


def _candidate_index(candidates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = candidates.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Forensic audit candidates items is not a list")
    return {
        str(item.get("key")): item
        for item in items
        if isinstance(item, dict) and str(item.get("key") or "")
    }


def _refresh_candidate(candidate: dict[str, Any], detail: dict[str, Any], detected_at: str) -> dict[str, Any]:
    enriched = live_monitor._merge_live_detail(candidate, detail)
    best = candidate.get("best_target_match")
    target = best.get("target") if isinstance(best, dict) and isinstance(best.get("target"), dict) else {}
    evaluation = audit.evaluate(enriched, target)
    refreshed = {
        **enriched,
        "audit_priority": int(evaluation["audit_priority"]),
        "review": bool(evaluation["review"]),
        "obvious_nonbook": bool(evaluation["obvious_nonbook"]),
        "best_target_match": {"target": target, **evaluation},
        "live_verified": True,
        "live_verified_at": detected_at,
        "availability_reason": "live",
    }
    return refreshed


def run_verification(
    client: ebay_api.EbayBrowseClient,
    state: dict[str, Any],
    candidates: dict[str, Any],
    findings: dict[str, Any],
    *,
    call_budget: int,
    detected_at: str,
) -> dict[str, Any]:
    queue = state.get("queue")
    if not isinstance(queue, list):
        raise RuntimeError("Forensic verification queue is not a list")
    index = _candidate_index(candidates)
    items = findings.setdefault("items", {})
    if not isinstance(items, dict):
        raise RuntimeError("Forensic verified findings items is not an object")

    was_complete = bool(state.get("complete"))
    calls = 0
    live_count = 0
    unavailable_count = 0
    failures: list[str] = []

    while queue and calls < max(0, int(call_budget)):
        key = str(queue.pop(0))
        candidate = index.get(key)
        if not candidate:
            continue
        rest_item_id = str(candidate.get("rest_item_id") or "")
        if not rest_item_id:
            items[key] = {
                **candidate,
                "live_verified": False,
                "availability_reason": "missing eBay REST item id",
                "live_verified_at": detected_at,
            }
            unavailable_count += 1
            continue
        calls += 1
        try:
            is_live, reason, detail = client.live_status(rest_item_id)
        except Exception as exc:
            failures.append(f"live-check {candidate.get('external_id')}: {exc}")
            queue.append(key)
            continue
        if not is_live:
            items[key] = {
                **candidate,
                "live_verified": False,
                "availability_reason": reason,
                "live_verified_at": detected_at,
            }
            unavailable_count += 1
            continue
        items[key] = _refresh_candidate(candidate, detail, detected_at)
        live_count += 1

    state["queue"] = queue
    state["complete"] = not queue
    state["last_run"] = detected_at
    state["last_run_calls"] = calls
    state["last_run_live"] = live_count
    state["last_run_unavailable"] = unavailable_count
    state["last_run_failures"] = failures
    state["total_calls"] = int(state.get("total_calls") or 0) + calls
    state["total_live"] = int(state.get("total_live") or 0) + live_count
    state["total_unavailable"] = int(state.get("total_unavailable") or 0) + unavailable_count
    findings["last_updated"] = detected_at

    return {
        "calls": calls,
        "live": live_count,
        "unavailable": unavailable_count,
        "remaining": len(queue),
        "complete": bool(state["complete"]),
        "completed_now": bool(state["complete"] and not was_complete),
        "total_verified_records": len(items),
        "failures": failures,
    }


def _money(item: dict[str, Any]) -> str:
    price = audit._price(item)
    return "unknown" if price >= 999999 else f"£{price:.2f}"


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def live_findings(findings: dict[str, Any]) -> list[dict[str, Any]]:
    values = findings.get("items")
    if not isinstance(values, dict):
        return []
    live = [
        value
        for value in values.values()
        if isinstance(value, dict)
        and value.get("live_verified") is True
        and value.get("review") is True
        and value.get("obvious_nonbook") is not True
        and str(value.get("seller_account_type") or "").upper() != "BUSINESS"
    ]
    live.sort(
        key=lambda item: (
            -int(item.get("audit_priority") or 0),
            audit._price(item),
            pb.normalize(item.get("title")),
        )
    )
    return live


def make_report(findings: dict[str, Any]) -> str:
    values = live_findings(findings)
    lines = [
        "# Live-verified independent eBay forensic candidates",
        "",
        "These listings survived a score-independent raw-results audit and were freshly checked through eBay's item endpoint. They still require human edition, condition and market-value research before purchase.",
        "",
        f"Live independent candidates: **{len(values)}**",
        "",
        "| Priority | Target | Listing | Landed | Prior monitor status | Evidence to inspect |",
        "|---:|---|---|---:|---|---|",
    ]
    for item in values:
        best = item.get("best_target_match") if isinstance(item.get("best_target_match"), dict) else {}
        target = best.get("target") if isinstance(best.get("target"), dict) else {}
        reasons = "; ".join(str(value) for value in (best.get("reasons") or [])[:3])
        link = f"[{_escape(item.get('title'))}]({item.get('url')})"
        lines.append(
            f"| {item.get('audit_priority')} | {_escape(target.get('contributor'))}, "
            f"*{_escape(target.get('title'))}* | {link} | {_money(item)} | "
            f"{_escape(item.get('prior_status'))} | {_escape(reasons)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_ready_issue(runtime: Path, findings: dict[str, Any]) -> None:
    issues = runtime / "issues"
    if issues.exists():
        shutil.rmtree(issues)
    issues.mkdir(parents=True, exist_ok=True)
    values = live_findings(findings)
    title = f"EBAY_PRIVATE_FORENSIC_READY: {len(values)} live independent candidates"
    body = make_report({"items": {str(value.get('key')): value for value in values[:50]}})
    body += "\nThe complete verified queue is stored in `data/ebay_private_forensic_verified.json`.\n"
    (issues / "issue-001.title").write_text(title[:240] + "\n", encoding="utf-8")
    (issues / "issue-001.md").write_text(body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="data/ebay_private_forensic_audit_candidates.json")
    parser.add_argument("--state", default="data/ebay_private_forensic_verify_state.json")
    parser.add_argument("--findings", default="data/ebay_private_forensic_verified.json")
    parser.add_argument("--report", default="data/ebay_private_forensic_verified_report.md")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private-forensic-verify")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--hard-call-cap", type=int, default=DEFAULT_HARD_CALL_CAP)
    parser.add_argument("--quota-reserve", type=int, default=DEFAULT_QUOTA_RESERVE)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if args.max_calls < 1 or args.hard_call_cap < 1 or args.candidate_limit < 1:
        parser.error("call and candidate caps must be at least 1")
    if args.quota_reserve < 0:
        parser.error("--quota-reserve cannot be negative")

    candidates = load_json(Path(args.candidates), {"version": 1, "items": []})
    state = load_json(Path(args.state), {"version": 1})
    findings = load_json(Path(args.findings), {"version": 1, "items": {}})
    detected_at = live_monitor.utc_now()
    initialized = initialize_state(
        state,
        candidates,
        detected_at,
        limit=args.candidate_limit,
        restart=bool(args.restart),
    )

    client = ebay_api.EbayBrowseClient(marketplace="EBAY_GB")
    call_budget, quota, quota_warning = backfill.api_call_budget(
        client,
        args.max_calls,
        hard_cap=args.hard_call_cap,
        quota_reserve=args.quota_reserve,
    )
    result = run_verification(
        client,
        state,
        candidates,
        findings,
        call_budget=call_budget,
        detected_at=detected_at,
    )
    result["quota"] = quota
    result["api_call_budget"] = call_budget
    if quota_warning:
        result["failures"].append(quota_warning)

    runtime = Path(args.runtime_dir)
    live_monitor.write_json(runtime / "proposed-state.json", state)
    live_monitor.write_json(runtime / "proposed-findings.json", findings)
    live_monitor.write_json(runtime / "latest-snapshot.json", result)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(make_report(findings), encoding="utf-8")
    if result["completed_now"]:
        write_ready_issue(runtime, findings)

    live_monitor.set_output("state_changed", "true" if initialized or result["calls"] else "false")
    live_monitor.set_output("remaining", result["remaining"])
    live_monitor.set_output("complete", "true" if result["complete"] else "false")
    live_monitor.set_output("completed_now", "true" if result["completed_now"] else "false")
    live_monitor.set_output("calls", result["calls"])
    live_monitor.set_output("live", result["live"])
    print(
        f"Forensic verification: {result['calls']} calls, {result['live']} live, "
        f"{result['unavailable']} unavailable, {result['remaining']} pending."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
