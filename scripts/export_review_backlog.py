#!/usr/bin/env python3
"""Export open photobook review packets as deduplicated, spreadsheet-safe CSVs.

Read-only: this script does not change issues, listings, or monitor state.
Run from the repository root with ``python scripts/export_review_backlog.py``.
Set GH_TOKEN or GITHUB_TOKEN for higher GitHub API limits. For a saved API
snapshot, pass ``--issues-json open_issues.json`` instead of fetching issues.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
QUEUES = (
    "EBAY_PRIVATE_NEW", "EXTERNAL_NEW", "OXFAM_ART_NEW", "OXFAM_NEW",
    "CHARITY_NEW", "ENDGAME_EARLY", "ENDGAME_4H",
)
QUEUE_FILENAMES = {
    "EBAY_PRIVATE_NEW": "ebay_private.csv",
    "EXTERNAL_NEW": "external_market.csv",
    "OXFAM_ART_NEW": "oxfam_art_photography.csv",
    "OXFAM_NEW": "oxfam_photography.csv",
    "CHARITY_NEW": "ebay_charity.csv",
    "ENDGAME_EARLY": "endgame_early.csv",
    "ENDGAME_4H": "endgame_4h.csv",
}
FIELD = re.compile(r"(?m)^- \*\*([^*]+):\*\*\s*(.*)$")
SECTION = re.compile(r"^### ([^\n]+)(?:\n|$).*?(?=^### |\Z)", re.M | re.S)
EBAY_ID = re.compile(r"/itm/(?:[^/\s?#]+/)?(\d{9,15})(?:[/?#]|$)", re.I)
SKU = re.compile(r"\bHD_{1,2}(\d{6,})\b", re.I)
MONEY = re.compile(r"(?<![\w])(?:(GBP|EUR|USD|AUD|CAD)\s*|([£€$]))\s*([\d,]+(?:\.\d{1,2})?)")
ISBN_FIELD = re.compile(r"\bISBN(?:s|[- ]?(?:10|13))?\b[^:]{0,24}:\s*([\dXx /-]{10,42})", re.I)
HEAD_SCORE = re.compile(r"^(?:[A-Z ]+ )?(\d{1,3})/100\s*-\s*")
END_AT = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\b")
WARNING_HEADINGS = {"source warnings", "seller warnings", "temporary search warnings"}
CSV_FIELDS = (
    "queue", "queues", "listing_title", "photographer", "seller", "marketplace",
    "price", "postage", "total_price", "currency", "listing_url", "listing_id",
    "isbn", "edition_details", "matched_target", "recognition_tier", "discovery_score",
    "deadline_utc", "auction_status", "listing_status", "review_status",
    "first_seen_utc", "last_seen_utc", "source_issue", "source_issue_numbers",
    "source_issue_url", "duplicate_count", "image_url", "discovery_notes", "my_notes",
)


def get_json_pages(url: str, token: str = "") -> list[dict]:
    """Follow REST Link pagination without GitHub Search's 1,000-result cap."""
    result = []
    seen = set()
    while url:
        if url in seen or len(seen) > 200:
            raise RuntimeError("GitHub pagination loop")
        seen.add(url)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "photobook-backlog-csv-export"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers=headers), timeout=90) as response:
                    page = json.load(response)
                    link = response.headers.get("Link", "")
                break
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                if attempt == 3 or isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"GitHub fetch failed: {url} ({exc})") from exc
                time.sleep(2 ** attempt)
        if not isinstance(page, list):
            raise ValueError(f"Expected a list from {url}")
        result.extend(page)
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else ""
    return result


def issue_queue(issue: dict) -> str:
    title = str(issue.get("title") or "")
    return next((name for name in QUEUES if title.startswith(name + ":")), "")


def review_status(issue: dict, owner: str, cache: dict, token: str, online: bool) -> str:
    if not int(issue.get("comments") or 0):
        return "unreviewed"
    cached = cache.get(str(issue["number"])) or {}
    if cached.get("signature") == [issue.get("updated_at"), issue.get("comments")]:
        return "reviewed" if cached.get("receipt") else "unreviewed"
    if not online:
        return "review_unverified"
    try:
        comments = get_json_pages(issue["comments_url"] + "?per_page=100", token)
    except (RuntimeError, ValueError):
        return "review_unverified"
    return "reviewed" if any(
        str(c.get("body") or "").startswith("CHATGPT_GEM_REVIEWED:")
        and str((c.get("user") or {}).get("login") or "").lower() == owner.lower()
        for c in comments
    ) else "unreviewed"


