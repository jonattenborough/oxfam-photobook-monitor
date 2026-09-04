#!/usr/bin/env python3
"""Build recall-first eBay private-seller alert packets.

The discovery search itself is fresh eBay Browse API data. Live item checks add
confidence, but are not required before a strong candidate is surfaced. This
module combines already live-verified candidates with strong search-only
candidates that the legacy monitor placed in pending_live.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import ebay_private_seller_monitor as monitor

ISSUE_CHUNK_SIZE = 10


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def set_output(name: str, value: Any) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def is_alertable(item: dict[str, Any], issue_threshold: int) -> bool:
    return (
        int(item.get("opportunity_score") or 0) >= issue_threshold
        and item.get("private_seller") is True
        and str(item.get("seller_account_type") or "").upper() != "BUSINESS"
    )


def collect_candidates(
    snapshot: dict[str, Any],
    state: dict[str, Any],
    issue_threshold: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    by_key: dict[str, dict[str, Any]] = {}
    search_only_keys: set[str] = set()

    for raw in snapshot.get("new_candidates") or []:
        if not isinstance(raw, dict) or not is_alertable(raw, issue_threshold):
            continue
        item = dict(raw)
        item["alert_verification"] = "LIVE VERIFIED"
        key = str(item.get("key") or "")
        if key:
            by_key[key] = item

    pending = state.get("pending_live") if isinstance(state.get("pending_live"), dict) else {}
    for key, raw in pending.items():
        if not isinstance(raw, dict) or not is_alertable(raw, issue_threshold):
            continue
        item = dict(raw)
        item["alert_verification"] = "SEARCH RESULT ONLY"
        item["search_observed_at"] = str(item.get("pending_since") or snapshot.get("checked_at") or "")
        by_key[str(key)] = item
        search_only_keys.add(str(key))

    ranked = sorted(
        by_key.values(),
        key=lambda item: (
            int(item.get("opportunity_score") or 0),
            1 if item.get("alert_verification") == "LIVE VERIFIED" else 0,
        ),
        reverse=True,
    )
    return ranked, search_only_keys


def _verification_line(item: dict[str, Any], detected_at: str) -> str:
    if item.get("alert_verification") == "LIVE VERIFIED" or item.get("live_verified") is True:
        checked = str(item.get("live_verified_at") or detected_at)
        return f"- **Verification:** LIVE VERIFIED at {checked}"
    observed = str(item.get("search_observed_at") or detected_at)
    return (
        f"- **Verification:** SEARCH RESULT ONLY at {observed}. "
        "This was returned by the fresh eBay search but availability was not rechecked."
    )


def make_issue_body(
    items: list[dict[str, Any]],
    *,
    detected_at: str,
    stats: dict[str, Any],
    failures: list[str],
    urgent_threshold: int,
) -> str:
    live_count = sum(
        1
        for item in items
        if item.get("alert_verification") == "LIVE VERIFIED" or item.get("live_verified") is True
    )
    search_only_count = len(items) - live_count
    lines = [
        "## New private-seller eBay photobook opportunities",
        "",
        f"Detected at **{detected_at}** by the private-seller discovery engine.",
        f"Recognition library: **{stats.get('records', '?')} books**.",
        f"This packet contains **{live_count} live-verified** and **{search_only_count} search-result-only** candidates.",
        "",
        "Search-result-only candidates are deliberately surfaced without a second item-detail check so a limited live-check allowance cannot hide a fast-moving bargain.",
        "The score is a discovery priority, not a purchase verdict. ChatGPT should still verify exact edition, printing, completeness, condition, delivery cost, current market value and current availability before recommending a purchase.",
        "",
    ]

    for item in sorted(items, key=lambda value: int(value.get("opportunity_score") or 0), reverse=True):
        score = int(item.get("opportunity_score") or 0)
        urgency = "URGENT" if score >= urgent_threshold else "REVIEW"
        lines.extend(
            [
                f"### {urgency} {score}/100 - {item.get('title') or 'Untitled listing'}",
                "",
                f"- **Observed price:** {monitor._price_line(item)}",
                f"- **Private seller:** {item.get('vendor') or 'eBay individual account'}",
                f"- **Collector lane:** {item.get('collecting_lane') or 'open discovery'}",
                f"- **Opportunity type:** {item.get('opportunity_kind') or 'review lead'}",
                f"- **Discovery lane:** {item.get('search_lane') or 'unknown'}",
                f"- **Search:** `{item.get('search_query') or ''}`",
                f"- **Buying format:** {', '.join(item.get('buying_options') or []) or 'not returned'}",
                _verification_line(item, detected_at),
                f"- **Why it surfaced:** {', '.join(item.get('opportunity_reasons') or [])}",
                f"- **Listing:** {item.get('url')}",
            ]
        )
        best = item.get("best_recognition")
        if isinstance(best, dict):
            canon = str(best.get("canon_sources") or "Recognition library")
            tier = str(best.get("collectibility_tier") or "?")
            year = f" ({best.get('year')})" if best.get("year") else ""
            lines.append(
                f"- **Best recognition:** {best.get('contributor')}, *{best.get('title')}*{year} | "
                f"match {best.get('score')}/100 | tier {tier} | {canon}"
            )
            if best.get("first_edition_notes"):
                lines.append(f"- **Edition target note:** {best.get('first_edition_notes')}")
            collector_fit = [
                str(best.get("collector_profile") or "").strip(),
                "first monograph" if str(best.get("first_monograph") or "").upper() == "YES" else "",
            ]
            collector_fit = [value for value in collector_fit if value]
            if collector_fit:
                lines.append(f"- **Collector fit:** {' | '.join(collector_fit)}")
            if best.get("awards_and_evidence"):
                lines.append(f"- **Respect evidence:** {best.get('awards_and_evidence')}")
            if best.get("collectible_variants"):
                lines.append(f"- **Known collectible variants:** {best.get('collectible_variants')}")
            if best.get("collectible_format_evidence"):
                lines.append(
                    f"- **Listing object evidence:** {', '.join(best.get('collectible_format_evidence') or [])}"
                )
            if best.get("edition_status"):
                detail = "; ".join(str(value) for value in best.get("edition_reasons") or [])
                lines.append(
                    f"- **Edition evidence:** {best.get('edition_status')}"
                    + (f" | {detail}" if detail else "")
                )
        bibliographic = [
            f"author: {item.get('author')}" if item.get("author") else "",
            f"publisher: {item.get('publisher')}" if item.get("publisher") else "",
            f"year: {item.get('publication_year')}" if item.get("publication_year") else "",
            f"edition: {item.get('edition')}" if item.get("edition") else "",
            f"ISBN: {item.get('isbn')}" if item.get("isbn") else "",
        ]
        bibliographic = [value for value in bibliographic if value]
        if bibliographic:
            lines.append(f"- **eBay bibliographic fields:** {', '.join(bibliographic)}")
        if item.get("image_url"):
            lines.append(f"- **Main image:** {item.get('image_url')}")
        if item.get("description"):
            excerpt = " ".join(str(item.get("description") or "").split())[:700]
            label = "Live description excerpt" if item.get("live_verified") is True else "Description excerpt"
            lines.append(f"- **{label}:** {excerpt}")
        lines.append("")

    if failures:
        lines.extend(["### Temporary search warnings", ""])
        lines.extend(f"- {failure}" for failure in failures[:20])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def mark_search_only_alerted(
    state: dict[str, Any],
    candidates: list[dict[str, Any]],
    search_only_keys: set[str],
    detected_at: str,
) -> None:
    seen = state.setdefault("seen", {})
    pending = state.setdefault("pending_live", {})
    for item in candidates:
        key = str(item.get("key") or "")
        if key not in search_only_keys:
            continue
        monitor._record_seen(seen, item, detected_at)
        pending.pop(key, None)
    state["seen"] = monitor._trim_seen(seen)


def write_packets(
    runtime: Path,
    candidates: list[dict[str, Any]],
    *,
    detected_at: str,
    stats: dict[str, Any],
    failures: list[str],
    urgent_threshold: int,
) -> int:
    alerts_dir = runtime / "alerts"
    if alerts_dir.exists():
        shutil.rmtree(alerts_dir)
    alerts_dir.mkdir(parents=True, exist_ok=True)
    chunks = [
        candidates[index:index + ISSUE_CHUNK_SIZE]
        for index in range(0, len(candidates), ISSUE_CHUNK_SIZE)
    ]
    for index, chunk in enumerate(chunks, start=1):
        live_count = sum(1 for item in chunk if item.get("live_verified") is True)
        search_count = len(chunk) - live_count
        title = (
            f"EBAY_PRIVATE_NEW: {len(chunk)} candidates | "
            f"{live_count} live checked | {search_count} search only"
        )
        if len(chunks) > 1:
            title += f" | {index}/{len(chunks)}"
        stem = alerts_dir / f"issue-{index:03d}"
        stem.with_suffix(".title").write_text(title[:240] + "\n", encoding="utf-8")
        stem.with_suffix(".md").write_text(
            make_issue_body(
                chunk,
                detected_at=detected_at,
                stats=stats,
                failures=failures,
                urgent_threshold=urgent_threshold,
            ),
            encoding="utf-8",
        )
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/ebay_private_searches.json")
    parser.add_argument("--runtime-dir", default="runtime/ebay-private")
    args = parser.parse_args()

    runtime = Path(args.runtime_dir)
    snapshot = load_json(runtime / "latest-snapshot.json")
    state = load_json(runtime / "proposed-state.json")
    config = monitor.load_config(Path(args.config))
    detected_at = str(snapshot.get("checked_at") or monitor.utc_now())
    candidates, search_only_keys = collect_candidates(
        snapshot,
        state,
        int(config["issue_threshold"]),
    )
    mark_search_only_alerted(state, candidates, search_only_keys, detected_at)
    monitor.write_json(runtime / "proposed-state.json", state)

    issue_count = write_packets(
        runtime,
        candidates,
        detected_at=detected_at,
        stats=snapshot.get("library_stats") if isinstance(snapshot.get("library_stats"), dict) else {},
        failures=[str(value) for value in snapshot.get("failures") or []],
        urgent_threshold=int(config["urgent_threshold"]),
    )
    search_only_count = sum(
        1 for item in candidates if item.get("alert_verification") == "SEARCH RESULT ONLY"
    )
    live_count = len(candidates) - search_only_count
    set_output("alert_count", len(candidates))
    set_output("issue_count", issue_count)
    set_output("search_only_count", search_only_count)
    set_output("live_verified_count", live_count)
    print(
        f"Recall-first alert builder: {len(candidates)} candidates, "
        f"{live_count} live checked, {search_only_count} search only, {issue_count} issue packets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
