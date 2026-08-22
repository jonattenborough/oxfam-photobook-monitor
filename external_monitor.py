#!/usr/bin/env python3
"""Monitor public external charity/dealer pages for newly surfaced photobook candidates."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

SOURCES: list[dict[str, Any]] = [
    {
        "id": "ebay_bhf_books",
        "source_name": "British Heart Foundation eBay",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/britishheartfoundationshop/BOOKS/_i.html?store_cat=3893944012&_sop=10",
        "alert_all": False,
    },
    {
        "id": "ebay_bhf_vintage_collectable",
        "source_name": "British Heart Foundation eBay Vintage & Collectable",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/britishheartfoundationshop/VINTAGE-COLLECTABLE/_i.html?store_cat=3893943012&_sop=10",
        "alert_all": False,
    },
    {
        "id": "ebay_red_cross",
        "source_name": "British Red Cross eBay",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/britishredcross?_nkw=book&_sop=10",
        "alert_all": False,
    },
    {
        "id": "ebay_scope",
        "source_name": "Scope eBay",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/scope?_nkw=book&_sop=10",
        "alert_all": False,
    },
    {
        "id": "ebay_marie_curie",
        "source_name": "Marie Curie eBay",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/mariecurieshop?_nkw=book&_sop=10",
        "alert_all": False,
    },
    {
        "id": "ebay_sue_ryder",
        "source_name": "Sue Ryder Pre-loved eBay Books",
        "kind": "ebay",
        "url": "https://www.ebay.co.uk/str/sueryderpreloved/Books/_i.html?store_cat=21266820018&_sop=10",
        "alert_all": False,
    },
    {
        "id": "wob_photography",
        "source_name": "World of Books Photography",
        "kind": "collection",
        "url": "https://www.worldofbooks.com/en-gb/collections/photography-books?sort_by=created-descending",
        "domain": "www.worldofbooks.com",
        "path_markers": ("/products/", "/books/"),
        "alert_all": True,
    },
    {
        "id": "wob_rare_art_photo",
        "source_name": "World of Books Old & Rare Art, Fashion & Photography",
        "kind": "collection",
        "url": "https://www.worldofbooks.com/en-gb/collections/rare-art-fashion-photography-books?sort_by=created-descending",
        "domain": "www.worldofbooks.com",
        "path_markers": ("/products/", "/books/"),
        "alert_all": True,
    },
    {
        "id": "awesome_art_photo",
        "source_name": "Awesome Books Art, Fashion & Photography",
        "kind": "collection",
        "url": "https://www.awesomebooks.com/books/category/1/art-fashion-photography",
        "domain": "www.awesomebooks.com",
        "path_markers": ("/book/",),
        "alert_all": False,
    },
]

DIRECT_PHOTO_TERMS = [
    "photograph", "photography", "photobook", "photo book", "photo-book",
    "photographs", "photojournal", "camera", "darkroom", "contact sheet",
    "magnum", "aperture", "street photography", "documentary photography",
]

VISUAL_ART_TERMS = [
    "art", "artist", "architecture", "architectural", "fashion", "design",
    "portrait", "portraits", "images", "illustrated", "exhibition", "catalogue",
    "catalog", "monograph", "portfolio", "visual", "typography",
]

EDITION_TERMS = [
    "signed", "inscribed", "first edition", "1st edition", "first printing",
    "1st printing", "limited edition", "numbered", "edition of", "artist proof",
    "artist's proof", "original print", "with print", "slipcase", "slip case",
    "glassine", "acetate",
]

PUBLISHER_TERMS = [
    "steidl", "mack", "scalo", "phaidon", "taschen", "dew i lewis",
    "twin palms", "powerhouse", "schirmer", "delpire", "lustrum",
]

TARGET_TERMS = [
    "robert frank", "william eggleston", "stephen shore", "larry clark",
    "nan goldin", "sally mann", "martin parr", "richard billingham",
    "jim goldberg", "alec soth", "richard avedon", "robert mapplethorpe",
    "mary ellen mark", "susan meiselas", "ed van der elsken", "bruce weber",
    "corinne day", "walker evans", "roy decarava", "josef koudelka",
    "bernd becher", "hilla becher", "new topographics", "nicholas nixon",
    "mike mandel", "larry sultan", "luigi ghirri", "lewis baltz",
    "diane arbus", "chris killip", "paul graham", "irving penn",
    "peter beard", "bruce gilden", "daido moriyama", "nobuyoshi araki",
    "anders petersen", "bill brandt", "don mccullin", "henri cartier-bresson",
    "robert adams", "joel meyerowitz", "garry winogrand", "gary winogrand",
    "lee friedlander", "ralph gibson", "helmut newton", "guy bourdin",
    "peter hujar", "francesca woodman", "vivian maier", "saul leiter",
    "masahisa fukase", "shomei tomatsu", "andreas gursky", "thomas struth",
    "thomas ruff", "wolfgang tillmans", "juergen teller", "juergen schadeberg",
    "mitch epstein", "harry gruyaert", "fay godwin", "eikoh hosoe",
    "sebastiao salgado", "sebstião salgado", "brassaï", "brassai",
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href\s*=\s*([\"'])(?P<href>.*?)(?:\1)[^>]*>(?P<body>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
PRICE_RE = re.compile(r"£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
EBAY_ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(?P<id>\d{9,15})(?:[/?#]|$)", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<script\b.*?</script>", " ", value)
    value = re.sub(r"(?is)<style\b.*?</style>", " ", value)
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value)
    return WS_RE.sub(" ", value).strip()


def request_html(url: str, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"HTTP {status} from {url}")
                text = raw.decode("utf-8", errors="replace")
                if len(text) < 1000:
                    raise RuntimeError(f"Unexpectedly short HTML from {url}")
                return text
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_error}")


def context_from_html(page_html: str, start: int, width: int = 1800) -> str:
    lo = max(0, start - 150)
    hi = min(len(page_html), start + width)
    return strip_html(page_html[lo:hi])[:900]


def parse_price(context: str) -> float | None:
    match = PRICE_RE.search(context)
    if not match:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def plausible(item: dict[str, Any]) -> bool:
    text = " ".join([
        str(item.get("title") or ""),
        str(item.get("context") or ""),
    ]).lower()
    if any(term in text for term in TARGET_TERMS):
        return True
    if any(term in text for term in DIRECT_PHOTO_TERMS):
        return True
    if any(term in text for term in PUBLISHER_TERMS):
        return True
    visual = any(term in text for term in VISUAL_ART_TERMS)
    edition = any(term in text for term in EDITION_TERMS)
    return visual and edition


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item["key"])
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        old_title = str(existing.get("title") or "")
        new_title = str(item.get("title") or "")
        if len(new_title) > len(old_title):
            existing["title"] = new_title
        if existing.get("price_gbp") is None and item.get("price_gbp") is not None:
            existing["price_gbp"] = item["price_gbp"]
        if len(str(item.get("context") or "")) > len(str(existing.get("context") or "")):
            existing["context"] = item.get("context")
    return list(by_key.values())


def parse_ebay(source: dict[str, Any], page_html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in ANCHOR_RE.finditer(page_html):
        href = html.unescape(match.group("href"))
        item_match = EBAY_ITEM_RE.search(href)
        if not item_match:
            continue
        item_id = item_match.group("id")
        title = strip_html(match.group("body"))
        if len(title) < 4:
            title = ""
        context = context_from_html(page_html, match.start())
        if not title:
            candidate = re.sub(r"\s+(?:Opens in a new window or tab|See details.*)$", "", context)
            title = candidate[:240]
        items.append({
            "key": f"{source['id']}:{item_id}",
            "external_id": item_id,
            "source_id": source["id"],
            "source_name": source["source_name"],
            "title": title[:300],
            "price_gbp": parse_price(context),
            "url": f"https://www.ebay.co.uk/itm/{item_id}",
            "source_page": source["url"],
            "context": context,
        })
    return dedupe_items(items)[:120]


def collection_href_allowed(source: dict[str, Any], href: str) -> bool:
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(source["url"], href))
    source_domain = str(source.get("domain") or "").lower()
    if source_domain and parsed.netloc.lower() not in {source_domain, source_domain.removeprefix("www.")}:
        if parsed.netloc.lower().removeprefix("www.") != source_domain.removeprefix("www."):
            return False
    path = parsed.path.lower()
    return any(marker in path for marker in source.get("path_markers", ()))


def parse_collection(source: dict[str, Any], page_html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in ANCHOR_RE.finditer(page_html):
        href = html.unescape(match.group("href"))
        if not collection_href_allowed(source, href):
            continue
        absolute = urllib.parse.urljoin(source["url"], href)
        parsed = urllib.parse.urlsplit(absolute)
        canonical = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        title = strip_html(match.group("body"))
        context = context_from_html(page_html, match.start())
        if len(title) < 4:
            continue
        lowered = title.lower()
        if lowered in {"view product", "add to basket", "wishlist this", "see more", "image"}:
            continue
        items.append({
            "key": f"{source['id']}:{parsed.path.rstrip('/')}",
            "external_id": parsed.path.rstrip("/"),
            "source_id": source["id"],
            "source_name": source["source_name"],
            "title": title[:300],
            "price_gbp": parse_price(context),
            "url": canonical,
            "source_page": source["url"],
            "context": context,
        })
    return dedupe_items(items)[:120]


def fetch_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    page_html = request_html(source["url"])
    if source["kind"] == "ebay":
        items = parse_ebay(source, page_html)
    else:
        items = parse_collection(source, page_html)
    if not items:
        raise RuntimeError("page fetched but no listing/product links were parsed")
    return items


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sources": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("External monitor state is not a JSON object")
    payload.setdefault("version", 1)
    payload.setdefault("sources", {})
    return payload


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def set_output(name: str, value: str) -> None:
    target = os.getenv("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def trim_seen(seen: dict[str, Any], max_items: int = 5000) -> dict[str, Any]:
    if len(seen) <= max_items:
        return seen
    rows = []
    for key, value in seen.items():
        stamp = ""
        if isinstance(value, dict):
            stamp = str(value.get("last_seen") or value.get("first_seen") or "")
        rows.append((stamp, key, value))
    rows.sort(reverse=True)
    return {key: value for _, key, value in rows[:max_items]}


def make_issue_body(items: list[dict[str, Any]], detected_at: str, failures: list[str]) -> str:
    lines = [
        "## New external photobook-radar listings",
        "",
        f"Detected at **{detected_at}** by the external public-page monitor.",
        "",
        "These are newly surfaced items only. Existing inventory was silently baselined on each source's first successful fetch.",
        "ChatGPT should verify exact editions, condition, completeness and market value before any email alert is sent.",
        "",
    ]
    for item in items:
        lines += [
            f"### {item.get('title') or 'Untitled listing'}",
            "",
            f"- **Source:** {item['source_name']}",
        ]
        if item.get("price_gbp") is not None:
            lines.append(f"- **Observed price:** £{item['price_gbp']:.2f}")
        lines += [
            f"- **Product URL:** {item['url']}",
            f"- **Source page:** {item['source_page']}",
        ]
        if item.get("context"):
            lines.append(f"- **Page context:** {str(item['context'])[:850]}")
        lines.append("")
    if failures:
        lines += [
            "### Source warnings",
            "",
            "Some sources were temporarily unavailable on this run. Their existing state was left untouched:",
        ]
        lines.extend(f"- {failure}" for failure in failures)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="data/external_state.json")
    parser.add_argument("--runtime-dir", default="runtime/external")
    args = parser.parse_args()

    state_path = Path(args.state)
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)

    state = load_state(state_path)
    sources_state = state["sources"]
    if not isinstance(sources_state, dict):
        raise RuntimeError("External monitor sources state is not an object")

    detected_at = utc_now()
    all_new: list[dict[str, Any]] = []
    failures: list[str] = []
    successes = 0
    state_changed = False

    for source in SOURCES:
        source_id = source["id"]
        try:
            items = fetch_source(source)
        except Exception as exc:
            warning = f"{source['source_name']}: {exc}"
            failures.append(warning)
            print("WARNING:", warning, file=sys.stderr)
            continue

        successes += 1
        source_state = sources_state.get(source_id)
        if not isinstance(source_state, dict) or not source_state.get("initialized"):
            seen = {
                item["key"]: {
                    "first_seen": detected_at,
                    "last_seen": detected_at,
                    "title": item.get("title"),
                    "url": item.get("url"),
                }
                for item in items
            }
            sources_state[source_id] = {
                "initialized": True,
                "first_successful_fetch": detected_at,
                "last_successful_fetch": detected_at,
                "last_count": len(items),
                "seen": seen,
            }
            state_changed = True
            print(f"{source['source_name']}: baseline seeded with {len(items)} current items.")
            continue

        seen = source_state.setdefault("seen", {})
        if not isinstance(seen, dict):
            seen = {}
            source_state["seen"] = seen

        source_new = 0
        for item in items:
            key = item["key"]
            previous = seen.get(key)
            if previous is None:
                seen[key] = {
                    "first_seen": detected_at,
                    "last_seen": detected_at,
                    "title": item.get("title"),
                    "url": item.get("url"),
                }
                state_changed = True
                if source.get("alert_all") or plausible(item):
                    all_new.append(item)
                    source_new += 1
            elif isinstance(previous, dict):
                previous["last_seen"] = detected_at

        source_state["seen"] = trim_seen(seen)
        source_state["last_successful_fetch"] = detected_at
        source_state["last_count"] = len(items)
        if source_new:
            print(f"{source['source_name']}: {source_new} new plausible candidate(s).")
        else:
            print(f"{source['source_name']}: no new plausible candidates.")

    if successes == 0:
        raise RuntimeError("All external sources failed; refusing to update state")

    state["sources"] = sources_state
    state["last_successful_run"] = detected_at
    state["last_successful_sources"] = successes
    state["last_failed_sources"] = failures
    write_json(runtime / "proposed-state.json", state)

    if all_new:
        write_json(runtime / "new-items.json", all_new)
        source_labels: list[str] = []
        for item in all_new:
            name = str(item["source_name"])
            if name not in source_labels:
                source_labels.append(name)
        label = ", ".join(source_labels[:3])
        if len(source_labels) > 3:
            label += f" +{len(source_labels) - 3}"
        issue_title = f"EXTERNAL_NEW: {len(all_new)} listing{'s' if len(all_new) != 1 else ''} | {label}"
        (runtime / "issue-title.txt").write_text(issue_title + "\n", encoding="utf-8")
        (runtime / "issue-body.md").write_text(
            make_issue_body(all_new, detected_at, failures), encoding="utf-8"
        )

    set_output("new_count", str(len(all_new)))
    set_output("state_changed", "true" if state_changed else "false")
    set_output("successful_sources", str(successes))
    set_output("failed_sources", str(len(failures)))
    print(
        f"External fetch complete: {successes}/{len(SOURCES)} sources succeeded; "
        f"{len(all_new)} new plausible listings."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