def clean_url(url: str) -> str:
    url = str(url or "").strip().strip("<>")
    if not url.startswith(("https://", "http://")):
        return ""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "_skw", "hash"}]
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/",
                       urlencode(sorted(query)), ""))


def listing_key(url: str, sku: str, issue_number: int, ordinal: int) -> tuple[str, str]:
    clean = clean_url(url)
    if sku:
        m = SKU.search(sku)
        if m:
            return "oxfam:" + m.group(1), "HD_" + m.group(1)
    host = (urlsplit(clean).hostname or "").lower()
    if re.search(r"(?:^|\.)ebay\.", host):
        m = EBAY_ID.search(urlsplit(clean).path)
        if m:
            return "ebay:" + m.group(1), m.group(1)
    if "abebooks." in host:
        m = re.search(r"/(\d{8,15})/bd(?:$|/)", urlsplit(clean).path, re.I)
        if m:
            return "abebooks:" + m.group(1), m.group(1)
    if "biblio." in host:
        m = re.search(r"/d/(\d+)(?:$|/)", urlsplit(clean).path)
        if m:
            return "biblio:" + m.group(1), m.group(1)
    if clean:
        return "url:" + clean, ""
    return f"issue:{issue_number}:{ordinal}", ""


def pick(fields: dict[str, str], *names: str) -> str:
    return next((fields[name] for name in names if fields.get(name)), "")


def parse_money(value: str) -> tuple[str, str, str, str]:
    matches = MONEY.findall(value or "")
    if not matches:
        return "", "", "", ""
    def unpack(item):
        code, symbol, amount = item
        return (code or {"£": "GBP", "€": "EUR", "$": "USD"}.get(symbol, "")), float(amount.replace(",", ""))
    currency, price = unpack(matches[0])
    postage = ""
    if len(matches) > 1 and "plus" in (value or "").lower():
        shipping_currency, shipping_amount = unpack(matches[1])
        if shipping_currency == currency:
            postage = f"{shipping_amount:.2f}"
    total = f"{price + float(postage):.2f}" if postage else ""
    return f"{price:.2f}", postage, total, currency


def isbns(section: str) -> str:
    found = []
    for match in ISBN_FIELD.finditer(section):
        for token in re.findall(r"\b(?:\d[\d-]{8,16}[\dXx]|\d{10}|\d{13})\b", match.group(1)):
            normalized = token.replace("-", "").upper()
            if len(normalized) in (10, 13) and normalized not in found:
                found.append(normalized)
    return "; ".join(found)


