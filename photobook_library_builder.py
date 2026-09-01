#!/usr/bin/env python3
"""Build a source-backed specialist-publisher photobook library snapshot.

The live monitor reads the generated CSV and never calls Open Library. This
script is an occasional, reproducible maintenance tool for refreshing the
checked-in snapshot from Open Library's public-domain catalogue.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import parr_badger_runner as pb

OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
USER_AGENT = "photobook-recognition-library/2.0 (github.com/jonattenborough/oxfam-photobook-monitor)"
PAGE_SIZE = 100
CSV_FIELDS = [
    "Record ID",
    "Contributor",
    "Contributor aliases",
    "Title",
    "Title aliases",
    "Year",
    "Publisher",
    "ISBN",
    "Canon sources",
    "Collectibility tier",
    "Search priority",
    "First edition notes",
    "Strong buy GBP",
    "Bargain GBP",
    "Evidence confidence",
    "Source",
    "Search tier",
]
EXCLUDED_TITLE_PHRASES = {
    "beginner's guide",
    "beginners guide",
    "business of photography",
    "camera manual",
    "complete guide to digital photography",
    "digital photography for dummies",
    "digital photography",
    "digital photography handbook",
    "how to photograph",
    "how to take photographs",
    "lightroom",
    "marketing for photographers",
    "mastering digital photography",
    "photoshop",
    "photographer's market",
    "photographers market",
    "photography for beginners",
    "photography for dummies",
    "posing techniques",
    "teach yourself photography",
    "wedding photography handbook",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if _clean(item)]


def _record_identity(row: dict[str, str]) -> tuple[str, str]:
    contributor_tokens = sorted(
        token
        for token in pb.normalize(row.get("Contributor")).split()
        if len(token) >= 2 and token not in {"and", "by", "et", "al"}
    )
    return " ".join(contributor_tokens), pb.normalize(row.get("Title"))


def excluded_title(title: str) -> bool:
    normalized = pb.normalize(title)
    if len(normalized) < 2:
        return True
    return any(pb.normalize(phrase) in normalized for phrase in EXCLUDED_TITLE_PHRASES)


def record_from_doc(doc: dict[str, Any], publisher: dict[str, Any]) -> dict[str, str] | None:
    work_key = _clean(doc.get("key"))
    title = _clean(doc.get("title"))
    authors = _list(doc.get("author_name"))
    if not work_key.startswith("/works/") or not title or not authors or excluded_title(title):
        return None

    first_year = doc.get("first_publish_year")
    year = str(first_year) if isinstance(first_year, int) and 1800 <= first_year <= 2100 else ""
    contributor = authors[0]
    aliases = " | ".join(dict.fromkeys(authors[1:4]))
    source_url = f"https://openlibrary.org{work_key}"
    name = _clean(publisher.get("name"))
    return {
        "Record ID": f"openlibrary:{work_key.rsplit('/', 1)[-1]}",
        "Contributor": contributor,
        "Contributor aliases": aliases,
        "Title": title,
        "Title aliases": "",
        "Year": year,
        "Publisher": name,
        "ISBN": "",
        "Canon sources": f"Specialist photography publisher backlist: {name}",
        "Collectibility tier": _clean(publisher.get("tier")) or "C",
        "Search priority": str(publisher.get("priority", 5)),
        "First edition notes": "Work-level metadata only; verify the exact publisher edition and printing.",
        "Strong buy GBP": "",
        "Bargain GBP": "",
        "Evidence confidence": "Medium",
        "Source": source_url,
        "Search tier": "BROAD",
    }


def search_url(publisher: dict[str, Any], page: int) -> str:
    query_name = _clean(publisher.get("query") or publisher.get("name"))
    query = f'publisher:"{query_name}" subject:photography'
    params = {
        "q": query,
        "fields": "key,title,author_name,first_publish_year",
        "limit": PAGE_SIZE,
        "page": page,
    }
    return f"{OPEN_LIBRARY_SEARCH}?{urllib.parse.urlencode(params)}"


def fetch_json(url: str, *, timeout: float, attempts: int = 3) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("Open Library returned a non-object response")
            return payload
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Open Library request failed after {attempts} attempts: {last_error}")


def fetch_publisher(publisher: dict[str, Any], *, timeout: float) -> list[dict[str, str]]:
    maximum = max(0, int(publisher.get("max_records") or 0))
    if not maximum:
        return []
    rows: list[dict[str, str]] = []
    page = 1
    while len(rows) < maximum:
        payload = fetch_json(search_url(publisher, page), timeout=timeout)
        docs = payload.get("docs")
        if not isinstance(docs, list) or not docs:
            break
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            row = record_from_doc(doc, publisher)
            if row is not None:
                rows.append(row)
                if len(rows) >= maximum:
                    break
        if len(docs) < PAGE_SIZE:
            break
        page += 1
    return rows


def merge_records(groups: Iterable[Iterable[dict[str, str]]]) -> list[dict[str, str]]:
    by_work: dict[str, dict[str, str]] = {}
    by_identity: dict[tuple[str, str], str] = {}
    for rows in groups:
        for row in rows:
            work_id = row["Record ID"]
            identity = _record_identity(row)
            existing_id = work_id if work_id in by_work else by_identity.get(identity)
            if existing_id is None:
                by_work[work_id] = dict(row)
                by_identity[identity] = work_id
                continue
            existing = by_work[existing_id]
            sources = [value.strip() for value in f"{existing['Canon sources']} | {row['Canon sources']}".split("|")]
            existing["Canon sources"] = " | ".join(dict.fromkeys(value for value in sources if value))
            if not existing.get("Year") and row.get("Year"):
                existing["Year"] = row["Year"]
    return sorted(
        by_work.values(),
        key=lambda row: (int(row["Search priority"]), pb.normalize(row["Contributor"]), pb.normalize(row["Title"])),
    )


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="data/photobook_recognition/openlibrary_publishers.json")
    parser.add_argument("--output", default="data/photobook_recognition/openlibrary_publisher_backlists.csv")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    publishers = config.get("publishers") if isinstance(config, dict) else None
    if not isinstance(publishers, list) or not publishers:
        raise RuntimeError("Open Library publisher config must contain a non-empty publishers list")

    if any(not isinstance(publisher, dict) or not _clean(publisher.get("name")) for publisher in publishers):
        raise RuntimeError("Every publisher entry must be an object with a name")

    groups: list[list[dict[str, str]]] = []
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as executor:
        futures = {
            executor.submit(fetch_publisher, publisher, timeout=args.timeout): publisher
            for publisher in publishers
        }
        for future in concurrent.futures.as_completed(futures):
            publisher = futures[future]
            try:
                rows = future.result()
            except Exception as exc:
                warning = f"{publisher['name']}: {exc}"
                failures.append(warning)
                print(f"WARNING: {warning}")
                continue
            groups.append(rows)
            print(f"{publisher['name']}: {len(rows)} retained records")

    if failures:
        raise RuntimeError(
            f"{len(failures)} publisher request(s) failed; refusing to replace the complete snapshot: {failures}"
        )

    merged = merge_records(groups)
    minimum_records = max(1, int(config.get("minimum_records") or 1))
    if len(merged) < minimum_records:
        raise RuntimeError(
            f"Only {len(merged)} records were built, below the configured minimum of {minimum_records}; "
            f"failures={failures}"
        )
    write_csv(Path(args.output), merged)
    print(f"Wrote {len(merged)} de-duplicated Open Library records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
