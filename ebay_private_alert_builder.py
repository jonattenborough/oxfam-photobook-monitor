#!/usr/bin/env python3
"""Build recall-first eBay private-seller alert packets.

The discovery search itself is fresh eBay Browse API data. Live item checks add
confidence, but are not required before a strong candidate is surfaced. This
module combines already live-verified candidates with strong search-only
candidates and puts the most time-sensitive bargains into the first packets.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import ebay_private_recall_monitor as recall
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
    score_or_recall_lane = (
        int(item.get("opportunity_score") or 0) >= issue_threshold
        or item.get("recall_first_unknown") is True
        or item.get("material_change") is True
    )
    return (
        score_or_recall_lane
        and item.get("private_seller") is True
        and str(item.get("seller_account_type") or "").upper() != "BUSINESS"
    )


def _collectibility_tier(item: dict[str, Any]) -> str:
    best = item.get("best_recognition")
    if isinstance(best, dict):
        return str(best.get("collectibility_tier") or "").upper()
    return ""


def _is_special_object(item: dict[str, Any]) -> bool:
    object_signals = set(recall.collectible_signals(item)) - {"best offer"}
    if object_signals:
        return True
    best = item.get("best_recognition")
    if isinstance(best, dict) and best.get("collectible_format_evidence"):
        return True
    reasons = " ".join(str(value) for value in item.get("opportunity_reasons") or []).lower()
    return any(
        term in reasons
        for term in (
            "signed by the photographer",
            "numbered copy",
            "original print",
            "artist-proof",
            "limited or special edition",
            "association copy",
        )
    )


def priority_band(item: dict[str, Any]) -> int:
    score = int(item.get("opportunity_score") or 0)
    price = recall._landed_price(item)
    tier = _collectibility_tier(item)
    special = _is_special_object(item)
    under_100 = price is not None and price <= 100
    under_300 = price is not None and price <= 300

    if score >= 90:
        return 0
    if under_100 and (special or tier in {"S", "A"}):
        return 1
    if item.get("material_change") is True and under_300:
        return 2
    if item.get("recall_first_unknown") is True:
        return 2
    if under_100:
        return 3
    if special and under_300:
        return 4
    if tier in {"S", "A"} and under_300:
        return 5
    if under_300:
        return 6
    return 7


def priority_key(item: dict[str, Any]) -> tuple[Any, ...]:
    price = recall._landed_price(item)
    return (
        priority_band(item),
        -int(item.get("opportunity_score") or 0),
        price if price is not None else 999999.0,
        0 if item.get("live_verified") is True else 1,
        monitor.pb.normalize(item.get("title")),
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

    return sorted(by_key.values(), key=priority_key), search_only_keys


def _verification_line(item: dict[str, Any], detected_at: str) -> str:
    if item.get("alert_verification") == "LIVE VERIFIED" or item.get("live_verified") is True:
        checked = str(item.get("live_verified_at") or detected_at)
        return f"- **Verification:** LIVE VERIFIED at {checked}"
    observed = str(item.get("search_observed_at") or detected_at)
    return (
        f"- **Verification:** SEARCH RESULT ONLY at {observed}. "
        "This was returned by the eBay search but availability was not rechecked."
    )


def _heading_label(item: dict[str, Any], urgent_threshold: int) -> str:
    if int(item.get("opportunity_score") or 0) >= urgent_threshold:
        return "URGENT"
    if item.get("material_change") is True:
        return "CHANGED"
    if item.get("recall_first_unknown") is True:
        return "DISCOVERY"
    return "REVIEW"


def make_issue_body(
    items: list[dict[str, Any]],
    *,
    detected_at: str,
    stats: dict[str, Any],
    failures: list[str],
    urgent_threshold: int,
) -> str:
    ordered = sorted(items, key=priority_key)
    live_count = sum(
        1
        for item in ordered
        if item.get("alert_verification") == "LIVE VERIFIED" or item.get("live_verified") is True
    )
    search_only_count = len(ordered) - live_count
    lines = [
        "## New private-seller eBay photobook opportunities",
        "",
        f"Detected at **{detected_at}** by the private-seller discovery engine.",
        f"Recognition library: **{stats.get('records', '?')} books**.",
        f"This packet contains **{live_count} live-verified** and **{search_only_count} search-result-only** candidates.",
        "",
        "Candidates are ordered for fast triage: urgent scores first, then sub-£100 special or canonical books, materially improved listings, cheap unknown books, and lower-priority review leads.",
        "Search-result-only candidates are deliberately surfaced without a second item-detail check so limited verification cannot hide a fast-moving bargain.",
        "The score is a discovery priority, not a purchase verdict. ChatGPT should still verify exact edition, printing, completeness, condition, delivery cost, current market value and current availability before recommending a purchase.",
        "",
    ]

    for item in ordered:
        score = int(item.get("opportunity_score") or 0)
        lines.extend(
            [
                f"### {_heading_label(item, urgent_threshold)} {score}/100 - {item.get('title') or 'Untitled listing'}",
                "",
                f"- **Observed price:** {monitor._price_line(item)}",
                f"- **Private seller:** {item.get('vendor') or 'eBay individual account'}",
                f"- **Collector lane:** {item.get('collecting_lane') or 'open discovery'}",
                f"- **Opportunity type:** {item.get('opportunity_kind') or 'review lead'}",
                f"- **Discovery lane:** {item.get('search_lane') or 'unknown'}",
                f"- **Search:** `{item.get('search_query') or ''}`",
                f"- **Buying format:** {', '.join(item.get('buying_options') or []) or 'not returned'}",
                _verification_line(item, detected_at),
            ]
        )
        if item.get("material_change") is True:
            change_text = "; ".join(str(value) for value in item.get("material_change_reasons") or [])
            lines.append(f"- **Material change:** {change_text or 'listing materially improved'}")
        if item.get("recall_first_unknown") is True:
            lines.append(
                "- **Unknown-book lane:** not recognised by the 4,318-book library, but cheap enough to receive human review instead of automatic rejection"
            )
        lines.extend(
            [
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
        recall.record_seen_recall(seen, item, detected_at)
        pending.pop(key, None)
    state["seen"] = monitor._trim_seen(seen)


def _packet_title(chunk: list[dict[str, Any]], index: int, total: int) -> str:
    top_score = max(int(item.get("opportunity_score") or 0) for item in chunk)
    under_100 = sum(
        1 for item in chunk if recall._landed_price(item) is not None and recall._landed_price(item) <= 100
    )
    special = sum(1 for item in chunk if _is_special_object(item))
    changed = sum(1 for item in chunk if item.get("material_change") is True)
    unknown = sum(1 for item in chunk if item.get("recall_first_unknown") is True)
    label = "HOT" if any(priority_band(item) <= 2 for item in chunk) else "REVIEW"
    metrics = [
        f"top {top_score}",
        f"{under_100} under £100",
    ]
    if special:
        metrics.append(f"{special} special")
    if changed:
        metrics.append(f"{changed} changed")
    if unknown:
        metrics.append(f"{unknown} unknown")
    title = f"EBAY_PRIVATE_NEW: {label} {len(chunk)} | " + " | ".join(metrics)
    if total > 1:
        title += f" | {index}/{total}"
    return title[:240]


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
    ordered = sorted(candidates, key=priority_key)
    chunks = [
        ordered[index:index + ISSUE_CHUNK_SIZE]
        for index in range(0, len(ordered), ISSUE_CHUNK_SIZE)
    ]
    for index, chunk in enumerate(chunks, start=1):
        stem = alerts_dir / f"issue-{index:03d}"
        stem.with_suffix(".title").write_text(
            _packet_title(chunk, index, len(chunks)) + "\n",
            encoding="utf-8",
        )
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