def parse_issue(issue: dict, queue: str, now: datetime) -> list[dict]:
    body = str(issue.get("body") or "")
    rows = []
    for ordinal, match in enumerate(SECTION.finditer(body), 1):
        section = match.group(0)
        heading = match.group(1).strip()
        if heading.lower() in WARNING_HEADINGS:
            continue
        fields = {name.strip(): value.strip() for name, value in FIELD.findall(section)}
        heading_url = ""
        linked = re.fullmatch(r"\[(.+)\]\((https?://\S+)\)", heading)
        if linked:
            heading, heading_url = linked.groups()
        score = HEAD_SCORE.match(heading)
        if score:
            heading = heading[score.end():]
        url = pick(fields, "Listing", "Product URL", "Possible product URL") or heading_url
        sku = fields.get("SKU", "").strip("` ")
        key, listing_id = listing_key(url, sku, issue["number"], ordinal)
        # Preserve malformed or underdescribed candidate sections for manual review.
        if not fields and not heading_url:
            continue
        price, postage, total, currency = parse_money(pick(fields, "Observed price", "Oxfam price", "Bid/price"))
        recognition = pick(fields, "Best recognition", "Best Parr/Badger match", "Best library recognition", "Canon match", "Author/photographer")
        photographer = ""
        if recognition and not recognition.lower().startswith("none"):
            contributor = recognition.split("|")[1] if recognition.startswith("V") and "|" in recognition else recognition.split("|")[0]
            contributor = contributor.split(": ")[-1].strip()
            photographer = contributor.split(", *")[0].strip("* ")
        tier = re.search(r"\btier\s+([SABC123])\b", recognition + " " + fields.get("Core photographer priority", "") + " " + fields.get("Priority target", ""), re.I)
        if not tier:
            tier = re.search(r"\b(CORE|BROAD)\b", recognition)
        deadline = END_AT.search(fields.get("Ends", ""))
        auction_status = ""
        if queue.startswith("ENDGAME"):
            if deadline:
                end = datetime.fromisoformat(deadline.group().replace("Z", "+00:00"))
                auction_status = "expired" if end <= now else "upcoming"
            else:
                auction_status = "unknown_deadline"
        edition = "; ".join(filter(None, [pick(fields, name) for name in (
            "Edition evidence", "Edition clues", "Edition target note", "Known collectible variants", "Listing object evidence", "Condition",
        )]))
        note = "; ".join(filter(None, [pick(fields, name) for name in (
            "Why it surfaced", "Why surfaced", "Listing context", "API context", "Description", "Description excerpt", "Live description excerpt",
        )]))
        image = pick(fields, "Main image")
        if not image:
            image_match = re.search(r"!\[Listing image\]\((https?://[^)]+)\)", section)
            image = image_match.group(1) if image_match else ""
        verification = pick(fields, "Live verification", "Verification", "Live check").lower()
        listing_status = ("live_verified_at_detection" if "confirmed at" in verification or "live verified" in verification
                          else "search_result_only" if "search result only" in verification else "not_live_checked")
        rows.append({
            "_key": key, "queue": queue, "queues": queue, "listing_title": heading,
            "photographer": photographer, "seller": pick(fields, "Private seller", "Seller"),
            "marketplace": pick(fields, "Marketplace", "Source") or (urlsplit(url).hostname or ""),
            "price": price, "postage": postage, "total_price": total, "currency": currency,
            "listing_url": url, "listing_id": listing_id, "isbn": isbns(section),
            "edition_details": edition, "matched_target": pick(fields, "Priority target", "Best recognition", "Best Parr/Badger match", "Canon match"),
            "recognition_tier": tier.group(1).upper() if tier else "",
            "discovery_score": score.group(1) if score else re.sub(r"\D.*", "", pick(fields, "Endgame score", "Discovery priority")),
            "deadline_utc": deadline.group() if deadline else "", "auction_status": auction_status,
            "listing_status": listing_status, "review_status": "unreviewed",
            "first_seen_utc": issue["created_at"], "last_seen_utc": issue["created_at"],
            "source_issue": str(issue["number"]), "source_issue_numbers": str(issue["number"]),
            "source_issue_url": issue["html_url"], "duplicate_count": 1,
            "image_url": image, "discovery_notes": note,
            "my_notes": "",
        })
    return rows


