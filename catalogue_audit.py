#!/usr/bin/env python3
"""Prepare a high-recall, human-reviewable Oxfam catalogue gem audit.

The live monitors remain new-stock only. This script is used by a separate
one-off workflow after a complete parent-category crawl. It evaluates every
live product, then creates a compact queue with two independent tracks:

* collection: important, collectible, canonical or strongly relevant books;
* cheap: genuinely interesting low-priced books useful in promotional baskets.

The deterministic score is only triage. Scheduled AI review verifies exact
editions, condition, availability and current market evidence before alerting.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import canon_runner as canon
from oxfam_parent_common import absolute_product_url, searchable_text, utc_now

DEFAULT_INPUT = Path("runtime/parent_full_scan/full_catalogue.json")
QUEUE_OUT = Path("data/oxfam_catalogue_audit_queue.json")
STATE_OUT = Path("data/oxfam_catalogue_audit_state.json")
RUNTIME = Path("runtime/catalogue_audit")

PHOTO_TERMS = {
    "photograph", "photography", "photographer", "photographic", "photobook",
    "photo book", "photographs by", "images by", "photojournal", "contact sheet",
}

MONOGRAPH_TERMS = {
    "monograph", "retrospective", "survey of", "photographs by", "photographic work",
    "photographic career", "previously unpublished photographs", "photo essay",
}

FIT_TERMS = {
    "documentary": 7, "street photography": 7, "social documentary": 9,
    "social history": 6, "photojournal": 6, "working class": 7,
    "working-class": 7, "post-war": 5, "urban": 4, "portrait": 4,
    "fashion photography": 5, "editorial": 3, "humanist": 7,
    "everyday life": 5, "communities": 4, "community": 3,
    "britain": 3, "british": 3, "ireland": 3, "irish": 3,
    "glasgow": 4, "liverpool": 4, "london": 2, "colour photography": 4,
    "color photography": 4, "landscape photography": 3,
}

EDITION_TERMS = {
    "original print": 22, "signed print": 22, "with print": 18,
    "association copy": 20, "artist proof": 16, "artist's proof": 16,
    "signed": 7, "inscribed": 9, "limited edition": 9, "numbered": 6,
    "edition of": 6, "first edition": 8, "1st edition": 8,
    "first printing": 10, "first impression": 10, "1st printing": 10,
    "slipcase": 5, "slip case": 5, "glassine": 5, "acetate": 4,
    "portfolio": 5,
}

PHOTO_PUBLISHERS = {
    "aperture", "steidl", "mack", "twin palms", "nazraeli", "scalo", "delpire",
    "dewi lewis", "hatje cantz", "schirmer", "twelvetrees", "lustrum",
    "grey editions", "promenade", "punto e virgola", "castelli graphics",
    "museum of modern art", "moma", "new york graphic society", "powerhouse",
    "phaidon", "thames & hudson", "thames and hudson", "kehrer", "contrasto",
    "charta", "damiani", "loose joints", "skinnerboox", "void", "stanley/barker",
    "stanley barker", "gost", "morel", "superlabo", "akio nagasawa", "shashin",
    "sokyusha", "little brown mushroom", "bluecoat press", "café royal books",
    "cafe royal books", "photoworks", "birlinn", "cornerhouse", "dew i lewis",
}

# This is deliberately broader than the user's canonical target list. It helps
# surface major and mid-career photographers whose inexpensive surveys or first
# monographs can be excellent collection additions.
SIGNIFICANT_PHOTOGRAPHERS = {
    "ansel adams", "robert adams", "diane arbus", "richard avedon", "brassaï",
    "brassai", "bill brandt", "henri cartier-bresson", "larry clark", "corinne day",
    "roy decarava", "william eggleston", "walker evans", "robert frank",
    "lee friedlander", "bruce gilden", "luigi ghirri", "jim goldberg", "paul graham",
    "ken grant", "eikoh hosoe", "peter hujar", "chris killip", "william klein",
    "josef koudelka", "saul leiter", "sally mann", "mary ellen mark",
    "robert mapplethorpe", "susan meiselas", "joel meyerowitz", "daido moriyama",
    "nobuyoshi araki", "nan goldin", "don mccullin", "martin parr", "irving penn",
    "anders petersen", "mark power", "alec soth", "stephen shore", "shomei tomatsu",
    "ed van der elsken", "bruce weber", "gary winogrand", "francesca woodman",
    "nick waplington", "tom wood", "tish murtha", "masahisa fukase", "lewis baltz",
    "bernd becher", "hilla becher", "raymond depardon", "raghu rai", "rene burri",
    "rené burri", "eve arnold", "philip jones griffiths", "bert hardy", "roger mayne",
    "tony ray-jones", "shirley baker", "joan eardley", "oscar marzaroli",
    "norman parkinson", "sophy rickett", "john davies", "chris steele-perkins",
    "tom stoddart", "tom hunter", "daniel meadows", "chris floyd", "martin chambi",
    "graciela iturbide", "rinko kawauchi", "hiroshi sugimoto", "kikuji kawada",
    "takuma nakahira", "rieko shiga", "helen levitt", "vivian maier", "alex webb",
    "rebecca norris webb", "trent parke", "narelle autio", "bill henson",
    "gregory halpern", "vanessa winship", "simon roberts", "markéta luskačová",
    "marketa luskacova", "tomio seike", "sarah moon", "guy bourdin", "helmut newton",
    "paolo roversi", "peter lindbergh", "tim walker", "nadav kander", "sian davey",
}

TECHNICAL_TERMS = {
    "digital photography handbook", "photography handbook", "camera manual",
    "complete guide to photography", "beginner's guide", "beginners guide",
    "how to photograph", "how to take", "teach yourself", "photoshop", "lightroom",
    "exposure made easy", "photography for dummies", "camera techniques",
    "photographic techniques", "mastering digital", "field guide to photography",
}

ART_ONLY_TERMS = {
    "oil painting", "watercolour", "watercolor", "how to draw", "drawing techniques",
    "ceramics", "pottery", "sculpture", "needlework", "embroidery", "quilting",
    "decorative arts", "furniture", "interior design",
}


def contains_any(text: str, terms: Iterable[str]) -> list[str]:
    return sorted(term for term in terms if term in text)


class CanonMatcher:
    """Fast indexed wrapper around the existing Parr/Badger and Roth matcher."""

    def __init__(self) -> None:
        self.rows = list(canon.load_canon_master())
        self.index: dict[str, set[int]] = defaultdict(set)
        for index, row in enumerate(self.rows):
            for token in set(row.get("_title_tokens") or set()):
                if len(token) >= 3:
                    self.index[token].add(index)

    def match(self, item: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
        listing_title = canon.pb.normalize(item.get("title"))
        title_author = canon.pb.normalize(" ".join(str(item.get(key) or "") for key in (
            "title", "author"
        )))
        listing_title_tokens = set(listing_title.split())
        title_author_tokens = set(title_author.split())
        candidates: set[int] = set()
        for token in listing_title_tokens:
            candidates.update(self.index.get(token, set()))
        matches: list[dict[str, Any]] = []
        for index in candidates:
            row = self.rows[index]
            book_title = str(row.get("_title_norm") or "")
            book_tokens = set(row.get("_title_tokens") or set())
            contributor_tokens = set(row.get("_contributor_tokens") or set())
            contributor_hit = bool(contributor_tokens & title_author_tokens)
            exact = bool(book_title and book_title in listing_title)
            coverage = len(book_tokens & listing_title_tokens) / len(book_tokens) if book_tokens else 0.0
            ratio = difflib.SequenceMatcher(None, book_title, listing_title).ratio() if listing_title else 0.0
            short_generic = len(book_tokens) <= 2 and len(book_title) < 16

            # For historical catalogue triage, references buried in a long
            # description are not canon matches. Require the listed title itself
            # to agree, with contributor confirmation for short/generic titles.
            if exact and (contributor_hit or not short_generic):
                score, reason = (100 if contributor_hit else 96), "exact listed title"
            elif contributor_hit and coverage >= 0.85 and ratio >= 0.50:
                score, reason = 92, "listed-title tokens + contributor"
            elif contributor_hit and ratio >= 0.86:
                score, reason = 91, "strong listed-title fuzzy match + contributor"
            elif ratio >= 0.93 and len(book_title) >= 16:
                score, reason = 90, "very strong listed-title fuzzy match"
            else:
                continue
            matches.append({
                "score": score,
                "reason": reason,
                "volumes": row.get("Volumes") or "",
                "contributor": row.get("Contributor") or "",
                "title": row.get("Title") or "",
                "year": row.get("Year") or "",
                "publisher": row.get("Publisher") or "",
                "pb_refs": row.get("PB page / refs") or "",
                "search_tier": row.get("Search tier") or "",
                "roth_101": row.get("Roth 101") or ("Yes" if row.get("Volumes") == "R101" else ""),
            })
        matches.sort(key=lambda match: (
            int(match["score"]), str(match.get("search_tier") or "").upper() == "CORE"
        ), reverse=True)
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for match in matches:
            key = (canon.pb.normalize(match["contributor"]), canon.pb.normalize(match["title"]))
            if key in seen:
                continue
            seen.add(key)
            unique.append(match)
            if len(unique) >= limit:
                break
        return unique


def score_item(item: dict[str, Any], matcher: CanonMatcher) -> dict[str, Any]:
    text = searchable_text(item)
    title_author = " ".join(str(item.get(key) or "") for key in ("title", "author")).lower()
    price = item.get("price_gbp")
    price = float(price) if isinstance(price, (int, float)) else None

    canon_matches = matcher.match(item)
    photos = contains_any(text, PHOTO_TERMS)
    monographs = contains_any(text, MONOGRAPH_TERMS)
    photographers = contains_any(title_author, SIGNIFICANT_PHOTOGRAPHERS)
    publishers = contains_any(text, PHOTO_PUBLISHERS)
    technical = contains_any(text, TECHNICAL_TERMS)
    art_only = contains_any(title_author, ART_ONLY_TERMS)

    collection = 0
    cheap = 0
    reasons: list[str] = []

    if canon_matches:
        best = canon_matches[0]
        collection += 55 if str(best.get("search_tier") or "").upper() == "CORE" else 42
        if str(best.get("roth_101") or "").lower() == "yes" or best.get("volumes") == "R101":
            collection += 12
        reasons.append(f"canon match: {best['contributor']} / {best['title']}")

    if photographers:
        collection += 20
        cheap += 18
        reasons.append("significant photographer: " + ", ".join(photographers[:2]))

    if photos:
        strength = min(16, 7 + 2 * (len(photos) - 1))
        collection += strength
        cheap += min(13, strength)
        reasons.append("photography signal: " + ", ".join(photos[:3]))

    if monographs:
        collection += min(10, 4 + 2 * (len(monographs) - 1))
        cheap += min(8, 3 + len(monographs))
        reasons.append("monograph/survey signal")

    if publishers:
        collection += min(14, 8 + 2 * (len(publishers) - 1))
        cheap += min(12, 7 + 2 * (len(publishers) - 1))
        reasons.append("photobook publisher: " + ", ".join(publishers[:2]))

    fit_hits = [(term, weight) for term, weight in FIT_TERMS.items() if term in text]
    if fit_hits:
        fit_score = min(20, sum(weight for _, weight in fit_hits))
        collection += fit_score
        cheap += min(10, math.ceil(fit_score / 2))
        reasons.append("collection fit: " + ", ".join(term for term, _ in fit_hits[:3]))

    edition_hits = [(term, weight) for term, weight in EDITION_TERMS.items() if term in text]
    if edition_hits:
        edition_score = min(36, sum(weight for _, weight in edition_hits))
        collection += edition_score
        cheap += min(14, math.ceil(edition_score / 2))
        reasons.append("edition clues: " + ", ".join(term for term, _ in edition_hits[:4]))

    if price is not None:
        if price <= 5:
            cheap += 22
        elif price <= 10:
            cheap += 19
        elif price <= 15:
            cheap += 16
        elif price <= 20:
            cheap += 12
        elif price <= 30:
            cheap += 5
        if price <= 50:
            collection += 5
        elif price <= 100:
            collection += 2

    if technical:
        penalty = 28
        collection -= penalty
        cheap -= penalty
        reasons.append("technical/manual penalty")

    # Art books often mention photography only because works are illustrated.
    # Do not apply this penalty to canon records or known photographers.
    if art_only and not canon_matches and not photographers:
        collection -= 18
        cheap -= 18
        reasons.append("non-photography art-book penalty")

    # A listing that never identifies a photographer, photographic practice,
    # recognised publisher or canon record is too weak for either audit track.
    hard_photo_signal = bool(canon_matches or photographers or publishers or photos)
    if not hard_photo_signal:
        collection = min(collection, 0)
        cheap = min(cheap, 0)

    tracks: list[str] = []
    if canon_matches or collection >= 36:
        tracks.append("collection")
    if price is not None and price <= 20 and cheap >= 34:
        tracks.append("cheap")

    return {
        **item,
        "url": item.get("url") or absolute_product_url(item),
        "collection_score": max(0, collection),
        "cheap_score": max(0, cheap),
        "tracks": tracks,
        "audit_reasons": reasons,
        "canon_matches": canon_matches,
    }


def compact_candidate(item: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "sku", "title", "author", "price_gbp", "description", "condition", "publisher",
        "isbn", "year", "format", "edition", "pages", "creation_date", "url",
        "collection_score", "cheap_score", "tracks", "audit_reasons", "canon_matches",
    }
    result = {key: item.get(key) for key in keep if item.get(key) not in (None, "", [], {})}
    if result.get("description"):
        result["description"] = re.sub(r"\s+", " ", str(result["description"])).strip()[:1800]
    return result


def select_queue(
    scored: list[dict[str, Any]], max_collection: int, max_cheap: int
) -> list[dict[str, Any]]:
    collection = sorted(
        (item for item in scored if "collection" in item["tracks"]),
        key=lambda item: (-item["collection_score"], item.get("price_gbp") or 999999),
    )[:max_collection]
    cheap = sorted(
        (item for item in scored if "cheap" in item["tracks"]),
        key=lambda item: (-item["cheap_score"], -item["collection_score"], item.get("price_gbp") or 999999),
    )[:max_cheap]

    selected: dict[str, dict[str, Any]] = {}
    for item in collection + cheap:
        selected[item["sku"]] = item
    queue = list(selected.values())
    queue.sort(key=lambda item: (
        not bool(item.get("canon_matches")),
        -max(item["collection_score"], item["cheap_score"]),
        item.get("price_gbp") or 999999,
    ))
    return [compact_candidate(item) for item in queue]


def candidate_markdown(number: int, item: dict[str, Any]) -> list[str]:
    price = item.get("price_gbp")
    display_price = f"£{price:.2f}" if isinstance(price, (int, float)) else "price unavailable"
    effective = f"£{price * 0.70:.2f}" if isinstance(price, (int, float)) else "unknown"
    lines = [
        f"### {number}. {item.get('title') or item.get('sku')}",
        "",
        f"- **Track:** {', '.join(item.get('tracks') or [])}",
        f"- **Oxfam price:** {display_price}",
        f"- **Effective price in a qualifying 30% basket:** {effective}",
        f"- **Audit scores:** collection {item.get('collection_score', 0)}, cheap {item.get('cheap_score', 0)}",
        f"- **SKU:** `{item.get('sku')}`",
        f"- **Link:** {item.get('url')}",
    ]
    for label, key in (
        ("Author/photographer", "author"), ("Publisher", "publisher"), ("Year", "year"),
        ("Format", "format"), ("Edition", "edition"), ("ISBN", "isbn"),
        ("Condition", "condition"),
    ):
        if item.get(key):
            lines.append(f"- **{label}:** {item[key]}")
    if item.get("audit_reasons"):
        lines.append(f"- **Triage reasons:** {'; '.join(item['audit_reasons'])}")
    for match in item.get("canon_matches") or []:
        label = "Roth 101" if match.get("volumes") == "R101" else f"Parr/Badger V{match.get('volumes')}"
        if str(match.get("roth_101") or "").lower() == "yes" and match.get("volumes") != "R101":
            label += " + Roth 101"
        lines.append(
            f"- **Canon lead:** {label}, {match.get('contributor')}, *{match.get('title')}*, match {match.get('score')}/100"
        )
    if item.get("description"):
        lines += ["", str(item["description"])]
    lines.append("")
    return lines


def write_issues(queue: list[dict[str, Any]], generated_at: str, batch_size: int) -> None:
    issues = RUNTIME / "issues"
    issues.mkdir(parents=True, exist_ok=True)
    total_batches = math.ceil(len(queue) / batch_size) if queue else 0
    audit_id = generated_at.replace(":", "").replace("-", "")[:15]

    report = [
        "## Oxfam full catalogue gem audit",
        "",
        f"Audit ID: `{audit_id}`",
        f"Fresh catalogue snapshot: **{generated_at}**",
        f"Queued for deep review: **{len(queue)}** candidates across **{total_batches}** batches.",
        "",
        "This report collects only verified finds from the separate historical catalogue audit. Live new-listing alerts remain independent.",
        "",
        "### Review tracks",
        "",
        "- **collection:** canonical, collectible, historically important, scarce or strongly matched to Jon's collection.",
        "- **cheap:** worthwhile books at £20 or below, including useful additions to qualifying Oxfam promotional baskets.",
        "",
        "Scheduled review will add verified hits as comments and close this report with a ranked final summary.",
    ]
    (RUNTIME / "report.title").write_text(
        f"OXFAM_CATALOGUE_AUDIT_REPORT: {audit_id}\n", encoding="utf-8"
    )
    (RUNTIME / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    for batch_index in range(total_batches):
        batch = queue[batch_index * batch_size:(batch_index + 1) * batch_size]
        start = batch_index * batch_size + 1
        lines = [
            "## Oxfam catalogue audit review batch",
            "",
            f"Audit ID: `{audit_id}`",
            f"Batch: **{batch_index + 1}/{total_batches}**",
            f"Queue positions: **{start}-{start + len(batch) - 1}**",
            "",
            "These are triage leads, not verified recommendations. Check current availability, exact edition, condition, completeness, photographic significance and realistic active-market comparisons.",
            "",
        ]
        for local_index, item in enumerate(batch, start=start):
            lines.extend(candidate_markdown(local_index, item))
        stem = issues / f"batch-{batch_index + 1:03d}"
        stem.with_suffix(".title").write_text(
            f"OXFAM_CATALOGUE_AUDIT: {audit_id} batch {batch_index + 1:03d}/{total_batches:03d}\n",
            encoding="utf-8",
        )
        stem.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def prepare(args: argparse.Namespace) -> int:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise RuntimeError(f"No products found in {args.input}")

    matcher = CanonMatcher()
    scored = [score_item(dict(item), matcher) for item in products if isinstance(item, dict)]
    queue = select_queue(scored, args.max_collection, args.max_cheap)
    generated_at = str(payload.get("generated_at") or utc_now())

    snapshot = {
        "version": 1,
        "generated_at": generated_at,
        "source_products": len(products),
        "max_collection": args.max_collection,
        "max_cheap": args.max_cheap,
        "queue_count": len(queue),
        "collection_count": sum("collection" in (item.get("tracks") or []) for item in queue),
        "cheap_count": sum("cheap" in (item.get("tracks") or []) for item in queue),
        "candidates": queue,
    }
    QUEUE_OUT.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_OUT.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    STATE_OUT.write_text(json.dumps({
        "version": 1,
        "generated_at": generated_at,
        "queue_count": len(queue),
        "status": "issues_pending",
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_issues(queue, generated_at, args.batch_size)
    print(
        f"Catalogue audit prepared: {len(products)} products evaluated, {len(queue)} queued, "
        f"collection={snapshot['collection_count']}, cheap={snapshot['cheap_count']}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    result.add_argument("--max-collection", type=int, default=800)
    result.add_argument("--max-cheap", type=int, default=400)
    result.add_argument("--batch-size", type=int, default=30)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.max_collection < 1 or args.max_cheap < 1 or args.batch_size < 1:
        raise SystemExit("Limits and batch size must be positive")
    return prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
