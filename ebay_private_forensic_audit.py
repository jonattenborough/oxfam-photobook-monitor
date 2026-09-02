#!/usr/bin/env python3
"""Offline, score-independent audit of losslessly captured eBay search hits."""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path
from typing import Any, Iterable

import parr_badger_runner as pb

BOOK_CATEGORY_ID = "261186"
REPORT_LIMIT = 300

STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "de",
    "der",
    "di",
    "du",
    "for",
    "from",
    "in",
    "la",
    "le",
    "of",
    "on",
    "photographs",
    "photography",
    "the",
    "to",
    "und",
    "with",
}

BOOK_EVIDENCE = re.compile(
    r"\b(?:book|photobook|monograph|hardback|hardcover|paperback|softcover|"
    r"dust\s+jacket|isbn|first\s+edition|1st\s+edition|first\s+printing|"
    r"published|publisher|pages?|catalogue|catalog|volume|signed\s+copy)\b",
    re.I,
)

STRONG_BOOK_EVIDENCE = re.compile(
    r"\b(?:book|photobook|monograph|hardback|hardcover|paperback|softcover|"
    r"dust\s+jacket|isbn|first\s+edition|1st\s+edition|first\s+printing|"
    r"published|publisher|catalogue|catalog|volume|signed\s+copy)\b",
    re.I,
)

ABSOLUTE_NONBOOK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("clippings", re.compile(r"\bclippings?\b", re.I)),
    ("clothing", re.compile(r"\b(?:t[ -]?shirt|sweatshirt|hoodie|jacket\s+size)\b", re.I)),
    ("calendar", re.compile(r"\bcalendar\b", re.I)),
    ("disc", re.compile(r"\b(?:dvd|blu[ -]?ray|cd-rom)\b", re.I)),
    ("memorabilia", re.compile(r"\b(?:trading\s+card|sticker|badge|mug|jigsaw)\b", re.I)),
)

CONDITIONAL_NONBOOK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("postcard", re.compile(r"\bpostcards?\b", re.I)),
    ("poster", re.compile(r"\bposters?\b", re.I)),
    ("loose print", re.compile(r"\b(?:art\s+print|photographic\s+print|photo\s+print|gicl[eé]e|screenprint)\b", re.I)),
    ("loose photograph", re.compile(r"\b(?:press\s+photo(?:graph)?|original\s+photo(?:graph)?|signed\s+photo(?:graph)?)\b", re.I)),
    ("invitation", re.compile(r"\b(?:private\s+view|exhibition)\s+invitation\b", re.I)),
    ("magazine", re.compile(r"\bmagazine\b", re.I)),
)

OBJECT_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("signed claim", re.compile(r"\b(?:signed|autograph(?:ed)?|inscribed)\b", re.I)),
    ("first-edition claim", re.compile(r"\b(?:first|1st)\s+(?:edition|printing|impression)\b", re.I)),
    ("limited/numbered claim", re.compile(r"\b(?:limited\s+edition|numbered|edition\s+of\s+\d+)\b", re.I)),
    ("print component claim", re.compile(r"\b(?:with|includes?|plus)\s+(?:an?\s+)?(?:signed\s+)?(?:original\s+)?print\b", re.I)),
    ("sealed claim", re.compile(r"\b(?:sealed|shrink[ -]?wrap(?:ped)?)\b", re.I)),
    ("out-of-print claim", re.compile(r"\b(?:out\s+of\s+print|oop)\b", re.I)),
)

REISSUE_PATTERN = re.compile(
    r"\b(?:second|2nd|third|3rd|revised|anniversary|facsimile|reissue|reprint)\s+(?:edition|printing)?\b",
    re.I,
)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return payload


def load_chunk(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("searches"), list):
        raise RuntimeError(f"Invalid forensic chunk: {path}")
    return payload


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in pb.normalize(value).split()
        if len(token) > 1 and token not in STOPWORDS
    }