def deduplicate(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[row["_key"]].append(row)
    result = []
    for group in groups.values():
        group.sort(key=lambda r: (r["last_seen_utc"], int(r["source_issue"])))
        latest = group[-1]
        combined = {field: next((r[field] for r in reversed(group) if r[field] != ""), "") for field in CSV_FIELDS}
        combined["first_seen_utc"] = group[0]["first_seen_utc"]
        combined["last_seen_utc"] = latest["last_seen_utc"]
        combined["source_issue"] = latest["source_issue"]
        combined["source_issue_url"] = latest["source_issue_url"]
        combined["source_issue_numbers"] = "; ".join(dict.fromkeys(r["source_issue"] for r in group))
        combined["queues"] = "; ".join(dict.fromkeys(r["queue"] for r in group))
        combined["duplicate_count"] = len(group)
        result.append(combined)
    result.sort(key=lambda r: (r["queue"], r["listing_title"].casefold(), r["listing_id"]))
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # CSV quoting alone does not stop spreadsheet formula execution.
            writer.writerow({k: "'" + str(v) if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v
                             for k, v in row.items() if k in CSV_FIELDS})


def export(issues: list[dict], output_dir: Path, repo: str, now: datetime,
           cache: dict | None = None, token: str = "", online: bool = False,
           legacy_issues: set[int] | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    skipped = Counter()
    observations = []
    for issue in issues:
        queue = issue_queue(issue)
        if not queue or "pull_request" in issue:
            continue
        counts[queue] += 1
        if queue == "EBAY_PRIVATE_NEW" and "FULL LIBRARY" in issue["title"]:
            skipped["historical_full_library"] += 1
            continue
        if queue == "EBAY_PRIVATE_NEW" and issue["number"] in (legacy_issues or set()):
            skipped["historical_legacy"] += 1
            continue
        status = review_status(issue, repo.split("/")[0], cache or {}, token, online)
        if status == "reviewed":
            skipped["reviewed_but_open"] += 1
            continue
        parsed = parse_issue(issue, queue, now)
        if not parsed:
            skipped["no_candidate_sections"] += 1
        for row in parsed:
            row["review_status"] = status
        observations.extend(parsed)
    unique = deduplicate(observations)
    files = []
    master = output_dir / "all_backlog.csv"
    write_csv(master, unique)
    files.append(master)
    by_queue = {}
    for queue in QUEUES:
        selected = [row for row in unique if queue in row["queues"].split("; ")]
        by_queue[queue] = len(selected)
        target = output_dir / QUEUE_FILENAMES[queue]
        write_csv(target, selected)
        files.append(target)
    expired = [r for r in unique if r["auction_status"] == "expired"]
    target = output_dir / "expired_auctions.csv"
    write_csv(target, expired)
    files.append(target)
    summary = {
        "as_of_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "repository": repo, "open_issues_fetched": len(issues),
        "queue_issue_counts": {q: counts[q] for q in QUEUES},
        "excluded_issue_counts": dict(skipped),
        "candidate_appearances": len(observations), "unique_listings": len(unique),
        "duplicates_removed": len(observations) - len(unique),
        "unique_rows_by_queue": by_queue,
        "expired_auction_rows": len(expired),
        "review_unverified_rows": sum(r["review_status"] == "review_unverified" for r in unique),
        "no_listing_url_rows": sum(not r["listing_url"] for r in unique),
        "limitations": [
            "Listings and prices are issue snapshots; current availability was not checked.",
            "Postage and total_price are blank unless both amounts appear in the same issue section.",
            "A listing seen in multiple queues appears once in the master and once per relevant queue CSV.",
            "Charity candidates that never became GitHub issues are absent from this issue export.",
        ],
    }
    summary_path = output_dir / "export_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    files.append(summary_path)
    readme = output_dir / "README.txt"
    readme.write_text(
        "Photobook review backlog\n"
        f"Exported {summary['as_of_utc']} from {repo}\n\n"
        "Start with all_backlog.csv. Each row is one distinct listing. Filter the queue, "
        "price, recognition_tier, auction_status and my_notes columns as you work. "
        "The other CSVs show the same rows split by source. A listing can appear in more than one source CSV.\n\n"
        "source_issue_numbers lists every open issue in which a listing was found. "
        "duplicate_count counts candidate appearances, including repeats within one issue. "
        "The newest recorded price is used when a listing appears repeatedly. "
        "Blank postage means the issue did not state it; blank total_price is therefore intentional.\n\n"
        "Reviewed packets and historical private-seller full-library scans are excluded. "
        "Expired auctions remain in the master, are marked expired, and are also in "
        "expired_auctions.csv. Listings have not been checked for current availability. "
        "Unpublished charity-seller discoveries are absent because they never became issues.\n\n"
        "export_summary.json has counts and any uncertain review status. "
        "The columns contain discovery claims, not verified edition or valuation conclusions.\n",
        encoding="utf-8",
    )
    files.append(readme)
    zip_path = output_dir.parent / "photobook_backlog_csvs.zip"
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.name)
    summary["zip_path"] = str(zip_path)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="jonattenborough/oxfam-photobook-monitor")
    parser.add_argument("--output-dir", type=Path, default=Path("runtime/backlog-export"))
    parser.add_argument("--issues-json", type=Path, help="Offline snapshot of the GitHub issues API list")
    parser.add_argument("--review-index", type=Path, default=REPO_ROOT / "data/photobook_review_index.json")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("--repo must be owner/repo")
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if args.issues_json:
        issues = json.loads(args.issues_json.read_text(encoding="utf-8"))
    else:
        issues = get_json_pages(f"https://api.github.com/repos/{args.repo}/issues?state=open&per_page=100", token)
    index = json.loads(args.review_index.read_text(encoding="utf-8")) if args.review_index.exists() else {}
    history_path = REPO_ROOT / "data/ebay_private_seller_review_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    legacy = {record["source_issue"] for record in history.get("seen", {}).values() if "source_issue" in record}
    summary = export(issues, args.output_dir, args.repo, datetime.now(timezone.utc),
                     index.get("comment_cache", {}), token, not bool(args.issues_json), legacy)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
