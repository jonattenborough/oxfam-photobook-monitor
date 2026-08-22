#!/usr/bin/env python3
"""One-time current-stock audit against the Parr/Badger master.

This is deliberately separate from the normal new-listing state. It searches
current AbeBooks stock for a shard of the master, identifies likely pricing
anomalies and edition-signalled copies, and writes a compact issue payload for
AI verification. Shards allow all master records to be swept in parallel.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any

import market_monitor as market
from parr_badger_runner import normalize


def set_output(name: str, value: Any) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    return normalize(row.get("Contributor")), normalize(row.get("Title"))


def is_target_match(item: dict[str, Any], row: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    wanted = row_key(row)
    matches = market.matches(item)
    for match in matches:
        got = normalize(match.get("contributor")), normalize(match.get("title"))
        if got == wanted and int(match.get("score") or 0) >= 85:
            item["parr_badger_matches"] = matches
            return True, match
    return False, matches[0] if matches else None


def candidate_score(item: dict[str, Any], median_price: float | None, match: dict[str, Any]) -> tuple[int, list[str]]:
    score = int(match.get("score") or 0)
    reasons: list[str] = []
    tier = str(match.get("search_tier") or "").upper()
    if tier == "CORE":
        score += 12
        reasons.append("Parr/Badger CORE")
    signals = market.signals(item)
    if signals:
        score += min(18, 4 * len(signals))
        reasons.append("edition clues: " + ", ".join(signals[:4]))
    price = item.get("price_gbp")
    if isinstance(price, (int, float)):
        if price <= 25:
            score += 20
            reasons.append("price <= £25")
        elif price <= 50:
            score += 14
            reasons.append("price <= £50")
        elif price <= 100:
            score += 8
            reasons.append("price <= £100")
        elif price <= 150:
            score += 4
            reasons.append("price <= £150")
        if median_price and median_price >= 20 and price <= median_price * 0.65 and median_price - price >= 15:
            discount = round(100 * (1 - price / median_price))
            score += 24
            reasons.append(f"about {discount}% below live AbeBooks median")
    return score, reasons


def select_candidates(items: list[dict[str, Any]], row: dict[str, Any]) -> list[dict[str, Any]]:
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in items:
        ok, match = is_target_match(item, row)
        if ok and match:
            matched.append((item, match))
    prices = [float(item["price_gbp"]) for item, _ in matched if isinstance(item.get("price_gbp"), (int, float))]
    median_price = statistics.median(prices) if prices else None
    out: list[dict[str, Any]] = []
    for item, match in matched:
        score, reasons = candidate_score(item, median_price, match)
        price = item.get("price_gbp")
        signals = market.signals(item)
        anomaly = isinstance(price, (int, float)) and median_price and median_price >= 20 and price <= median_price * 0.65 and median_price - price >= 15
        cheap = isinstance(price, (int, float)) and price <= (175 if str(match.get("search_tier") or "").upper() == "CORE" else 100)
        edition = bool(signals) and (not isinstance(price, (int, float)) or price <= 250)
        if not (anomaly or cheap or edition):
            continue
        row_out = dict(item)
        row_out["initial_sweep_score"] = score
        row_out["initial_sweep_reasons"] = reasons
        row_out["live_matching_count"] = len(matched)
        row_out["live_median_gbp"] = round(float(median_price), 2) if median_price is not None else None
        row_out["target_contributor"] = row.get("Contributor") or ""
        row_out["target_title"] = row.get("Title") or ""
        row_out["target_volumes"] = row.get("Volumes") or ""
        row_out["target_tier"] = row.get("Search tier") or ""
        out.append(row_out)
    out.sort(key=lambda x: int(x.get("initial_sweep_score") or 0), reverse=True)
    return out[:2]


def make_issue(candidates: list[dict[str, Any]], shard: int, shards: int, checked: int, failures: list[str]) -> tuple[str, str]:
    title = f"INITIAL_SWEEP: Parr/Badger current market shard {shard + 1}/{shards} | {len(candidates)} candidates"
    lines = [
        "## One-time Parr/Badger current-market audit",
        "",
        f"Shard **{shard + 1}/{shards}** checked **{checked}** master records against current AbeBooks stock.",
        "",
        "This is not a new-listing alert. These are current copies selected because they are cheap, carry edition/signature clues, or appear materially below the live price distribution for the same Parr/Badger title. Every candidate still needs exact-edition, condition and market-value verification.",
        "",
    ]
    for item in candidates:
        price = item.get("price_gbp")
        lines += [f"### {item.get('title') or item.get('target_title')}", ""]
        lines.append(f"- **Parr/Badger target:** V{item.get('target_volumes') or '?'} {item.get('target_tier') or ''} | {item.get('target_contributor')} | {item.get('target_title')}")
        if isinstance(price, (int, float)):
            lines.append(f"- **Observed price:** £{price:.2f}")
        if isinstance(item.get("live_median_gbp"), (int, float)):
            lines.append(f"- **Live matching-result median:** £{item['live_median_gbp']:.2f} across {item.get('live_matching_count')} matching result(s)")
        lines.append(f"- **Discovery score:** {item.get('initial_sweep_score')}")
        if item.get("initial_sweep_reasons"):
            lines.append("- **Why surfaced:** " + "; ".join(item["initial_sweep_reasons"]))
        lines.append(f"- **Listing:** {item.get('url')}")
        if item.get("context"):
            lines.append(f"- **Listing context:** {str(item['context'])[:1400]}")
        lines.append("")
    if failures:
        lines += ["### Search warnings", ""]
        lines.extend(f"- {x}" for x in failures[:20])
        if len(failures) > 20:
            lines.append(f"- plus {len(failures) - 20} additional query failures")
        lines.append("")
    return title[:240], "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=16)
    parser.add_argument("--runtime-dir", default="runtime/initial_sweep")
    args = parser.parse_args()
    if args.shard < 0 or args.shard >= args.shards:
        raise SystemExit("invalid shard")

    rows = market.master_rows()
    selected = [row for i, row in enumerate(rows) if i % args.shards == args.shard]
    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    successful = 0

    for row in selected:
        try:
            items = market.fetch_target("abebooks", row)
            successful += 1
            candidates.extend(select_candidates(items, row))
        except Exception as exc:
            failures.append(f"{row.get('Contributor')} / {row.get('Title')}: {exc}")

    candidates.sort(key=lambda x: int(x.get("initial_sweep_score") or 0), reverse=True)
    candidates = candidates[:20]
    runtime = Path(args.runtime_dir) / f"shard-{args.shard:02d}"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "results.json").write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    title, body = make_issue(candidates, args.shard, args.shards, len(selected), failures)
    (runtime / "issue-title.txt").write_text(title + "\n", encoding="utf-8")
    (runtime / "issue-body.md").write_text(body, encoding="utf-8")
    set_output("candidate_count", len(candidates))
    set_output("successful_queries", successful)
    set_output("failed_queries", len(failures))
    print(f"Initial sweep shard {args.shard + 1}/{args.shards}: {len(selected)} records, {successful} successful queries, {len(failures)} failures, {len(candidates)} surfaced candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