def _aliases(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _coverage(wanted: set[str], available: set[str]) -> float:
    if not wanted:
        return 0.0
    return len(wanted & available) / len(wanted)


def _best_title_coverage(target: dict[str, Any], available: set[str]) -> float:
    options = [str(target.get("title") or ""), *_aliases(target.get("title_aliases"))]
    return max((_coverage(_tokens(value), available) for value in options), default=0.0)


def _best_contributor_coverage(target: dict[str, Any], available: set[str]) -> float:
    options = [str(target.get("contributor") or ""), *_aliases(target.get("contributor_aliases"))]
    return max((_coverage(_tokens(value), available) for value in options), default=0.0)


def _phrase_present(phrase: Any, text: Any) -> bool:
    wanted = pb.normalize(phrase)
    haystack = pb.normalize(text)
    return bool(wanted and len(wanted) >= 4 and wanted in haystack)


def match_target(item: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    title_text = str(item.get("title") or "")
    full_text = " ".join((title_text, str(item.get("context") or "")))
    available = _tokens(full_text)
    author_coverage = _best_contributor_coverage(target, available)
    title_coverage = _best_title_coverage(target, available)
    contributor_exact = _phrase_present(target.get("contributor"), full_text)
    title_exact = _phrase_present(target.get("title"), title_text)

    if title_exact and (contributor_exact or author_coverage >= 0.5):
        strength = "exact"
        points = 36
    elif author_coverage >= 0.67 and title_coverage >= 0.67:
        strength = "strong"
        points = 31
    elif (title_exact and title_coverage >= 0.67) or (author_coverage >= 0.5 and title_coverage >= 0.45):
        strength = "moderate"
        points = 23
    elif author_coverage >= 0.5 or title_coverage >= 0.67:
        strength = "weak"
        points = 12
    else:
        strength = "incidental"
        points = 0

    return {
        "strength": strength,
        "points": points,
        "author_coverage": round(author_coverage, 3),
        "title_coverage": round(title_coverage, 3),
        "contributor_exact": contributor_exact,
        "title_exact": title_exact,
    }


def _book_evidence(item: dict[str, Any]) -> bool:
    if str(item.get("category_id") or "") == BOOK_CATEGORY_ID:
        return True
    text = " ".join((str(item.get("title") or ""), str(item.get("context") or "")))
    return bool(BOOK_EVIDENCE.search(text))


def obvious_nonbook(item: dict[str, Any], target: dict[str, Any], match: dict[str, Any]) -> tuple[bool, list[str]]:
    title = str(item.get("title") or "")
    full_text = " ".join((title, str(item.get("context") or "")))
    has_book_evidence = _book_evidence(item)
    has_strong_book_evidence = bool(STRONG_BOOK_EVIDENCE.search(full_text)) or str(
        item.get("category_id") or ""
    ) == BOOK_CATEGORY_ID
    exact_expected_object = bool(match.get("title_exact") and match.get("contributor_exact"))
    reasons: list[str] = []

    for label, pattern in ABSOLUTE_NONBOOK_PATTERNS:
        if pattern.search(title) and not has_strong_book_evidence:
            reasons.append(label)
    for label, pattern in CONDITIONAL_NONBOOK_PATTERNS:
        if pattern.search(full_text) and not has_book_evidence and not exact_expected_object:
            reasons.append(label)

    return bool(reasons), sorted(set(reasons))


def _price(item: dict[str, Any]) -> float:
    raw = item.get("landed_price_gbp")
    if raw is None:
        raw = item.get("price_gbp")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 999999.0


def _number(target: dict[str, Any], key: str) -> float | None:
    try:
        value = float(target.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def object_signals(item: dict[str, Any]) -> list[str]:
    text = " ".join((str(item.get("title") or ""), str(item.get("context") or "")))
    return [label for label, pattern in OBJECT_SIGNAL_PATTERNS if pattern.search(text)]


def _tier_points(target: dict[str, Any]) -> int:
    return {"S": 44, "A": 34, "B": 23, "C": 12}.get(
        str(target.get("collectibility_tier") or "").upper(),
        6,
    )


def _price_points(price: float) -> int:
    if price <= 10:
        return 25
    if price <= 20:
        return 22
    if price <= 40:
        return 18
    if price <= 75:
        return 14
    if price <= 100:
        return 10
    if price <= 150:
        return 6
    if price <= 300:
        return 2
    return -50


def evaluate(item: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    match = match_target(item, target)
    nonbook, nonbook_reasons = obvious_nonbook(item, target, match)
    signals = object_signals(item)
    price = _price(item)
    tier = str(target.get("collectibility_tier") or "").upper()
    priority = _tier_points(target) + int(match["points"]) + _price_points(price)
    reasons = [f"{match['strength']} author/title match", f"tier {tier or 'unknown'} target"]

    canon = str(target.get("canon_sources") or "").strip()
    if canon:
        priority += 7
        reasons.append("record has canon or specialist-source evidence")
    if str(target.get("first_monograph") or "").upper() == "YES":
        priority += 7
        reasons.append("first-monograph target")
    if str(target.get("documentary_relevance") or "").upper() == "HIGH":
        priority += 5
        reasons.append("high documentary relevance")
    if str(target.get("special_edition_priority") or "").upper() == "HIGH":
        priority += 7
        reasons.append("high special-edition priority")

    signal_points = {
        "signed claim": 8,
        "first-edition claim": 7,
        "limited/numbered claim": 8,
        "print component claim": 9,
        "sealed claim": 2,
        "out-of-print claim": 3,
    }
    for signal in signals:
        priority += signal_points.get(signal, 0)
    if signals:
        reasons.append("listing claims " + ", ".join(signals))

    bargain = _number(target, "bargain_gbp")
    strong_buy = _number(target, "strong_buy_gbp")
    if bargain is not None and price <= bargain:
        priority += 20
        reasons.append("at or below curated bargain benchmark")
    elif strong_buy is not None and price <= strong_buy:
        priority += 12
        reasons.append("at or below curated strong-buy benchmark")

    reissue = bool(REISSUE_PATTERN.search(" ".join((str(item.get("title") or ""), str(item.get("context") or "")))))
    if reissue:
        priority -= 14
        reasons.append("explicit later-edition or reissue wording")
    if nonbook:
        priority -= 100
        reasons.append("obvious non-book evidence: " + ", ".join(nonbook_reasons))

    tier_cap = {"S": 300.0, "A": 250.0, "B": 150.0, "C": 75.0}.get(tier, 50.0)
    strength = str(match["strength"])
    review = False
    if not nonbook and price <= 300:
        if strength in {"exact", "strong"} and price <= tier_cap:
            review = True
        elif strength == "moderate" and tier in {"S", "A"} and price <= 175:
            review = True
        elif strength in {"moderate", "weak"} and price <= 25 and tier in {"S", "A", "B"}:
            review = True
        elif signals and float(match.get("author_coverage") or 0) >= 0.5 and price <= min(200, tier_cap):
            review = True
        elif bargain is not None and price <= bargain and strength != "incidental":
            review = True

    return {
        "audit_priority": priority,
        "match": match,
        "obvious_nonbook": nonbook,
        "nonbook_reasons": nonbook_reasons,
        "object_signals": signals,
        "explicit_reissue_wording": reissue,
        "review": review,
        "reasons": reasons,
    }


def prior_statuses(state: dict[str, Any], findings: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    findings_items = findings.get("items") if isinstance(findings.get("items"), dict) else {}
    for key in findings_items:
        statuses[str(key)] = "surfaced"
    pending = state.get("pending_live") if isinstance(state.get("pending_live"), dict) else {}
    for key in pending:
        statuses.setdefault(str(key), "pending_live_check")
    reviewed = state.get("reviewed") if isinstance(state.get("reviewed"), dict) else {}
    for key in reviewed:
        statuses.setdefault(str(key), "reviewed_not_surfaced")
    return statuses


def audit_chunks(
    chunk_paths: Iterable[Path],
    *,
    existing_state: dict[str, Any] | None = None,
    existing_findings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    statuses = prior_statuses(existing_state or {}, existing_findings or {})
    combined: dict[str, dict[str, Any]] = {}
    query_count = 0
    raw_hits = 0
    truncated_queries: list[dict[str, Any]] = []

    for path in chunk_paths:
        chunk = load_chunk(path)
        query_count += int(chunk.get("query_count") or len(chunk["searches"]))
        raw_hits += int(chunk.get("result_count") or 0)
        truncated_queries.extend(
            item for item in chunk.get("truncated_queries") or [] if isinstance(item, dict)
        )
        for search in chunk["searches"]:
            if not isinstance(search, dict):
                continue
            target = search.get("target") if isinstance(search.get("target"), dict) else {}
            for item in search.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "")
                if not key:
                    continue
                evaluation = evaluate(item, target)
                target_match = {
                    "target": target,
                    **evaluation,
                    "search_query": str(search.get("query") or ""),
                }
                current = combined.setdefault(
                    key,
                    {
                        "key": key,
                        "item": dict(item),
                        "prior_status": statuses.get(key, "unseen_by_scored_monitor"),
                        "target_matches": [],
                    },
                )
                current["target_matches"].append(target_match)

    candidates: list[dict[str, Any]] = []
    for current in combined.values():
        matches = sorted(
            current["target_matches"],
            key=lambda value: int(value.get("audit_priority") or -999),
            reverse=True,
        )
        best = matches[0]
        compact_others = []
        for value in matches[1:6]:
            target = value.get("target") if isinstance(value.get("target"), dict) else {}
            compact_others.append(
                {
                    "target": {
                        key: target.get(key)
                        for key in ("record_id", "contributor", "title", "year", "collectibility_tier")
                    },
                    "audit_priority": value.get("audit_priority"),
                    "match": value.get("match"),
                    "search_query": value.get("search_query"),
                }
            )
        candidate = {
            **current["item"],
            "prior_status": current["prior_status"],
            "audit_priority": int(best.get("audit_priority") or 0),
            "review": any(bool(value.get("review")) for value in matches),
            "obvious_nonbook": all(bool(value.get("obvious_nonbook")) for value in matches),
            "best_target_match": best,
            "other_target_matches": compact_others,
            "matched_query_count": len(matches),
        }
        candidates.append(candidate)

    candidates.sort(
        key=lambda value: (
            0 if value.get("review") else 1,
            -int(value.get("audit_priority") or 0),
            _price(value),
            pb.normalize(value.get("title")),
        )
    )
    review_candidates = [value for value in candidates if value.get("review")]
    summary = {
        "queries_captured": query_count,
        "raw_hits": raw_hits,
        "unique_items": len(candidates),
        "duplicate_hits": max(0, raw_hits - len(candidates)),
        "truncated_query_count": len(truncated_queries),
        "truncated_queries": truncated_queries,
        "obvious_nonbooks": sum(bool(value.get("obvious_nonbook")) for value in candidates),
        "review_candidates": len(review_candidates),
        "surfaced_review_candidates": sum(
            value.get("prior_status") == "surfaced" for value in review_candidates
        ),
        "pending_review_candidates": sum(
            value.get("prior_status") == "pending_live_check" for value in review_candidates
        ),
        "independent_false_negative_candidates": sum(
            value.get("prior_status") in {"unseen_by_scored_monitor", "reviewed_not_surfaced"}
            for value in review_candidates
        ),
    }
    return summary, candidates


def _money(value: dict[str, Any]) -> str:
    price = _price(value)
    return "unknown" if price >= 999999 else f"£{price:.2f}"


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def make_report(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    false_negative_pool = [
        value
        for value in candidates
        if value.get("review")
        and value.get("prior_status") in {"unseen_by_scored_monitor", "reviewed_not_surfaced"}
    ]
    pending = [
        value
        for value in candidates
        if value.get("review") and value.get("prior_status") == "pending_live_check"
    ]
    lines = [
        "# Independent eBay full-library forensic audit",
        "",
        "This report is generated from the raw search summaries. It does not use the normal monitor's opportunity score or alert threshold.",
        "",
        f"- Library queries captured: **{summary['queries_captured']}**",
        f"- Raw search hits preserved: **{summary['raw_hits']}**",
        f"- Unique eBay items: **{summary['unique_items']}**",
        f"- Obvious non-book objects flagged: **{summary['obvious_nonbooks']}**",
        f"- Independent review pool: **{summary['review_candidates']}**",
        f"- Possible scored-monitor false negatives: **{summary['independent_false_negative_candidates']}**",
        f"- Queries capped at 200 results: **{summary['truncated_query_count']}**",
        "",
        "## Possible false negatives",
        "",
        "| Priority | Prior status | Target | Listing | Landed | Why |",
        "|---:|---|---|---|---:|---|",
    ]
    for candidate in false_negative_pool[:REPORT_LIMIT]:
        best = candidate["best_target_match"]
        target = best.get("target") or {}
        link = f"[{_escape(candidate.get('title'))}]({candidate.get('url')})"
        why = "; ".join(str(value) for value in (best.get("reasons") or [])[:3])
        lines.append(
            f"| {candidate.get('audit_priority')} | {_escape(candidate.get('prior_status'))} | "
            f"{_escape(target.get('contributor'))}, *{_escape(target.get('title'))}* | "
            f"{link} | {_money(candidate)} | {_escape(why)} |"
        )

    lines.extend(
        [
            "",
            "## Candidates awaiting completion of the original live-check queue",
            "",
            "| Priority | Target | Listing | Landed |",
            "|---:|---|---|---:|",
        ]
    )
    for candidate in pending[:100]:
        target = candidate["best_target_match"].get("target") or {}
        link = f"[{_escape(candidate.get('title'))}]({candidate.get('url')})"
        lines.append(
            f"| {candidate.get('audit_priority')} | {_escape(target.get('contributor'))}, "
            f"*{_escape(target.get('title'))}* | {link} | {_money(candidate)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default="data/ebay_private_forensic_chunks/*.json.gz")
    parser.add_argument("--existing-state", default="data/ebay_private_full_library_scan_state.json")
    parser.add_argument("--existing-findings", default="data/ebay_private_full_library_scan_findings.json")
    parser.add_argument("--summary", default="data/ebay_private_forensic_audit_summary.json")
    parser.add_argument("--candidates", default="data/ebay_private_forensic_audit_candidates.json")
    parser.add_argument("--report", default="data/ebay_private_forensic_audit_report.md")
    args = parser.parse_args()

    paths = sorted(Path().glob(args.chunks))
    if not paths:
        raise RuntimeError(f"No forensic chunks match {args.chunks}")
    state = load_json(Path(args.existing_state), {})
    findings = load_json(Path(args.existing_findings), {})
    summary, candidates = audit_chunks(paths, existing_state=state, existing_findings=findings)
    review_candidates = [value for value in candidates if value.get("review")]
    write_json(Path(args.summary), summary)
    write_json(Path(args.candidates), {"version": 1, "summary": summary, "items": review_candidates})
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(make_report(summary, candidates), encoding="utf-8")
    print(
        f"Forensic audit: {summary['raw_hits']} raw hits, {summary['unique_items']} unique items, "
        f"{summary['independent_false_negative_candidates']} possible false negatives."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
